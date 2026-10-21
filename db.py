"""SQLite persistence + milestone state machine for SpecPay (stdlib only)."""
import os
import sqlite3
import time
import uuid
from contextlib import closing

DB_PATH = os.getenv("SPECPAY_DB", "specpay.db")

# draft -> awaiting_payment -> funded -> submitted -> approved -> released
TRANSITIONS = {
    "draft": {"awaiting_payment", "voided"},
    "awaiting_payment": {"funded", "voided"},
    "funded": {"submitted", "voided"},
    "submitted": {"approved", "funded", "voided"},  # "funded" again = review rejected, resubmit
    "approved": {"released", "voided"},
    "released": set(),
    "voided": set(),
}
ALLOWED_FIELDS = {"order_id", "auth_id", "capture_id"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS milestones (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  title TEXT NOT NULL,
  criteria TEXT,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  status TEXT NOT NULL DEFAULT 'draft',
  order_id TEXT,
  auth_id TEXT,
  capture_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  milestone_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  note TEXT,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY,
  event_type TEXT,
  received_at REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def create_milestone(project, title, amount, currency="USD", criteria=None) -> dict:
    mid = uuid.uuid4().hex[:12]
    now = time.time()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO milestones (id, project, title, criteria, amount, currency, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (mid, project, title, criteria, amount, currency, now, now),
        )
        conn.execute(
            "INSERT INTO history (milestone_id, from_status, to_status, note, at) VALUES (?, NULL, 'draft', 'created', ?)",
            (mid, now),
        )
        conn.commit()
    return get(mid)


def get(mid: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM milestones WHERE id = ?", (mid,)).fetchone()
    return dict(row) if row else None


def list_milestones(project: str | None = None) -> list[dict]:
    with closing(_connect()) as conn:
        if project:
            rows = conn.execute(
                "SELECT * FROM milestones WHERE project = ? ORDER BY created_at", (project,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM milestones ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def history(mid: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT from_status, to_status, note, at FROM history WHERE milestone_id = ? ORDER BY id", (mid,)
        ).fetchall()
    return [dict(r) for r in rows]


def find_by(order_id: str | None = None, auth_id: str | None = None) -> dict | None:
    """Find a milestone by PayPal order id or authorization id (used by webhooks)."""
    with closing(_connect()) as conn:
        if order_id:
            row = conn.execute("SELECT * FROM milestones WHERE order_id = ?", (order_id,)).fetchone()
            if row:
                return dict(row)
        if auth_id:
            row = conn.execute("SELECT * FROM milestones WHERE auth_id = ?", (auth_id,)).fetchone()
            if row:
                return dict(row)
    return None


def can_transition(current: str, new: str) -> bool:
    return current == new or new in TRANSITIONS.get(current, set())


def transition(mid: str, new_status: str, note: str | None = None, **fields) -> dict:
    """Move a milestone to new_status (validated) and optionally store PayPal ids."""
    bad = set(fields) - ALLOWED_FIELDS
    if bad:
        raise ValueError(f"Unknown fields: {bad}")
    with closing(_connect()) as conn:
        row = conn.execute("SELECT status FROM milestones WHERE id = ?", (mid,)).fetchone()
        if row is None:
            raise KeyError(mid)
        current = row["status"]
        if not can_transition(current, new_status):
            raise ValueError(f"Illegal transition {current} -> {new_status}")
        now = time.time()
        sets = ["status = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
        conn.execute(
            f"UPDATE milestones SET {', '.join(sets)} WHERE id = ?",
            [new_status, now, *fields.values(), mid],
        )
        if current != new_status:
            conn.execute(
                "INSERT INTO history (milestone_id, from_status, to_status, note, at) VALUES (?, ?, ?, ?, ?)",
                (mid, current, new_status, note, now),
            )
        conn.commit()
    return get(mid)


def mark_event(event_id: str, event_type: str) -> bool:
    """Record a webhook event id. True if new, False if already processed."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO webhook_events (id, event_type, received_at) VALUES (?, ?, ?)",
            (event_id, event_type, time.time()),
        )
        conn.commit()
        return cur.rowcount == 1


def reset() -> None:
    """Demo helper: delete all milestones and their history (webhook event ids are kept)."""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM history")
        conn.execute("DELETE FROM milestones")
        conn.commit()
