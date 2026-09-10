"""Flask backend for querying satellite ephemerides and DSS finder charts."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

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
    {"name": "S0 (6000)", "code": "6000"},
    {"name": "S8 (6008)", "code": "6008"},
    {"name": "S9 (6009)", "code": "6009"},
    {"name": "J0 (5000)", "code": "5000"},
    {"name": "J6 (10001)", "code": "10001"},
    {"name": "J7 (10002)", "code": "10002"},
    {"name": "J8 (10003)", "code": "10003"},
    {"name": "J9 (10004)", "code": "10004"},
    {"name": "N1 (8001)", "code": "8001"},
    {"name": "N2 (8002)", "code": "8002"},
    {"name": "U0 (7000)", "code": "7000"},
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
    dec_match = re.fullmatch(r"\s*([+-]?\d{1,2})\s+(\d{1,2})\s+(\d+(?:\.\d+)?)\s*", dec)
    if not ra_match or not dec_match:
        raise ValueError("赤经或赤纬格式错误")

    rah, ram, ras = int(ra_match[1]), int(ra_match[2]), float(ra_match[3])
    ded, dem, des = int(dec_match[1]), int(dec_match[2]), float(dec_match[3])
    if rah > 23 or ram > 59 or ras >= 60:
        raise ValueError("赤经超出有效范围")
    if abs(ded) > 90 or dem > 59 or des >= 60 or (abs(ded) == 90 and (dem or des)):
        raise ValueError("赤纬超出有效范围")
    return " ".join(ra.split()), " ".join(dec.split())


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


def parse_ephemeris(pre_content: str) -> list[dict[str, str]]:
    """Parse the data rows returned inside MSU's ``pre`` block."""
    results = []
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
        results.append({
            "time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "ra": f"{rah}h {ram}m {ras}s",
            "de": f"{ded}° {dem}' {des}\"",
            "ra_pure": ra_pure,
            "de_pure": de_pure,
        })
    return results


def fetch_satellite_data(plnvar, satellite, nde, observatory, initmom, ntimes, timestep):
    payload = {
        "langue": "30", "plnvar": plnvar, "satellite": satellite,
        "relative": "-1", "nde": nde, "observatory": observatory,
        "epoch": "ICRF", "tscale": "UTC", "initform": "1",
        "initmom": initmom, "steptype": "1", "timestep": timestep,
        "ntimes": ntimes, "outputtype": "0", "vangle": "0",
    }
    try:
        response = requests.post(
            MSU_URL, data=payload, headers=REQUEST_HEADERS, timeout=(5, 20)
        )
        response.raise_for_status()
    except requests.Timeout:
        return {"success": False, "message": "星历服务器请求超时，请稍后重试"}
    except requests.RequestException:
        return {"success": False, "message": "无法连接星历服务器，请稍后重试"}

    soup = BeautifulSoup(response.text, "html.parser")
    pre_tag = soup.find("pre")
    if not pre_tag:
        return {"success": False, "message": "星历服务器未返回预期的数据格式"}
    pre_content = pre_tag.get_text()
    page_title = soup.title.get_text(strip=True) if soup.title else ""
    if "error" in page_title.lower():
        return {
            "success": False,
            "message": (
                f"MSU 星历服务器不支持天体代码 {satellite}，"
                "请输入该自然卫星服务的代码，例如 5001（木卫一）；通用月球代码 301 不适用"
            ),
        }
    results = parse_ephemeris(pre_content)
    if not results:
        return {"success": False, "message": "未从星历服务器返回内容中解析到数据"}
    return {"success": True, "data": results}


def _safe_download_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return value[:60] or "Object"


@app.route("/")
def index():
    return render_template("index.html", satellites=SATELLITES, default_satellite="6000")


@app.route("/api/calculate", methods=["POST"])
def calculate():
    data = _get_json_object()
    if data is None:
        return _json_error("请求体必须是 JSON 对象")

    satellite = str(data.get("satellite", "")).strip()
    observatory = str(data.get("observatory", "")).strip()
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


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    host = os.environ.get("EPHEMERIS_HOST", "127.0.0.1")
    port = int(os.environ.get("EPHEMERIS_PORT", "8000"))
    app.run(host=host, port=port, debug=debug)
