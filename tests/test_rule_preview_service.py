"""Service-level tests for the read-only rule-impact preview.

The preview evaluates the stored rule and the unsaved draft against the
*current* events and planned maintenance windows and must never write to the
rules / fault_intervals / maintenance tables.
"""

import json

import pytest

from app import db
from app.services import evaluate_all_rules, preview_rule, query_faults


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "preview.db"))
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()


def _minute_series_jsonl(device="d", metric="m", hot=range(6, 17)):
    lines = []
    for m in range(31):
        value = 90 if m in hot else 10
        lines.append(json.dumps({"device": device, "metric": metric, "ts": m * 60, "value": value}))
    return "\n".join(lines)


def _insert_rule(duration):
    cur = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,0)",
        ("高温持续", "AND", duration),
    )
    rid = cur.lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,0)",
        (rid, "d", "m", ">=", 80),
    )
    return rid


@pytest.fixture()
def fresh_db(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from app.services import import_events

    import_events(_minute_series_jsonl())
    yield


def test_preview_reports_confirmation_shift_without_writing(fresh_db):
    rid = _insert_rule(300)
    evaluate_all_rules()
    faults_before = query_faults()
    assert [(f["start"], f["end"], f["confirmed_at"]) for f in faults_before] == [
        (360.0, 1020.0, 660.0)
    ]

    draft = {
        "name": "高温持续",
        "logic": "AND",
        "duration": 600,
        "conditions": [{"device": "d", "metric": "m", "op": ">=", "threshold": 80}],
    }
    result = preview_rule(rid, draft)

    # same [6,17) interval, but confirmation moves 660 -> 960
    diff = result["diff"]
    assert diff["added"] == [] and diff["removed"] == []
    assert [(s["start"], s["end"]) for s in diff["confirmation_changed"]] == [(360.0, 1020.0)]
    seg = diff["confirmation_changed"][0]
    assert seg["confirmed_at_before"] == 660.0
    assert seg["confirmed_at_after"] == 960.0

    # nothing was written: stored rule/faults are untouched
    assert db.query_one("SELECT duration FROM rules WHERE id = ?", (rid,))["duration"] == 300
    assert [(f["start"], f["end"], f["confirmed_at"]) for f in query_faults()] == [
        (360.0, 1020.0, 660.0)
    ]


def test_preview_uses_current_maintenance_and_shows_removed_fault(fresh_db):
    rid = _insert_rule(300)
    db.execute(
        "INSERT INTO maintenance_windows (device, name, start_ts, end_ts, note, created_at)"
        " VALUES (?,?,?,?,?,0)",
        ("d", "检修", 600, 720, None),
    )
    evaluate_all_rules()  # current stored state: maintenance [10,12) applied
    stored = query_faults()
    assert [(f["start"], f["end"], f["confirmed_at"]) for f in stored] == [
        (720.0, 1020.0, 1020.0)
    ]

    # draft asks for 10 minutes: the surviving [12,17) piece is only 5 min
    draft = {
        "name": "高温持续",
        "logic": "AND",
        "duration": 600,
        "conditions": [{"device": "d", "metric": "m", "op": ">=", "threshold": 80}],
    }
    result = preview_rule(rid, draft)
    assert result["maintenance_windows"] == 1
    diff = result["diff"]
    assert [(s["start"], s["end"]) for s in diff["removed"]] == [(720.0, 1020.0)]
    assert diff["added"] == [] and diff["confirmation_changed"] == []
    assert result["faults_after"] == []
    # stored faults are still there — preview did not evaluate/persist
    assert [(f["start"], f["end"]) for f in query_faults()] == [(720.0, 1020.0)]


def test_preview_new_rule_has_empty_original_side(fresh_db):
    draft = {
        "name": "新规则",
        "logic": "AND",
        "duration": 300,
        "conditions": [{"device": "d", "metric": "m", "op": ">=", "threshold": 80}],
    }
    result = preview_rule(None, draft)
    assert result["is_new_rule"] is True
    assert result["original_rule"] is None
    assert result["faults_before"] == []
    diff = result["diff"]
    assert [(s["start"], s["end"]) for s in diff["added"]] == [(360.0, 1020.0)]
    assert diff["added"][0]["confirmed_at_after"] == 660.0
    # no rule row was created
    assert db.query_one("SELECT COUNT(*) AS c FROM rules")["c"] == 0


def test_preview_unknown_rule_raises(fresh_db):
    draft = {
        "name": "x",
        "logic": "AND",
        "duration": 0,
        "conditions": [{"device": "d", "metric": "m", "op": ">", "threshold": 1}],
    }
    with pytest.raises(KeyError):
        preview_rule(999, draft)
