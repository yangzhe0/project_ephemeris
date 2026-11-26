"""Tests for the observation outline planner routes, scheduler, and Word export."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import docx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app as app_module
from planner_engine import auto_allocate_schedule


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


def test_auto_allocate_schedule_balanced_and_non_overlapping():
    windows = [
        {"target_id": "N1", "planet": "neptune", "valid": True, "win_start": 460, "win_end": 820, "transit_min": 600, "order": 1},
        {"target_id": "S1-S7", "planet": "saturn", "valid": True, "win_start": 460, "win_end": 860, "transit_min": 640, "order": 3},
        {"target_id": "U1-U5", "planet": "uranus", "valid": True, "win_start": 600, "win_end": 1060, "transit_min": 840, "order": 6},
        {"target_id": "J6", "planet": "jupiter", "valid": True, "win_start": 900, "win_end": 1080, "transit_min": 1020, "order": 7},
    ]
    blocks = auto_allocate_schedule(460, 1080, windows, step=5)
    assert len(blocks) == 4

    # Non-overlapping, ordered, and strictly inside each target window
    win_map = {w["target_id"]: w for w in windows}
    for idx, blk in enumerate(blocks):
        w = win_map[blk["target_id"]]
        assert blk["start_min"] >= w["win_start"]
        assert blk["end_min"] <= w["win_end"]
        assert blk["duration_min"] >= 100
        if idx > 0:
            assert blk["start_min"] >= blocks[idx - 1]["end_min"]


def test_planner_page_renders(client):
    resp = client.get("/planner")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "观测纲要与多夜排程工作台" in html
    assert "O44" in html
    assert "extraTargetSelect" in html
    assert "5001 - J1" in html
    assert "10005 - J10" in html
    assert "10007 - J12" in html
    assert "/api/planner/windows" in html
    assert "/api/planner/export" in html


def test_extra_and_custom_targets_schedule(client):
    resp = client.post(
        "/api/planner/windows",
        json={
            "observatory": "286",
            "start_date": "2026-11-22",
            "end_date": "2026-11-22",
            "targets": ["J1", "J4", "J10", "J12", "custom:jupiter:J14"],
            "min_alt": 30,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert len(data["selected_targets"]) == 5
    assert len(data["exposure_rules"]) == 5
    sched_ids = {b["target_id"] for b in data["nights"][0]["schedule"]}
    assert {"J1", "J4", "J10", "J12", "custom:jupiter:J14"}.issubset(sched_ids)


def test_lijiang_o07_windows_and_schedule(client):
    resp = client.post(
        "/api/planner/windows",
        json={
            "observatory": "O07",
            "start_date": "2026-11-22",
            "end_date": "2026-11-22",
            "targets": ["N1", "S1-S7", "U1-U5", "J6"],
            "min_alt": 30,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["station"]["code"] == "O44"
    assert 99.0 <= data["station"]["lon"] <= 101.0
    assert 26.0 <= data["station"]["lat"] <= 27.5
    assert len(data["nights"][0]["schedule"]) >= 3


def test_planner_windows_and_export_roundtrip(client):
    resp = client.post(
        "/api/planner/windows",
        json={
            "observatory": "286",
            "start_date": "2026-11-22",
            "end_date": "2026-11-24",
            "targets": ["N1", "S1-S7", "U1-U5", "J6"],
            "min_alt": 30,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert len(data["nights"]) == 3
    assert len(data["exposure_rules"]) == 4
    assert data["station"]["code"] == "286"

    # Add a buffer block to night 0 to simulate user manual adjustment
    night0 = data["nights"][0]
    night0["schedule"].insert(
        0,
        {
            "kind": "buffer",
            "target_id": "BUFFER",
            "label": "前半夜机动预留",
            "name": "前半夜机动预留",
            "start_min": night0["twilight"]["dusk_min"],
            "end_min": night0["twilight"]["dusk_min"] + 60,
            "start_time": night0["twilight"]["dusk"],
            "end_time": "20:40",
            "duration_min": 60,
            "alt_summary": "--",
            "note": "预留 1 小时",
        },
    )

    export_resp = client.post(
        "/api/planner/export",
        json={
            "station": data["station"],
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "min_alt": data["min_alt"],
            "observer": "杨哲",
            "assistant": "张恒",
            "custom_note": "本地测试验证",
            "include_checklist": True,
            "selected_targets": data["selected_targets"],
            "exposure_rules": data["exposure_rules"],
            "nights": data["nights"],
        },
    )
    assert export_resp.status_code == 200
    assert "wordprocessingml.document" in export_resp.content_type

    parsed_doc = docx.Document(io.BytesIO(export_resp.data))
    full_text = "\n".join(p.text for p in parsed_doc.paragraphs)
    assert "2026-11-22 至 2026-11-24 昆明1m 天然卫星观测纲要及记录" in full_text
    assert "一线实操与应急速查清单" in full_text
    # Meta table + Table 1~4 + 3 nightly log sheets = 8 tables
    assert len(parsed_doc.tables) == 8
