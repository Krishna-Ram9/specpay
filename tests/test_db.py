import sqlite3

import pytest

PATH_TO = {
    "draft": [], "awaiting_payment": ["awaiting_payment"],
    "funded": ["awaiting_payment", "funded"], "submitted": ["awaiting_payment", "funded", "submitted"],
    "approved": ["awaiting_payment", "funded", "submitted", "approved"],
    "released": ["awaiting_payment", "funded", "submitted", "approved", "released"], "voided": ["voided"],
}


def make(db, status="draft"):
    m = db.create_milestone("P", "T", "10.00", "USD", "criteria")
    for step in PATH_TO[status]:
        db.transition(m["id"], step)
    return m["id"]


def test_happy_path_is_recorded_in_history(fresh_db):
    mid = make(fresh_db, "released")
    assert fresh_db.get(mid)["status"] == "released"
    assert [h["to_status"] for h in fresh_db.history(mid)] == ["draft", "awaiting_payment", "funded", "submitted", "approved", "released"]


def test_rejected_review_returns_to_funded(fresh_db):
    mid = make(fresh_db, "submitted")
    assert fresh_db.transition(mid, "funded")["status"] == "funded"


@pytest.mark.parametrize("start,target", [
    ("draft", "funded"), ("draft", "released"), ("awaiting_payment", "released"), ("funded", "approved"),
    ("funded", "released"), ("submitted", "released"), ("approved", "funded"), ("released", "voided"),
    ("released", "funded"), ("voided", "funded"),
])
def test_illegal_transitions_are_rejected(fresh_db, start, target):
    mid = make(fresh_db, start)
    with pytest.raises(ValueError):
        fresh_db.transition(mid, target)
    assert fresh_db.get(mid)["status"] == start


def test_same_status_adds_no_history_row(fresh_db):
    mid = make(fresh_db, "funded")
    before = len(fresh_db.history(mid))
    fresh_db.transition(mid, "funded")
    assert len(fresh_db.history(mid)) == before


def test_unknown_field_and_unknown_milestone(fresh_db):
    mid = make(fresh_db)
    with pytest.raises(ValueError):
        fresh_db.transition(mid, "awaiting_payment", evil="x")
    with pytest.raises(KeyError):
        fresh_db.transition("nope", "voided")


def test_paypal_ids_are_stored_and_findable(fresh_db):
    mid = make(fresh_db)
    fresh_db.transition(mid, "awaiting_payment", order_id="ORD1")
    fresh_db.transition(mid, "funded", auth_id="AUTH1")
    assert fresh_db.find_by(order_id="ORD1")["id"] == mid
    assert fresh_db.find_by(auth_id="AUTH1")["id"] == mid
    assert fresh_db.find_by(order_id="missing") is None


def test_webhook_events_are_remembered(fresh_db):
    assert not fresh_db.event_seen("EV1")
    assert fresh_db.mark_event("EV1", "X") is True
    assert fresh_db.mark_event("EV1", "X") is False
    assert fresh_db.event_seen("EV1")


def test_reviews_round_trip_and_latest_wins(fresh_db):
    mid = make(fresh_db, "funded")
    facts = [{"kind": "dxf", "file_name": "a.dxf", "summary": "s"}]
    fresh_db.add_review(mid, {"verdict": "fail", "summary": "one", "checks": [{"criterion": "c", "met": False, "evidence": "e"}], "missing": ["c"]})
    fresh_db.add_review(mid, {"verdict": "pass", "summary": "two", "checks": [], "missing": [], "file_facts": facts})
    last = fresh_db.latest_reviews()[mid]
    assert last["verdict"] == "pass" and last["summary"] == "two" and last["file_facts"] == facts
    assert len(fresh_db.review_history(mid)) == 2
    assert fresh_db.review_history(mid)[0]["checks"][0]["criterion"] == "c"


def test_reset_clears_everything_but_keeps_event_ids(fresh_db):
    mid = make(fresh_db, "funded")
    fresh_db.add_review(mid, {"verdict": "fail", "summary": "", "checks": [], "missing": []})
    fresh_db.mark_event("EV9", "X")
    fresh_db.reset()
    assert fresh_db.list_milestones() == [] and fresh_db.latest_reviews() == {}
    assert fresh_db.event_seen("EV9")


def test_old_database_without_file_facts_is_upgraded(tmp_path, monkeypatch):
    import db
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, milestone_id TEXT NOT NULL, verdict TEXT NOT NULL, summary TEXT, checks TEXT, missing TEXT, at REAL NOT NULL)")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_PATH", str(old))
    db.init_db()
    cols = [r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(reviews)")]
    assert "file_facts" in cols
