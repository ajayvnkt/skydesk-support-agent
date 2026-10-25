"""Mock SkyDesk airline backend in SQLite (store/airline.db; seed.py rebuilds it).

Money is integer cents; times are ISO-8601 UTC strings. The backend runs on a simulated clock
(meta.now, set by the seed) so the "booked 5 hours ago" demo bookings stay 5 hours old whenever
the demo is run.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("SKYDESK_DB", PROJECT / "store" / "airline.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, phone TEXT,
  skymiles_id TEXT, skymiles_balance INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS flights (
  flight_id TEXT PRIMARY KEY, flight_no TEXT, origin TEXT, dest TEXT, departs_at TEXT, arrives_at TEXT,
  seats_left INTEGER, fare_skylite INTEGER, fare_skyclassic INTEGER, fare_skyflex INTEGER, fare_skybusiness INTEGER);
CREATE TABLE IF NOT EXISTS bookings (
  ref TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers, flight_id TEXT REFERENCES flights,
  fare_class TEXT, status TEXT, channel TEXT DEFAULT 'direct', passengers INTEGER DEFAULT 1,
  booked_at TEXT, base_fare INTEGER, taxes INTEGER, ancillaries INTEGER DEFAULT 0,
  paid_with_credit INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0, card_last4 TEXT,
  payment_method TEXT DEFAULT 'card', dispute_open INTEGER DEFAULT 0,
  delay_minutes INTEGER DEFAULT 0, schedule_change_minutes INTEGER DEFAULT 0,
  disruption_cause TEXT, disrupted_at TEXT, verify_failures INTEGER DEFAULT 0, fee_waived INTEGER DEFAULT 0,
  scenario TEXT);
CREATE TABLE IF NOT EXISTS payments (
  payment_id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT, kind TEXT, amount INTEGER, method TEXT,
  created_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS vouchers (
  code TEXT PRIMARY KEY, customer_id TEXT, ref TEXT, kind TEXT, amount INTEGER, miles INTEGER DEFAULT 0,
  issued_at TEXT, expires_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS verifications (ref TEXT, customer_id TEXT, verified_at TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, action TEXT, target TEXT, params TEXT, summary TEXT, status TEXT,
  requested_at TEXT, decided_by TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS actions (
  action_id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE, action TEXT, target TEXT,
  approval_id TEXT, params TEXT, result TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS escalations (
  ticket_id TEXT PRIMARY KEY, team TEXT, reason TEXT, summary TEXT, ref TEXT, created_at TEXT);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("SKYDESK_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='now'").fetchone()
    if not row:
        raise RuntimeError("airline.db has no clock: run `python -m skydesk.seed`")
    return row["value"]


def set_now(conn, iso: str):
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('now', ?)", (iso,))
    conn.commit()


def flight(conn, flight_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM flights WHERE flight_id=?", (flight_id,)).fetchone()
    if not r:
        return None
    f = dict(r)
    f["fares"] = {fc: f.pop(f"fare_{fc.lower()}") for fc in ("SkyLite", "SkyClassic", "SkyFlex", "SkyBusiness")}
    return f


def booking(conn, ref: str) -> dict | None:
    """Booking joined with its flight: the dict shape policy.evaluate/quote expect."""
    r = conn.execute("SELECT * FROM bookings WHERE ref=?", (str(ref or "").strip().upper(),)).fetchone()
    if not r:
        return None
    b = dict(r)
    f = flight(conn, b["flight_id"])
    b.update({k: f[k] for k in ("flight_no", "origin", "dest", "departs_at", "arrives_at")})
    return b


def dumps(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)
