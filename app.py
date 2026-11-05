"""Flask backend for querying satellite ephemerides and DSS finder charts."""

from __future__ import annotations

from typing import Any

import hashlib
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlencode

try:
    import numpy as np
    import astropy.units as u
    from astropy.coordinates import EarthLocation, AltAz, get_sun, get_body
    from astropy.time import Time
    from astropy.utils import iers
    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    HAS_ASTROPY = True
except ImportError:
    HAS_ASTROPY = False

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, send_file, url_for


app = Flask(__name__)

MSU_URL = "https://www.sai.msu.ru/neb/nss/cgi-bin/nss-eph3.cgi"
DSS_URL = "https://archive.stsci.edu/cgi-bin/dss_search"
REQUEST_HEADERS = {"User-Agent": "project-ephemeris/1.0"}
MAX_DSS_BYTES = 25 * 1024 * 1024
CACHE_DIR = Path(tempfile.gettempdir()) / "project_ephemeris_dss"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()

SATELLITES = [
    # 海王星系统（第一优先级）
    {"name": "N1 (8001)", "code": "8001", "planet": "neptune"},
    {"name": "N2 (8002)", "code": "8002", "planet": "neptune"},
    # 土星系统
    {"name": "S8 (6008)", "code": "6008", "planet": "saturn"},
    {"name": "S9 (6009)", "code": "6009", "planet": "saturn"},
    {"name": "S1~S7 (6001)", "code": "6001", "planet": "saturn"},
    # 天王星系统
    {"name": "U1~U5 (7001)", "code": "7001", "planet": "uranus"},
    # 木星系统（后半夜空闲观测）
    {"name": "J6 (10001)", "code": "10001", "planet": "jupiter"},
    {"name": "J7 (10002)", "code": "10002", "planet": "jupiter"},
    {"name": "J8 (10003)", "code": "10003", "planet": "jupiter"},
    {"name": "J9 (10004)", "code": "10004", "planet": "jupiter"},
]


def _json_error(message: str, status: int = 400):
    return jsonify({"success": False, "message": message}), status


def _get_json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _bounded_int(value, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _bounded_float(value, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return parsed


def _parse_coordinates(ra: str, dec: str) -> tuple[str, str]:
    ra_match = re.fullmatch(r"\s*(\d{1,2})\s+(\d{1,2})\s+(\d+(?:\.\d+)?)\s*", ra)
    # 兼容负赤纬在 0° 到 -1° 之间时 MSU 返回的 "0 -39 24.8" 或 "-0 39 24.8" 等特殊天文格式
    dec_match = re.fullmatch(r"\s*([+-]?\d{1,2})\s+([+-]?\d{1,2})\s+([+-]?\d+(?:\.\d+)?)\s*", dec)
    if not ra_match or not dec_match:
        raise ValueError("赤经或赤纬格式错误")

    rah, ram, ras_str = ra_match[1], ra_match[2], ra_match[3]
    raw_d, raw_m, raw_s = dec_match[1], dec_match[2], dec_match[3]

    rah_val, ram_val, ras_val = int(rah), int(ram), float(ras_str)
    # 检测符号位（若度、分或秒任何一处包含负号，均判定为负赤纬）
    is_negative = raw_d.startswith("-") or raw_m.startswith("-") or raw_s.startswith("-")
    ded_val = abs(int(raw_d))
    dem_val = abs(int(raw_m))
    des_val = abs(float(raw_s))

    if rah_val > 23 or ram_val > 59 or ras_val >= 60:
        raise ValueError("赤经超出有效范围")
    if ded_val > 90 or dem_val > 59 or des_val >= 60 or (ded_val == 90 and (dem_val or des_val)):
        raise ValueError("赤纬超出有效范围")

    sign = "-" if is_negative else ("+" if raw_d.startswith("+") else "")
    if is_negative and not sign:
        sign = "-"

    # 规范化：统一使用两位数补零，如 00 08 10.22 及 -00 39 24.88
    clean_des = raw_s.lstrip("+-")
    if des_val < 10 and not clean_des.startswith("0"):
        clean_des = f"0{clean_des}"
    clean_ras = ras_str
    if ras_val < 10 and not clean_ras.startswith("0"):
        clean_ras = f"0{clean_ras}"

    ra_normalized = f"{rah_val:02d} {ram_val:02d} {clean_ras}"
    dec_normalized = f"{sign}{ded_val:02d} {dem_val:02d} {clean_des}"
    return ra_normalized, dec_normalized


def _cache_id(ra: str, dec: str, height: float, width: float, survey: str, file_format: str) -> str:
    value = f"{ra}|{dec}|{height:g}|{width:g}|{survey}|{file_format}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_path(cache_id: str, file_format: str = "gif") -> Path:
    return CACHE_DIR / f"{cache_id}.{file_format}"


def _lock_for(cache_id: str) -> threading.Lock:
    with _cache_locks_guard:
        return _cache_locks.setdefault(cache_id, threading.Lock())


def download_dss_image(
    ra: str,
    dec: str,
    height: float = 7,
    width: float = 7,
    survey: str = "poss2ukstu_red",
    file_format: str = "gif",
) -> tuple[str, Path] | None:
    """Download a DSS image into the application-owned cache."""
    cache_id = _cache_id(ra, dec, height, width, survey, file_format)
    output_path = _cache_path(cache_id, file_format)

    with _lock_for(cache_id):
        if output_path.is_file() and output_path.stat().st_size:
            return cache_id, output_path

        params = {
            "v": survey, "r": ra, "d": dec, "e": "J2000",
            "h": height, "w": width, "f": file_format,
            "c": "none", "fov": "NONE", "v3": "",
        }
        partial_path = output_path.with_suffix(f".{file_format}.part-{os.getpid()}-{threading.get_ident()}")
        try:
            response = requests.get(
                DSS_URL, params=params, headers=REQUEST_HEADERS,
                stream=True, timeout=(5, 60),
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "image" not in content_type and "fits" not in content_type:
                return None
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_DSS_BYTES:
                return None

            total = 0
            with partial_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DSS_BYTES:
                        raise ValueError("DSS image exceeds size limit")
                    output.write(chunk)
            if not total:
                return None
            partial_path.replace(output_path)
            return cache_id, output_path
        except (OSError, ValueError, requests.RequestException):
            return None
        finally:
            partial_path.unlink(missing_ok=True)



import json

def _load_observatories() -> dict[str, dict[str, Any]]:
    json_path = Path(__file__).resolve().parent / "observatories.json"
    if json_path.is_file():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "286": {"name": "云南天文台昆明 1m望远镜", "lat": 25.0294, "lon": 102.7883, "alt": 2000.0},
        "O07": {"name": "云南天文台丽江 2.4m望远镜", "lat": 26.6950, "lon": 100.0300, "alt": 3200.0},
        "327": {"name": "国家天文台兴隆观测站 (2.16m/大视场)", "lat": 40.3964, "lon": 117.5750, "alt": 960.0},
        "500": {"name": "地心坐标", "lat": 0.0, "lon": 0.0, "alt": 0.0},
    }

OBSERVATORIES = _load_observatories()

def normalize_obs_code(raw_code: str) -> str:
    """Extract standard observatory code from raw string or labeled option."""
    s = str(raw_code or "").strip()
    m = re.search(r"\[?([A-Za-z0-9]{1,6})\]?", s)
    if m:
        return m.group(1).upper()
    return s[:16] or "286"


def get_observatory(obs_code: str) -> dict[str, Any]:
    raw = str(obs_code).strip()
    norm = normalize_obs_code(raw)
    for k in [norm, norm.upper(), norm.zfill(3), raw, raw.upper()]:
        if k in OBSERVATORIES:
            data = OBSERVATORIES[k]
            return {
                "code": k,
                "name": data.get("name", f"台站 {k}"),
                "lat": float(data.get("lat", 0.0)),
                "lon": float(data.get("lon", 0.0)),
                "alt": float(data.get("alt", 0.0)),
            }
    return {
        "code": norm or "286",
        "name": f"自定义台站 ({norm})" if norm else "云南天文台昆明 1m望远镜",
        "lat": 25.0294,
        "lon": 102.7883,
        "alt": 2000.0,
    }


TARGET_METADATA = {
    # 海王星系统 (Neptune)
    "8001": {"name": "海卫一 Triton (N1)", "diameter": "2,707 km", "angular_size": "约 0.13″", "mag": "13.5", "parent": "海王星 (Neptune)"},
    "8002": {"name": "海卫二 Nereid (N2)", "diameter": "350 km", "angular_size": "约 0.02″", "mag": "18.8~19.2", "parent": "海王星 (Neptune)"},
    "8000": {"name": "海王星 Neptune (中心)", "diameter": "49,244 km", "angular_size": "约 2.35″", "mag": "7.8", "parent": "海王星"},

    # 土星系统 (Saturn)
    "6008": {"name": "土卫八 Iapetus (S8)", "diameter": "1,469 km", "angular_size": "约 0.17″", "mag": "11.2", "parent": "土星 (Saturn)"},
    "6009": {"name": "土卫九 Phoebe (S9)", "diameter": "213 km", "angular_size": "约 0.02″", "mag": "16.5", "parent": "土星 (Saturn)"},
    "6001": {"name": "土卫一 Mimas (S1~S7)", "diameter": "396 km (系内396~5150km)", "angular_size": "0.05″~0.75″", "mag": "8.5~14.5", "parent": "土星 (Saturn)"},
    "6002": {"name": "土卫二 Enceladus (S2)", "diameter": "504 km", "angular_size": "约 0.07″", "mag": "11.7", "parent": "土星 (Saturn)"},
    "6003": {"name": "土卫三 Tethys (S3)", "diameter": "1,062 km", "angular_size": "约 0.15″", "mag": "10.2", "parent": "土星 (Saturn)"},
    "6004": {"name": "土卫四 Dione (S4)", "diameter": "1,123 km", "angular_size": "约 0.16″", "mag": "10.4", "parent": "土星 (Saturn)"},
    "6005": {"name": "土卫五 Rhea (S5)", "diameter": "1,527 km", "angular_size": "约 0.22″", "mag": "9.7", "parent": "土星 (Saturn)"},
    "6006": {"name": "土卫六 Titan (S6)", "diameter": "5,150 km", "angular_size": "约 0.75″", "mag": "8.5", "parent": "土星 (Saturn)"},
    "6007": {"name": "土卫七 Hyperion (S7)", "diameter": "270 km", "angular_size": "约 0.04″", "mag": "14.2", "parent": "土星 (Saturn)"},
    "6000": {"name": "土星 Saturn (中心)", "diameter": "120,536 km", "angular_size": "约 18.5″", "mag": "0.6", "parent": "土星"},

    # 天王星系统 (Uranus)
    "7001": {"name": "天卫一 Ariel (U1~U5)", "diameter": "1,158 km (系内472~1578km)", "angular_size": "0.03″~0.11″", "mag": "14.0~16.5", "parent": "天王星 (Uranus)"},
    "7002": {"name": "天卫二 Umbriel (U2)", "diameter": "1,169 km", "angular_size": "约 0.08″", "mag": "15.0", "parent": "天王星 (Uranus)"},
    "7003": {"name": "天卫三 Titania (U3)", "diameter": "1,578 km", "angular_size": "约 0.11″", "mag": "13.9", "parent": "天王星 (Uranus)"},
    "7004": {"name": "天卫四 Oberon (U4)", "diameter": "1,523 km", "angular_size": "约 0.11″", "mag": "14.1", "parent": "天王星 (Uranus)"},
    "7005": {"name": "天卫五 Miranda (U5)", "diameter": "472 km", "angular_size": "约 0.03″", "mag": "16.5", "parent": "天王星 (Uranus)"},
    "7000": {"name": "天王星 Uranus (中心)", "diameter": "50,724 km", "angular_size": "约 3.7″", "mag": "5.6", "parent": "天王星"},

    # 木星系统 (Jupiter)
    "10001": {"name": "木卫六 Himalia (J6)", "diameter": "170 km", "angular_size": "约 0.05″", "mag": "14.8", "parent": "木星 (Jupiter)"},
    "10002": {"name": "木卫七 Elara (J7)", "diameter": "86 km", "angular_size": "约 0.03″", "mag": "16.3", "parent": "木星 (Jupiter)"},
    "10003": {"name": "木卫八 Pasiphae (J8)", "diameter": "60 km", "angular_size": "约 0.02″", "mag": "17.0", "parent": "木星 (Jupiter)"},
    "10004": {"name": "木卫九 Sinope (J9)", "diameter": "38 km", "angular_size": "约 0.01″", "mag": "18.3", "parent": "木星 (Jupiter)"},
    "5001": {"name": "木卫一 Io (J1)", "diameter": "3,643 km", "angular_size": "约 1.2″", "mag": "5.0", "parent": "木星 (Jupiter)"},
    "5002": {"name": "木卫二 Europa (J2)", "diameter": "3,122 km", "angular_size": "约 1.0″", "mag": "5.3", "parent": "木星 (Jupiter)"},
    "5003": {"name": "木卫三 Ganymede (J3)", "diameter": "5,268 km", "angular_size": "约 1.7″", "mag": "4.6", "parent": "木星 (Jupiter)"},
    "5004": {"name": "木卫四 Callisto (J4)", "diameter": "4,821 km", "angular_size": "约 1.5″", "mag": "5.6", "parent": "木星 (Jupiter)"},
    "5000": {"name": "木星 Jupiter (中心)", "diameter": "139,820 km", "angular_size": "约 48.0″", "mag": "-2.7", "parent": "木星"},
}

def calculate_alt_az(stamp_utc: datetime, ra_str: str, dec_str: str, obs_code: str = "286") -> dict:
    station = get_observatory(obs_code)
    station_code = station["code"]

    if station_code == "500":
        return {
            "alt_deg": 0.0,
            "alt_str": "地心",
            "alt_dms": "00°00'",
            "az_deg": None,
            "is_observable": False,
            "obs_status": "geocentric",
            "status_text": "地心 (无地平)",
            "station_name": "地心坐标",
            "station_code": "500",
        }

    lat = station["lat"]
    lon = station["lon"]
    alt_m = station["alt"]

    ra_parts = ra_str.split()
    ra_h = int(ra_parts[0]) + int(ra_parts[1]) / 60.0 + float(ra_parts[2]) / 3600.0

    dec_parts = dec_str.split()
    is_neg = dec_parts[0].startswith("-") or dec_parts[1].startswith("-") or dec_parts[2].startswith("-")
    d_val = abs(int(dec_parts[0]))
    m_val = abs(int(dec_parts[1]))
    s_val = abs(float(dec_parts[2]))
    dec_d = d_val + m_val / 60.0 + s_val / 3600.0
    if is_neg:
        dec_d = -dec_d

    alt_deg = None
    az_deg = None

    if HAS_ASTROPY:
        try:
            loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=alt_m * u.m)
            sc = SkyCoord(ra=ra_h * 15.0 * u.deg, dec=dec_d * u.deg, frame="icrs")
            t = Time(stamp_utc, scale="utc")
            altaz = sc.transform_to(AltAz(obstime=t, location=loc))
            alt_deg = float(altaz.alt.deg)
            az_deg = float(altaz.az.deg)
        except Exception:
            pass

    if alt_deg is None:
        y, m, d = stamp_utc.year, stamp_utc.month, stamp_utc.day
        h = stamp_utc.hour + stamp_utc.minute / 60.0 + stamp_utc.second / 3600.0
        if m <= 2:
            y -= 1
            m += 12
        A = math.floor(y / 100)
        B = 2 - A + math.floor(A / 4)
        jd = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5 + h / 24.0
        d_t = jd - 2451545.0
        gmst = (280.46061837 + 360.98564736629 * d_t) % 360.0
        lmst = (gmst + lon) % 360.0
        ha_deg = (lmst - (ra_h * 15.0)) % 360.0

        ha_rad = math.radians(ha_deg)
        dec_rad = math.radians(dec_d)
        lat_rad = math.radians(lat)

        sin_alt = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
        alt_deg = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))

        cos_az = (math.sin(dec_rad) - math.sin(lat_rad) * sin_alt) / (math.cos(lat_rad) * math.cos(math.radians(alt_deg)) + 1e-12)
        sin_az = -math.cos(dec_rad) * math.sin(ha_rad) / (math.cos(math.radians(alt_deg)) + 1e-12)
        az_deg = (math.degrees(math.atan2(sin_az, cos_az))) % 360.0

    sign = "+" if alt_deg >= 0 else "-"
    abs_alt = abs(alt_deg)
    d = int(abs_alt)
    m = int((abs_alt - d) * 60)

    if alt_deg >= 30.0:
        obs_status = "observable"
        status_text = "适测 (≥30°)"
    elif alt_deg >= 0:
        obs_status = "low"
        status_text = "低空 (<30°)"
    else:
        obs_status = "below_horizon"
        status_text = "地平线下"

    return {
        "alt_deg": round(alt_deg, 2),
        "alt_str": f"{alt_deg:+.1f}°",
        "alt_dms": f"{sign}{d:02d}°{m:02d}'",
        "az_deg": round(az_deg, 1) if az_deg is not None else None,
        "is_observable": alt_deg >= 30.0,
        "obs_status": obs_status,
        "status_text": status_text,
        "station_name": station.get("name", f"台站 {station_code}"),
        "station_code": station_code,
    }


def parse_ephemeris(pre_content: str, observatory: str = "286", satellite: str = "") -> list[dict[str, Any]]:
    """Parse the data rows returned inside MSU's ``pre`` block and compute Alt relative to Yunnan Observatory."""
    results = []
    meta = TARGET_METADATA.get(str(satellite), {})
    for line in pre_content.splitlines():
        stripped = line.strip()
        if not re.match(r"\d{4}\s+\d+\s+\d+", stripped):
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) < 12:
            continue
        try:
            stamp = datetime(
                int(parts[0]), int(parts[1]), int(parts[2]),
                int(parts[3]), int(parts[4]), int(float(parts[5])),
            )
            ra_pure, de_pure = _parse_coordinates(" ".join(parts[6:9]), " ".join(parts[9:12]))
        except (ValueError, OverflowError):
            continue
        rah, ram, ras = ra_pure.split()
        ded, dem, des = de_pure.split()

        alt_info = calculate_alt_az(stamp, ra_pure, de_pure, obs_code=observatory)

        results.append({
            "time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "ra": f"{rah}h {ram}m {ras}s",
            "de": f"{ded}° {dem}' {des}\"",
            "ra_pure": ra_pure,
            "de_pure": de_pure,
            "alt_deg": alt_info["alt_deg"],
            "alt_str": alt_info["alt_str"],
            "alt_dms": alt_info["alt_dms"],
            "az_deg": alt_info["az_deg"],
            "is_observable": alt_info["is_observable"],
            "obs_status": alt_info["obs_status"],
            "status_text": alt_info["status_text"],
            "station_name": alt_info["station_name"],
            "diameter": meta.get("diameter", "--"),
            "angular_size": meta.get("angular_size", "--"),
            "mag": meta.get("mag", "--"),
        })
    return results


def fetch_satellite_data(plnvar, satellite, nde, observatory, initmom, ntimes, timestep):
    observatory = normalize_obs_code(observatory)
    payload = {
        "langue": "30", "plnvar": plnvar, "satellite": satellite,
        "relative": "-1", "nde": nde, "observatory": observatory,
        "epoch": "ICRF", "tscale": "UTC", "initform": "1",
        "initmom": initmom, "steptype": "1", "timestep": timestep,
        "ntimes": ntimes, "outputtype": "0", "vangle": "0",
    }
    try:
        # 放宽连接与读取超时，增强弱网抗波动能力
        response = requests.post(
            MSU_URL, data=payload, headers=REQUEST_HEADERS, timeout=(12, 30)
        )
        response.raise_for_status()
    except requests.Timeout:
        return {"success": False, "message": "星历服务器请求超时，请稍后重试"}
    except requests.RequestException:
        return {"success": False, "message": "无法连接星历服务器，请稍后重试"}

    soup = BeautifulSoup(response.text, "html.parser")
    pre_tag = soup.find("pre")
    pre_content = pre_tag.get_text() if pre_tag else ""
    page_title = soup.title.get_text(strip=True) if soup.title else ""

    # 若 MSU 历史内置台站表中未收录该台站代码（如近年设立的 O07 丽江 2.4m 等）
    # 自动以 500 (地心坐标) 获取该天体天球坐标，本地再基于所选测站的精确经纬度与海拔推算高度角与晨昏窗口
    if "not found in the observatory list" in pre_content.lower():
        fallback_payload = dict(payload)
        fallback_payload["observatory"] = "500"
        try:
            fb_response = requests.post(
                MSU_URL, data=fallback_payload, headers=REQUEST_HEADERS, timeout=(12, 30)
            )
            fb_response.raise_for_status()
            fb_soup = BeautifulSoup(fb_response.text, "html.parser")
            fb_pre = fb_soup.find("pre")
            if fb_pre and "not found in the observatory list" not in fb_pre.get_text().lower():
                pre_tag = fb_pre
                pre_content = fb_pre.get_text()
                page_title = fb_soup.title.get_text(strip=True) if fb_soup.title else ""
        except Exception:
            pass

    if not pre_tag:
        return {"success": False, "message": "星历服务器未返回预期的数据格式"}

    if "error" in page_title.lower() or "error is discovered" in pre_content.lower():
        if "not found in the observatory list" in pre_content.lower():
            return {
                "success": False,
                "message": f"MSU 星历服务器未收录测站代码 {observatory}，且地心回退失败，请核对测站代码",
            }
        return {
            "success": False,
            "message": (
                f"MSU 星历服务器不支持天体代码 {satellite}，"
                "请输入该自然卫星服务的代码，例如 5001（木卫一）；通用月球代码 301 不适用"
            ),
        }

    results = parse_ephemeris(pre_content, observatory=observatory, satellite=satellite)
    if not results:
        return {"success": False, "message": "未从星历服务器返回内容中解析到数据"}

    target_info = TARGET_METADATA.get(str(satellite), {
        "name": f"天体 {satellite}",
        "diameter": "--",
        "angular_size": "--",
        "mag": "--",
        "parent": "--"
    })
    station = get_observatory(observatory)

    return {
        "success": True,
        "data": results,
        "target_info": target_info,
        "station": station
    }


def _safe_download_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return value[:60] or "Object"


@app.route("/")
def index():
    return render_template("index.html", satellites=SATELLITES, default_satellite="8001")


@app.route("/api/calculate", methods=["POST"])
def calculate():
    data = _get_json_object()
    if data is None:
        return _json_error("请求体必须是 JSON 对象")

    satellite = str(data.get("satellite", "")).strip()
    raw_obs = str(data.get("observatory", "")).strip()
    observatory = normalize_obs_code(raw_obs)
    initmom = " ".join(str(data.get("initmom", "")).split())
    if not re.fullmatch(r"\d{1,10}", satellite):
        return _json_error("天体代码必须是 1 到 10 位数字")
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", observatory):
        return _json_error("观测站代码格式错误")
    try:
        datetime.strptime(initmom, "%Y %m %d %H %M %S")
    except ValueError:
        return _json_error("时间格式或日期无效，请使用 YYYY MM DD HH mm ss")
    try:
        nde = _bounded_int(data.get("nde", 6), "历表类型", 0, 10)
        ntimes = _bounded_int(data.get("ntimes", 1), "步数", 1, 100)
        timestep = _bounded_float(data.get("timestep", 1), "步长", 0.001, 8760)
    except ValueError as exc:
        return _json_error(str(exc))

    return jsonify(fetch_satellite_data(
        "0", satellite, str(nde), observatory, initmom, str(ntimes), f"{timestep:g}"
    ))


@app.route("/api/download_chart", methods=["POST"])
def download_chart():
    data = _get_json_object()
    if data is None:
        return _json_error("请求体必须是 JSON 对象")
    try:
        ra, dec = _parse_coordinates(str(data.get("ra", "")), str(data.get("dec", "")))
        fov = _bounded_float(data.get("fov", 10), "视场", 1, 60)
    except ValueError as exc:
        return _json_error(str(exc))

    result = download_dss_image(ra, dec, height=fov, width=fov)
    if result is None:
        return _json_error("从 DSS 服务器获取图片失败", 502)
    cache_id, _ = result

    try:
        dt = datetime.strptime(str(data.get("time_str", "")), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.now(timezone.utc)
    satellite_name = _safe_download_name(str(data.get("satellite_name", "Unknown")).split("(")[0])
    filename = f"{satellite_name}_{dt.strftime('%Y%m%d_%Hh%Mm%Ss')}_fov{fov:g}.gif"
    query = urlencode({"filename": filename})
    download_url = f"{url_for('get_file', cache_id=cache_id)}?{query}"
    return jsonify({"success": True, "download_url": download_url, "filename": filename})


@app.route("/api/get_file/<cache_id>")
def get_file(cache_id: str):
    if not re.fullmatch(r"[0-9a-f]{64}", cache_id):
        return "File not found", 404
    filepath = _cache_path(cache_id)
    if not filepath.is_file():
        return "File not found", 404
    filename = _safe_download_name(request.args.get("filename", "dss_chart.gif"))
    if not filename.lower().endswith(".gif"):
        filename += ".gif"
    return send_file(filepath, mimetype="image/gif", download_name=filename)


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nDisallow:", 200, {"Content-Type": "text/plain"}


@app.route("/favicon.ico")
@app.route("/favicon.png")
def favicon():
    # This historical asset is JPEG-encoded despite its .png filename.
    return send_file(Path(app.root_path) / "static" / "favicon.png", mimetype="image/jpeg")


_SCHEDULE_CACHE = {}

def compute_dynamic_windows(bjt_dt=None, obs_code="286"):
    """Compute true astronomical twilight and planet >= 30 deg windows using Astropy."""
    bjt = ZoneInfo("Asia/Shanghai")
    if bjt_dt is None:
        bjt_dt = datetime.now(timezone.utc).astimezone(bjt)
    elif bjt_dt.tzinfo is None:
        bjt_dt = bjt_dt.replace(tzinfo=bjt)

    # Observation night (noon to noon next day)
    if bjt_dt.hour < 12:
        night_date = (bjt_dt - timedelta(days=1)).date()
    else:
        night_date = bjt_dt.date()

    station = get_observatory(obs_code)
    cache_key = f"{night_date.isoformat()}_{obs_code}"

    if cache_key not in _SCHEDULE_CACHE and HAS_ASTROPY:
        t_noon = datetime(night_date.year, night_date.month, night_date.day, 12, 0, 0, tzinfo=bjt)
        loc = EarthLocation(lat=station["lat"]*u.deg, lon=station["lon"]*u.deg, height=station["alt"]*u.m)

        # Sample every 3 minutes for 24 hours
        minutes = np.arange(0, 24 * 60 + 1, 3)
        dt_list = [t_noon + timedelta(minutes=int(m)) for m in minutes]
        times = Time(dt_list)
        frame = AltAz(obstime=times, location=loc)

        sun_alt = get_sun(times).transform_to(frame).alt.deg
        dark_mask = sun_alt <= -18.0
        dark_indices = np.where(dark_mask)[0]

        if len(dark_indices) > 0:
            dark_start = dt_list[dark_indices[0]]
            dark_end = dt_list[dark_indices[-1]]
            twilight = {
                "dusk": dark_start.strftime("%H:%M"),
                "dawn": dark_end.strftime("%H:%M"),
                "dusk_iso": dark_start.isoformat(),
                "dawn_iso": dark_end.isoformat(),
            }
        else:
            twilight = {"dusk": "--:--", "dawn": "--:--"}

        planets_data = {}
        for p_key, p_name in [("neptune", "neptune"), ("saturn", "saturn"), ("uranus", "uranus"), ("jupiter", "jupiter")]:
            body = get_body(p_name, times, location=loc)
            alt = body.transform_to(frame).alt.deg
            valid_mask = (alt >= 30.0) & dark_mask
            valid_indices = np.where(valid_mask)[0]

            if len(valid_indices) > 0:
                w_start = dt_list[valid_indices[0]]
                w_end = dt_list[valid_indices[-1]]
                planets_data[p_key] = {
                    "valid": True,
                    "start": w_start.strftime("%H:%M"),
                    "end": w_end.strftime("%H:%M"),
                    "start_iso": w_start.isoformat(),
                    "end_iso": w_end.isoformat(),
                    "max_alt": round(float(np.max(alt)), 1),
                }
            else:
                planets_data[p_key] = {"valid": False}

        _SCHEDULE_CACHE[cache_key] = {"twilight": twilight, "planets": planets_data}

    schedule = _SCHEDULE_CACHE.get(cache_key, {
        "twilight": {"dusk": "19:44", "dawn": "06:01"},
        "planets": {
            "neptune": {"valid": True, "start": "19:44", "end": "02:03", "start_iso": f"{night_date}T19:44:00+08:00", "end_iso": f"{(night_date+timedelta(days=1))}T02:03:00+08:00", "max_alt": 64.5},
            "saturn":  {"valid": True, "start": "19:44", "end": "02:34", "start_iso": f"{night_date}T19:44:00+08:00", "end_iso": f"{(night_date+timedelta(days=1))}T02:34:00+08:00", "max_alt": 66.1},
            "uranus":  {"valid": True, "start": "21:58", "end": "06:01", "start_iso": f"{night_date}T21:58:00+08:00", "end_iso": f"{(night_date+timedelta(days=1))}T06:01:00+08:00", "max_alt": 85.9},
            "jupiter": {"valid": True, "start": "03:46", "end": "06:01", "start_iso": f"{(night_date+timedelta(days=1))}T03:46:00+08:00", "end_iso": f"{(night_date+timedelta(days=1))}T06:01:00+08:00", "max_alt": 78.9},
        }
    })

    planets_eval = {}
    best_candidate = "8001"
    priority_order = [("neptune", "8001"), ("saturn", "6008"), ("saturn", "6001"), ("uranus", "7001"), ("jupiter", "10001")]
    found_best = False

    for p_key, p_info in schedule["planets"].items():
        if not p_info.get("valid"):
            planets_eval[p_key] = {"valid": False, "status": "none", "pct": 0, "badge": "无窗口"}
            continue

        st = datetime.fromisoformat(p_info["start_iso"])
        et = datetime.fromisoformat(p_info["end_iso"])
        tot_sec = (et - st).total_seconds()

        if bjt_dt < st:
            planets_eval[p_key] = {
                "valid": True, "status": "waiting", "pct": 100,
                "start": p_info["start"], "end": p_info["end"], "max_alt": p_info.get("max_alt", 0),
                "badge": f"{p_info['start']}开",
                "tooltip": f"【待开启】今晚窗口: {p_info['start']} ~ {p_info['end']} (预计 {p_info['start']} 升至30°)"
            }
        elif bjt_dt > et:
            planets_eval[p_key] = {
                "valid": True, "status": "ended", "pct": 0,
                "start": p_info["start"], "end": p_info["end"], "max_alt": p_info.get("max_alt", 0),
                "badge": "已落",
                "tooltip": f"【已落】窗口已结束 (已于 {p_info['end']} 降至30°以下)"
            }
        else:
            rem_sec = (et - bjt_dt).total_seconds()
            pct = max(0, min(100, round((rem_sec / tot_sec) * 100)))
            rem_h = rem_sec / 3600.0
            planets_eval[p_key] = {
                "valid": True, "status": "observing", "pct": pct,
                "start": p_info["start"], "end": p_info["end"], "max_alt": p_info.get("max_alt", 0),
                "badge": f"{pct}%",
                "tooltip": f"【可观测】剩余 {rem_h:.1f}小时 (至 {p_info['end']})，窗口总长: {p_info['start']} ~ {p_info['end']}"
            }
            if not found_best:
                for cand_p, cand_code in priority_order:
                    if cand_p == p_key:
                        best_candidate = cand_code
                        found_best = True
                        break

    return {
        "success": True,
        "night_date": night_date.isoformat(),
        "station": station,
        "twilight": schedule["twilight"],
        "planets": planets_eval,
        "best_target": best_candidate
    }


@app.route("/api/observation_status")
def observation_status():
    obs = normalize_obs_code(request.args.get("obs", "286"))
    time_str = request.args.get("time", "").strip()
    bjt_dt = None
    if time_str:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y %m %d %H %M %S", "%Y-%m-%d"]:
            try:
                bjt_dt = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                pass
    return jsonify(compute_dynamic_windows(bjt_dt, obs))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    host = os.environ.get("EPHEMERIS_HOST", "127.0.0.1")
    port = int(os.environ.get("EPHEMERIS_PORT", "8000"))
    app.run(host=host, port=port, debug=debug)
