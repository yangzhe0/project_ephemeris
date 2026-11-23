"""Astronomy window calculation, max-min fair scheduling, and Word (.docx) export."""

from __future__ import annotations

import io
import math
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from planner_data import (
    EXPOSURE_RULES,
    FIELD_CHECKLIST,
    PLANET_META,
    PLANNER_TARGETS_BY_ID,
    STATION_PROFILES,
)

try:
    import numpy as np
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body, get_sun
    from astropy.time import Time
    from astropy.utils import iers

    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    HAS_ASTROPY = True
except ImportError:
    HAS_ASTROPY = False

BJT = ZoneInfo("Asia/Shanghai")
SAMPLE_STEP_MIN = 5
WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def offset_min_to_hhmm(offset_min: int | float | None) -> str:
    """Convert minutes since local 12:00 to HH:MM (wrapping 24h)."""
    if offset_min is None:
        return "--:--"
    total_min = int(round(offset_min))
    clock_min = (12 * 60 + total_min) % (24 * 60)
    return f"{clock_min // 60:02d}:{clock_min % 60:02d}"


def format_duration_zh(minutes: int) -> str:
    minutes = max(0, int(round(minutes)))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def resolve_target_meta(tid: str) -> dict[str, Any] | None:
    """Resolve a built-in target ID or custom:<planet>:<label> specification."""
    if tid in PLANNER_TARGETS_BY_ID:
        return PLANNER_TARGETS_BY_ID[tid]
    if tid.startswith("custom:"):
        parts = tid.split(":", 2)
        if len(parts) == 3:
            planet = parts[1].strip().lower()
            label = parts[2].strip()[:24]
            if planet in PLANET_META and label:
                pm = PLANET_META[planet]
                return {
                    "id": tid,
                    "code": label,
                    "planet": planet,
                    "planet_zh": pm["zh"],
                    "planet_en": pm["en"],
                    "name": f"{label} ({pm['zh']}系自定义)",
                    "short": label,
                    "color": pm.get("color", "#4f46e5"),
                    "order": 90,
                }
    return None


def _resolve_exposure_rule(t_meta: dict[str, Any]) -> dict[str, Any]:
    tid = t_meta["id"]
    if tid in EXPOSURE_RULES:
        return EXPOSURE_RULES[tid]
    return {
        "target": t_meta["name"],
        "code": t_meta["short"],
        "planet": t_meta["planet_zh"],
        "pointing": f"{t_meta['short']}历表指向",
        "mag": "--",
        "filter": "I",
        "suggested_exposure_s": "按试拍调整",
        "suggested_interval_s": "0",
        "suggested_frames": "20 ~ 30",
        "snr_baseline": "--",
        "notes": "自定义添加目标，建议现场试拍 1~2 帧确认信噪比与背景饱和度后再连续采样。",
    }


def _longest_true_run(mask: Any) -> tuple[int, int] | None:
    """Return (start_idx, end_idx) inclusive of the longest True contiguous run."""
    indices = [i for i, flag in enumerate(mask) if bool(flag)]
    if not indices:
        return None
    best_start = cur_start = indices[0]
    best_end = cur_end = indices[0]
    for idx in indices[1:]:
        if idx == cur_end + 1:
            cur_end = idx
        else:
            if (cur_end - cur_start) > (best_end - best_start):
                best_start, best_end = cur_start, cur_end
            cur_start = cur_end = idx
    if (cur_end - cur_start) > (best_end - best_start):
        best_start, best_end = cur_start, cur_end
    return best_start, best_end


def _is_feasible_durations(items: list[dict[str, Any]], durations: list[int]) -> list[int] | None:
    """Greedy left-aligned feasibility check for ordered intervals with given durations."""
    starts: list[int] = []
    prev_end = -10**9
    for item, dur in zip(items, durations):
        if dur < 0:
            return None
        s = max(item["win_start"], prev_end)
        e = s + dur
        if e > item["win_end"]:
            return None
        starts.append(s)
        prev_end = e
    return starts


def _center_blocks_near_transit(
    items: list[dict[str, Any]],
    starts: list[int],
    durations: list[int],
) -> list[int]:
    """Shift blocks within their slack towards each target's transit time while keeping order."""
    n = len(items)
    if n == 0:
        return []
    pos = list(starts)
    # Two-pass relaxation: right-to-left then left-to-right
    for _ in range(2):
        for i in range(n - 1, -1, -1):
            dur = durations[i]
            desired = int(round((items[i]["transit_min"] - dur / 2.0) / SAMPLE_STEP_MIN)) * SAMPLE_STEP_MIN
            low = items[i]["win_start"]
            if i > 0:
                low = max(low, pos[i - 1] + durations[i - 1])
            high = items[i]["win_end"] - dur
            if i < n - 1:
                high = min(high, pos[i + 1] - dur)
            if low <= high:
                pos[i] = max(low, min(high, desired))
        for i in range(n):
            dur = durations[i]
            desired = int(round((items[i]["transit_min"] - dur / 2.0) / SAMPLE_STEP_MIN)) * SAMPLE_STEP_MIN
            low = items[i]["win_start"]
            if i > 0:
                low = max(low, pos[i - 1] + durations[i - 1])
            high = items[i]["win_end"] - dur
            if i < n - 1:
                high = min(high, pos[i + 1] - dur)
            if low <= high:
                pos[i] = max(low, min(high, desired))
    return pos


def auto_allocate_schedule(
    dusk_min: int,
    dawn_min: int,
    target_windows: list[dict[str, Any]],
    step: int = SAMPLE_STEP_MIN,
) -> list[dict[str, Any]]:
    """Allocate non-overlapping, balanced durations inside each target's valid window.

    Uses max-min fair water-filling:
    1. Sort valid targets by transit time (then canonical order).
    2. Binary search the maximum uniform duration feasible for all targets.
    3. Iteratively expand the shortest expandable target by `step` until saturated.
    4. Gently center blocks near transit times within available slack.
    """
    valid_items: list[dict[str, Any]] = []
    for item in target_windows:
        if not item.get("valid"):
            continue
        w_start = max(dusk_min, int(item["win_start"]))
        w_end = min(dawn_min, int(item["win_end"]))
        # Snap inward to step grid
        w_start = ((w_start + step - 1) // step) * step
        w_end = (w_end // step) * step
        if w_end - w_start < step:
            continue
        valid_items.append(
            {
                "target_id": item["target_id"],
                "planet": item["planet"],
                "win_start": w_start,
                "win_end": w_end,
                "transit_min": item.get("transit_min", (w_start + w_end) // 2),
                "order": item.get("order", 99),
            }
        )

    if not valid_items:
        return []

    valid_items.sort(key=lambda x: (x["transit_min"], x["win_end"], x["order"]))
    n = len(valid_items)

    # Step 1: Binary search max uniform duration (in multiples of `step`)
    max_possible_steps = max(1, (dawn_min - dusk_min) // step)
    lo, hi = 0, max_possible_steps
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _is_feasible_durations(valid_items, [mid * step] * n) is not None:
            lo = mid
        else:
            hi = mid - 1

    durations = [lo * step] * n

    # Step 2: Water-filling expansion for targets with remaining room
    active = [True] * n
    while any(active):
        # Pick active targets with currently smallest duration
        min_dur = min(durations[i] for i in range(n) if active[i])
        candidates = [i for i in range(n) if active[i] and durations[i] == min_dur]
        progressed = False
        for idx in candidates:
            trial = list(durations)
            trial[idx] += step
            if _is_feasible_durations(valid_items, trial) is not None:
                durations[idx] += step
                progressed = True
            else:
                active[idx] = False
        if not progressed:
            break

    starts = _is_feasible_durations(valid_items, durations)
    if starts is None:
        return []

    # Step 3: Center near transit without breaking order or window bounds
    starts = _center_blocks_near_transit(valid_items, starts, durations)

    blocks: list[dict[str, Any]] = []
    for item, s, dur in zip(valid_items, starts, durations):
        if dur <= 0:
            continue
        meta = resolve_target_meta(item["target_id"]) or {}
        blocks.append(
            {
                "kind": "target",
                "target_id": item["target_id"],
                "label": meta.get("short", item["target_id"]),
                "name": meta.get("name", item["target_id"]),
                "planet": item["planet"],
                "color": meta.get("color", "#2563eb"),
                "start_min": s,
                "end_min": s + dur,
                "start_time": offset_min_to_hhmm(s),
                "end_time": offset_min_to_hhmm(s + dur),
                "duration_min": dur,
                "note": "",
            }
        )
    return blocks


def _sample_alt_at(samples: list[float], offset_min: int) -> float | None:
    if not samples:
        return None
    idx = int(round(offset_min / SAMPLE_STEP_MIN))
    idx = max(0, min(len(samples) - 1, idx))
    return samples[idx]


def _compute_single_night_astropy(
    night_date: date,
    station: dict[str, Any],
    planet_keys: list[str],
    min_alt: float,
) -> dict[str, Any]:
    t_noon = datetime(night_date.year, night_date.month, night_date.day, 12, 0, 0, tzinfo=BJT)
    loc = EarthLocation(
        lat=float(station["lat"]) * u.deg,
        lon=float(station["lon"]) * u.deg,
        height=float(station.get("alt", 0.0)) * u.m,
    )
    minutes = np.arange(0, 24 * 60 + 1, SAMPLE_STEP_MIN)
    dt_list = [t_noon + timedelta(minutes=int(m)) for m in minutes]
    times = Time(dt_list)
    frame = AltAz(obstime=times, location=loc)

    sun_body = get_body("sun", times, location=loc)
    sun_alt = sun_body.transform_to(frame).alt.deg

    naut_mask = sun_alt <= -12.0
    astro_mask = sun_alt <= -18.0
    sun_down_mask = sun_alt <= -0.833

    naut_run = _longest_true_run(naut_mask)
    astro_run = _longest_true_run(astro_mask)
    sun_run = _longest_true_run(sun_down_mask)

    if naut_run:
        dusk_min = int(minutes[naut_run[0]])
        dawn_min = int(minutes[naut_run[1]])
    else:
        dusk_min, dawn_min = 420, 1080  # Fallback 19:00 - 06:00

    if astro_run:
        astro_dusk_min = int(minutes[astro_run[0]])
        astro_dawn_min = int(minutes[astro_run[1]])
    else:
        astro_dusk_min, astro_dawn_min = dusk_min, dawn_min

    sunset_min = int(minutes[sun_run[0]]) if sun_run else dusk_min - 50
    sunrise_min = int(minutes[min(sun_run[1] + 1, len(minutes) - 1)]) if sun_run else dawn_min + 50
    solar_offset_min = round((float(station.get("lon", 120.0)) - 120.0) * 4.0)

    # Moon calculation
    moon_body = get_body("moon", times, location=loc)
    moon_alt = moon_body.transform_to(frame).alt.deg
    mid_idx = int(round(((dusk_min + dawn_min) / 2.0) / SAMPLE_STEP_MIN))
    mid_idx = max(0, min(len(minutes) - 1, mid_idx))
    elong_rad = float(sun_body[mid_idx].separation(moon_body[mid_idx]).rad)
    moon_illum_pct = round(50.0 * (1.0 - math.cos(elong_rad)), 1)

    moon_night_mask = (moon_alt > 0.0) & naut_mask
    moon_night_run = _longest_true_run(moon_night_mask)
    moon_up_run = _longest_true_run(moon_alt > 0.0)

    moon_info = {
        "illum_pct": moon_illum_pct,
        "in_night": moon_night_run is not None,
        "night_start_min": int(minutes[moon_night_run[0]]) if moon_night_run else None,
        "night_end_min": int(minutes[moon_night_run[1]]) if moon_night_run else None,
        "night_start": offset_min_to_hhmm(int(minutes[moon_night_run[0]])) if moon_night_run else "--:--",
        "night_end": offset_min_to_hhmm(int(minutes[moon_night_run[1]])) if moon_night_run else "--:--",
        "rise": offset_min_to_hhmm(int(minutes[moon_up_run[0]])) if moon_up_run else "--:--",
        "set": offset_min_to_hhmm(int(minutes[moon_up_run[1]])) if moon_up_run else "--:--",
    }

    planets_data: dict[str, Any] = {}
    for p_key in planet_keys:
        body = get_body(p_key, times, location=loc)
        alt = body.transform_to(frame).alt.deg
        alt_list = [round(float(v), 1) for v in alt]

        above_horizon_run = _longest_true_run(alt > 0.0)
        transit_idx = int(np.argmax(alt))
        transit_min = int(minutes[transit_idx])
        max_alt = round(float(alt[transit_idx]), 1)

        valid_mask = (alt >= min_alt) & naut_mask
        valid_run = _longest_true_run(valid_mask)

        if valid_run:
            w_start = int(minutes[valid_run[0]])
            w_end = int(minutes[valid_run[1]])
            valid_flag = (w_end - w_start) >= SAMPLE_STEP_MIN
        else:
            w_start, w_end, valid_flag = None, None, False

        planets_data[p_key] = {
            "planet": p_key,
            "name_zh": PLANET_META[p_key]["zh"],
            "name_en": PLANET_META[p_key]["en"],
            "mag": PLANET_META[p_key]["mag"],
            "valid": valid_flag,
            "win_start": w_start,
            "win_end": w_end,
            "win_start_time": offset_min_to_hhmm(w_start),
            "win_end_time": offset_min_to_hhmm(w_end),
            "rise_time": offset_min_to_hhmm(int(minutes[above_horizon_run[0]])) if above_horizon_run else "--:--",
            "transit_min": transit_min,
            "transit_time": offset_min_to_hhmm(transit_min),
            "set_time": offset_min_to_hhmm(int(minutes[above_horizon_run[1]])) if above_horizon_run else "--:--",
            "max_alt": max_alt,
            "zenith_warning": max_alt >= 85.0,
            "alt_samples": alt_list,
        }

    return {
        "date": night_date.isoformat(),
        "weekday": WEEKDAYS_ZH[night_date.weekday()],
        "twilight": {
            "sunset_min": sunset_min,
            "sunset": offset_min_to_hhmm(sunset_min),
            "dusk_min": dusk_min,
            "dusk": offset_min_to_hhmm(dusk_min),
            "astro_dusk_min": astro_dusk_min,
            "astro_dusk": offset_min_to_hhmm(astro_dusk_min),
            "astro_dawn_min": astro_dawn_min,
            "astro_dawn": offset_min_to_hhmm(astro_dawn_min),
            "dawn_min": dawn_min,
            "dawn": offset_min_to_hhmm(dawn_min),
            "sunrise_min": sunrise_min,
            "sunrise": offset_min_to_hhmm(sunrise_min),
            "dark_duration_min": max(0, dawn_min - dusk_min),
            "solar_offset_min": solar_offset_min,
        },
        "moon": moon_info,
        "planets": planets_data,
    }


def _compute_single_night_fallback(
    night_date: date,
    planet_keys: list[str],
) -> dict[str, Any]:
    """Deterministic fallback if Astropy is unavailable."""
    dusk_min, dawn_min = 460, 1080  # 19:40 - 06:00
    presets = {
        "neptune": (460, 840, 620, 64.5),
        "saturn": (460, 870, 650, 66.1),
        "uranus": (600, 1080, 860, 85.9),
        "jupiter": (940, 1080, 1050, 78.9),
    }
    planets_data = {}
    for p_key in planet_keys:
        ws, we, tr, ma = presets.get(p_key, (480, 960, 720, 65.0))
        planets_data[p_key] = {
            "planet": p_key,
            "name_zh": PLANET_META[p_key]["zh"],
            "name_en": PLANET_META[p_key]["en"],
            "mag": PLANET_META[p_key]["mag"],
            "valid": True,
            "win_start": ws,
            "win_end": we,
            "win_start_time": offset_min_to_hhmm(ws),
            "win_end_time": offset_min_to_hhmm(we),
            "rise_time": offset_min_to_hhmm(max(0, ws - 120)),
            "transit_min": tr,
            "transit_time": offset_min_to_hhmm(tr),
            "set_time": offset_min_to_hhmm(min(1440, we + 120)),
            "max_alt": ma,
            "zenith_warning": ma >= 85.0,
            "alt_samples": [round(max(-10.0, ma - abs(m - tr) * 0.22), 1) for m in range(0, 1441, SAMPLE_STEP_MIN)],
        }
    return {
        "date": night_date.isoformat(),
        "weekday": WEEKDAYS_ZH[night_date.weekday()],
        "twilight": {
            "sunset_min": 410,
            "sunset": offset_min_to_hhmm(410),
            "dusk_min": dusk_min,
            "dusk": offset_min_to_hhmm(dusk_min),
            "astro_dusk_min": 490,
            "astro_dusk": offset_min_to_hhmm(490),
            "astro_dawn_min": 1050,
            "astro_dawn": offset_min_to_hhmm(1050),
            "dawn_min": dawn_min,
            "dawn": offset_min_to_hhmm(dawn_min),
            "sunrise_min": 1130,
            "sunrise": offset_min_to_hhmm(1130),
            "dark_duration_min": dawn_min - dusk_min,
            "solar_offset_min": -69,
        },
        "moon": {
            "illum_pct": 35.0,
            "in_night": False,
            "night_start_min": None,
            "night_end_min": None,
            "night_start": "--:--",
            "night_end": "--:--",
            "rise": "--:--",
            "set": "--:--",
        },
        "planets": planets_data,
    }


def compute_planner_nights(
    obs_code: str,
    start_date_str: str,
    end_date_str: str,
    target_ids: list[str],
    min_alt: float = 30.0,
    station_lookup: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute multi-night astronomical windows and default max-min fair schedules."""
    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)
    if end_d < start_d:
        raise ValueError("结束日期不能早于开始日期")
    span_days = (end_d - start_d).days + 1
    if span_days > 14:
        raise ValueError("单次最多支持连续 14 晚的观测纲要排程")

    resolved_targets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for tid in target_ids:
        if tid in seen_ids:
            continue
        meta = resolve_target_meta(tid)
        if meta is not None:
            resolved_targets.append(meta)
            seen_ids.add(tid)
    if not resolved_targets:
        raise ValueError("请至少选择一个观测目标")

    base_station = station_lookup(obs_code) if station_lookup else {
        "code": obs_code,
        "name": f"测站 {obs_code}",
        "lat": 25.029,
        "lon": 102.797,
        "alt": 2000.0,
    }
    profile = STATION_PROFILES.get(base_station.get("code", obs_code), {})
    station_info = {
        "code": base_station.get("code", obs_code),
        "name": profile.get("name") or base_station.get("name") or f"测站 {obs_code}",
        "short_name": profile.get("short_name") or base_station.get("name") or obs_code,
        "lat": round(float(base_station.get("lat", 0.0)), 4),
        "lon": round(float(base_station.get("lon", 0.0)), 4),
        "alt": round(float(base_station.get("alt", 0.0)), 1),
        "aperture_cm": profile.get("aperture_cm", "--"),
        "focal_length": profile.get("focal_length", "--"),
        "ccd": profile.get("ccd", "--"),
        "fov_arcmin": profile.get("fov_arcmin", "--"),
        "pixel_scale": profile.get("pixel_scale", "--"),
        "standard_filter": profile.get("standard_filter", "I"),
        "notes": profile.get("notes", ""),
    }

    planet_keys: list[str] = []
    for t_meta in resolved_targets:
        p = t_meta["planet"]
        if p not in planet_keys:
            planet_keys.append(p)

    nights: list[dict[str, Any]] = []
    for offset in range(span_days):
        curr_date = start_d + timedelta(days=offset)
        if HAS_ASTROPY and station_info["code"] != "500":
            night_data = _compute_single_night_astropy(curr_date, station_info, planet_keys, min_alt)
        else:
            night_data = _compute_single_night_fallback(curr_date, planet_keys)

        target_windows = []
        for t_meta in resolved_targets:
            p_info = night_data["planets"][t_meta["planet"]]
            target_windows.append(
                {
                    "target_id": t_meta["id"],
                    "code": t_meta["code"],
                    "short": t_meta["short"],
                    "name": t_meta["name"],
                    "planet": t_meta["planet"],
                    "planet_zh": t_meta["planet_zh"],
                    "color": t_meta["color"],
                    "order": t_meta["order"],
                    "valid": p_info["valid"],
                    "win_start": p_info["win_start"],
                    "win_end": p_info["win_end"],
                    "win_start_time": p_info["win_start_time"],
                    "win_end_time": p_info["win_end_time"],
                    "transit_min": p_info["transit_min"],
                    "transit_time": p_info["transit_time"],
                    "max_alt": p_info["max_alt"],
                    "zenith_warning": p_info["zenith_warning"],
                }
            )

        blocks = auto_allocate_schedule(
            night_data["twilight"]["dusk_min"],
            night_data["twilight"]["dawn_min"],
            target_windows,
            step=SAMPLE_STEP_MIN,
        )
        night_data["target_windows"] = target_windows
        night_data["schedule"] = blocks
        nights.append(night_data)

    exposure_list = [_resolve_exposure_rule(t_meta) for t_meta in resolved_targets]

    return {
        "success": True,
        "station": station_info,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "min_alt": min_alt,
        "sample_step_min": SAMPLE_STEP_MIN,
        "selected_targets": resolved_targets,
        "nights": nights,
        "exposure_rules": exposure_list,
        "checklist": FIELD_CHECKLIST,
    }


# ---------- Word (.docx) Export Helpers (lazy-loaded) ----------

_DOCX_LOADED = False


def _ensure_docx_loaded() -> None:
    global _DOCX_LOADED
    if _DOCX_LOADED:
        return
    try:
        import docx as _docx
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT as _WD_CVA
        from docx.enum.table import WD_TABLE_ALIGNMENT as _WD_TA
        from docx.enum.text import WD_ALIGN_PARAGRAPH as _WD_AP
        from docx.oxml import OxmlElement as _OxmlElement
        from docx.oxml import parse_xml as _parse_xml
        from docx.oxml.ns import nsdecls as _nsdecls
        from docx.oxml.ns import qn as _qn
        from docx.shared import Cm as _Cm
        from docx.shared import Pt as _Pt
        from docx.shared import RGBColor as _RGBColor
    except ImportError as exc:
        raise RuntimeError("后端未安装 python-docx，无法导出 Word 文档") from exc

    g = globals()
    g.update(
        {
            "docx": _docx,
            "WD_CELL_VERTICAL_ALIGNMENT": _WD_CVA,
            "WD_TABLE_ALIGNMENT": _WD_TA,
            "WD_ALIGN_PARAGRAPH": _WD_AP,
            "OxmlElement": _OxmlElement,
            "parse_xml": _parse_xml,
            "nsdecls": _nsdecls,
            "qn": _qn,
            "Cm": _Cm,
            "Pt": _Pt,
            "RGBColor": _RGBColor,
        }
    )
    _DOCX_LOADED = True


def _set_cell_shading(cell: Any, fill_hex: str) -> None:
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _set_cell_margins(cell: Any, top: int = 60, bottom: int = 60, left: int = 90, right: int = 90) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = OxmlElement("w:tcMar")
    for side, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")
        tc_mar.append(node)
    tc_pr.append(tc_mar)


def _set_run_font(run: Any, size_pt: float, bold: bool = False, color_rgb: tuple[int, int, int] | None = None) -> None:
    run.bold = bold
    run.font.size = Pt(size_pt)
    run.font.name = "Microsoft YaHei"
    r_pr = run._r.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    r_fonts.set(qn("w:eastAsia"), "微软雅黑")
    r_fonts.set(qn("w:ascii"), "Calibri")
    r_fonts.set(qn("w:hAnsi"), "Calibri")
    if color_rgb is not None:
        run.font.color.rgb = RGBColor(*color_rgb)


def _add_section_heading(doc: Any, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(5)
    p.paragraph_format.keep_with_next = True
    r = p.add_run(text)
    _set_run_font(r, 12.5, bold=True, color_rgb=(30, 64, 175))


def _add_styled_table(
    doc: Any,
    headers: list[str],
    rows: list[list[str]],
    col_widths_cm: list[float],
    font_size: float = 8.5,
    left_align_cols: set[int] | None = None,
) -> Any:
    left_align_cols = left_align_cols or set()
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Header row
    hdr_row = table.rows[0]
    tr_pr = hdr_row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:tblHeader"))
    for j, title in enumerate(headers):
        cell = hdr_row.cells[j]
        cell.width = Cm(col_widths_cm[j])
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _set_cell_shading(cell, "E0EAFF")
        _set_cell_margins(cell, top=70, bottom=70, left=80, right=80)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(title)
        _set_run_font(r, font_size, bold=True, color_rgb=(30, 41, 59))

    for i, row_vals in enumerate(rows):
        row = table.rows[i + 1]
        bg = "F8FAFC" if i % 2 == 1 else None
        for j, val in enumerate(row_vals):
            cell = row.cells[j]
            cell.width = Cm(col_widths_cm[j])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if bg:
                _set_cell_shading(cell, bg)
            _set_cell_margins(cell, top=55, bottom=55, left=80, right=80)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT if j in left_align_cols else WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(str(val))
            _set_run_font(r, font_size, bold=False, color_rgb=(15, 23, 42))

    return table


def generate_outline_docx(payload: dict[str, Any]) -> io.BytesIO:
    """Render a publication-grade Word (.docx) observation outline in memory."""
    _ensure_docx_loaded()

    station = payload.get("station") or {}
    start_date = str(payload.get("start_date", ""))
    end_date = str(payload.get("end_date", ""))
    observer = str(payload.get("observer") or "待填写").strip()
    assistant = str(payload.get("assistant") or "待填写").strip()
    min_alt = payload.get("min_alt", 30)
    custom_note = str(payload.get("custom_note") or "").strip()
    include_checklist = bool(payload.get("include_checklist", True))
    nights = payload.get("nights") or []
    selected_targets = payload.get("selected_targets") or []
    exposure_rules = payload.get("exposure_rules") or []

    doc = docx.Document()
    for sec in doc.sections:
        sec.top_margin = Cm(1.8)
        sec.bottom_margin = Cm(1.8)
        sec.left_margin = Cm(1.6)
        sec.right_margin = Cm(1.6)

    # 1. Title & Metadata Header
    short_name = station.get("short_name") or station.get("code") or "测站"
    title_date = start_date if start_date == end_date else f"{start_date} 至 {end_date}"
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_title.paragraph_format.space_after = Pt(4)
    r_title = p_title.add_run(f"{title_date} {short_name} 天然卫星观测纲要及记录")
    _set_run_font(r_title, 16, bold=True, color_rgb=(15, 23, 42))

    p_sub = doc.add_paragraph()
    p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_sub.paragraph_format.space_after = Pt(10)
    r_sub = p_sub.add_run(
        f"测站：{station.get('name', short_name)} (MPC {station.get('code', '')})  |  "
        f"编制日期：{datetime.now(BJT).strftime('%Y-%m-%d')}  |  最低仰角阈值：≥{min_alt}°"
    )
    _set_run_font(r_sub, 9.5, color_rgb=(71, 85, 105))

    target_summary = "、".join(t.get("short") or t.get("id", "") for t in selected_targets) or "未指定"
    meta_rows = [
        ["观测日期", title_date, "观测夜数", f"共 {len(nights)} 晚"],
        ["观测人员", observer, "观测助手", assistant],
        ["选定目标", target_summary, "标准滤光片", str(station.get("standard_filter", "I"))],
        [
            "测站坐标",
            f"东经 {station.get('lon', '--')}°，北纬 {station.get('lat', '--')}°，海拔 {station.get('alt', '--')}m",
            "望远镜/视场",
            f"{station.get('aperture_cm', '--')} / {station.get('fov_arcmin', '--')}",
        ],
    ]
    if custom_note:
        meta_rows.append(["特别备注", custom_note, "平太阳时差", f"{nights[0]['twilight'].get('solar_offset_min', 0):+d} 分钟" if nights else "--"])
    _add_styled_table(
        doc,
        ["项目", "内容", "项目", "内容"],
        meta_rows,
        [2.6, 6.4, 2.6, 6.2],
        font_size=8.8,
        left_align_cols={1, 3},
    )

    # 2. Table 1: Station & Nighttime Twilight / Moon Windows
    _add_section_heading(doc, "一、逐夜暗夜窗口与月相条件（表 1）")
    if station.get("notes"):
        p_note = doc.add_paragraph()
        p_note.paragraph_format.space_after = Pt(4)
        r_n = p_note.add_run(f"设备说明：{station['notes']}")
        _set_run_font(r_n, 8.8, color_rgb=(71, 85, 105))

    t1_rows = []
    for n in nights:
        tw = n.get("twilight", {})
        moon = n.get("moon", {})
        moon_str = (
            f"{moon.get('illum_pct', '--')}% ({moon.get('night_start', '--')}~{moon.get('night_end', '--')})"
            if moon.get("in_night")
            else f"{moon.get('illum_pct', '--')}% (暗夜无月)"
        )
        t1_rows.append(
            [
                f"{n.get('date', '')} ({n.get('weekday', '')})",
                f"{tw.get('sunset', '--')} / {tw.get('sunrise', '--')}",
                f"{tw.get('dusk', '--')} ~ {tw.get('dawn', '--')}",
                f"{tw.get('astro_dusk', '--')} ~ {tw.get('astro_dawn', '--')}",
                format_duration_zh(tw.get("dark_duration_min", 0)),
                moon_str,
            ]
        )
    _add_styled_table(
        doc,
        ["观测夜（BJT）", "日落 / 日出", "航海暗夜 (-12°)", "天文暗夜 (-18°)", "可用暗夜", "月相与暗夜月光"],
        t1_rows,
        [3.1, 2.6, 3.1, 3.1, 2.0, 3.9],
        font_size=8.3,
    )

    # 3. Table 2: Planetary Ephemeris & Valid Windows
    _add_section_heading(doc, f"二、所选目标所属行星升中落与 ≥{min_alt}° 窗口（表 2）")
    t2_rows = []
    for n in nights:
        planets = n.get("planets") or {}
        for p_key, p_info in planets.items():
            win_str = (
                f"{p_info.get('win_start_time', '--')} ~ {p_info.get('win_end_time', '--')}"
                if p_info.get("valid")
                else "无有效窗口"
            )
            warn = "近天顶(>85°)" if p_info.get("zenith_warning") else "正常"
            t2_rows.append(
                [
                    n.get("date", ""),
                    f"{p_info.get('name_zh', p_key)}",
                    p_info.get("rise_time", "--"),
                    f"{p_info.get('transit_time', '--')} ({p_info.get('max_alt', '--')}°)",
                    p_info.get("set_time", "--"),
                    win_str,
                    warn,
                ]
            )
    _add_styled_table(
        doc,
        ["观测夜", "大行星", "升起", "中天（最大仰角）", "落下", f"暗夜 ≥{min_alt}° 窗口", "状态提示"],
        t2_rows,
        [2.4, 2.0, 1.8, 3.3, 1.8, 3.7, 2.8],
        font_size=8.3,
    )

    # 4. Table 3: Final Interactive Nightly Schedule
    _add_section_heading(doc, "三、逐夜精细观测排程表（表 3）")
    t3_rows = []
    for n in nights:
        schedule = n.get("schedule") or []
        if not schedule:
            t3_rows.append([n.get("date", ""), "--", "--", "无排程", "--", "--"])
            continue
        for idx, blk in enumerate(schedule, start=1):
            label = blk.get("name") or blk.get("label") or blk.get("target_id") or "机动/留白"
            if blk.get("kind") == "buffer":
                label = f"[机动/留白] {blk.get('label', '预留时间')}"
            alt_text = blk.get("alt_summary") or "--"
            warn_flag = " [越界预警]" if blk.get("out_of_window") else ""
            note = (blk.get("note") or "") + warn_flag
            t3_rows.append(
                [
                    n.get("date", "") if idx == 1 else "",
                    f"{blk.get('start_time', '--')} ~ {blk.get('end_time', '--')}",
                    format_duration_zh(blk.get("duration_min", 0)),
                    label,
                    alt_text,
                    note.strip() or "--",
                ]
            )
    _add_styled_table(
        doc,
        ["观测夜", "排程时段（BJT）", "时长", "观测目标 / 事项", "时段高度角", "备注"],
        t3_rows,
        [2.4, 3.1, 1.8, 4.4, 2.8, 3.3],
        font_size=8.3,
        left_align_cols={3, 5},
    )

    # 5. Table 4: Exposure Rules
    _add_section_heading(doc, "四、实测曝光参数与指向建议（表 4）")
    t4_rows = []
    for r in exposure_rules:
        t4_rows.append(
            [
                r.get("code", ""),
                r.get("target", ""),
                r.get("pointing", ""),
                f"{r.get('filter', 'I')} / {r.get('suggested_exposure_s', '--')}s",
                str(r.get("suggested_frames", "--")),
                str(r.get("snr_baseline", "--")),
                r.get("notes", ""),
            ]
        )
    if t4_rows:
        _add_styled_table(
            doc,
            ["代号", "目标卫星", "指向方式", "滤光片/曝光", "建议张数", "参考SNR", "实测要点与避坑"],
            t4_rows,
            [1.5, 2.7, 2.7, 2.1, 1.6, 2.0, 5.2],
            font_size=8.0,
            left_align_cols={1, 2, 6},
        )

    # 6. Section 5: Field Checklist
    if include_checklist:
        _add_section_heading(doc, "五、一线实操与应急速查清单")
        for idx, item in enumerate(FIELD_CHECKLIST, start=1):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            r1 = p.add_run(f"{idx}. {item['title']}：")
            _set_run_font(r1, 9.0, bold=True, color_rgb=(30, 41, 59))
            r2 = p.add_run(item["detail"])
            _set_run_font(r2, 9.0, color_rgb=(51, 65, 85))

    # 7. Table 5: Per-night Printable Observation Log Sheets
    for n in nights:
        doc.add_page_break()
        _add_section_heading(
            doc,
            f"观测记录表：{n.get('date', '')} ({n.get('weekday', '')}) — {short_name}",
        )
        tw = n.get("twilight", {})
        p_meta = doc.add_paragraph()
        p_meta.paragraph_format.space_after = Pt(5)
        r_m = p_meta.add_run(
            f"观测人：{observer}    助手：{assistant}    "
            f"暗夜(-12°)：{tw.get('dusk', '--')}~{tw.get('dawn', '--')}    "
            f"天气/视宁度：__________    时钟偏差(初始/中段/结束)：____ / ____ / ____ s"
        )
        _set_run_font(r_m, 8.8, color_rgb=(51, 65, 85))

        log_rows = []
        for blk in n.get("schedule") or []:
            if blk.get("kind") == "buffer":
                log_rows.append(
                    [
                        f"{blk.get('start_time', '')}~{blk.get('end_time', '')}",
                        f"[机动] {blk.get('label', '预留')}",
                        "--",
                        "--",
                        "--",
                        "",
                        blk.get("note", ""),
                    ]
                )
            else:
                tid = blk.get("target_id", "")
                exp = EXPOSURE_RULES.get(tid, {})
                log_rows.append(
                    [
                        f"{blk.get('start_time', '')}~{blk.get('end_time', '')}",
                        blk.get("label") or tid,
                        exp.get("filter", "I"),
                        f"{exp.get('suggested_exposure_s', '')}s" if exp else "",
                        str(exp.get("suggested_frames", "")),
                        "",
                        "",
                    ]
                )
        # Add 3 extra blank rows for ad-hoc notes/re-takes
        for _ in range(3):
            log_rows.append(["", "", "", "", "", "", ""])

        _add_styled_table(
            doc,
            ["计划/实测时段", "目标代号", "滤光片", "曝光(s)", "实拍张数", "高度角/SNR", "天气 / 结霜 / 文件名备注"],
            log_rows,
            [2.8, 2.3, 1.4, 1.7, 1.8, 2.3, 5.5],
            font_size=8.5,
            left_align_cols={1, 6},
        )

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
