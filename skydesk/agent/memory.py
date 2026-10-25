"""Small persistent customer memory (store/memory.db). Loaded only after verification.
Never stores payment data: card-like digit runs and payment-ish keys are dropped before writing."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from rag.llm import STORE

MEMORY_DB = STORE / "memory.db"
_PAYMENT_KEY = re.compile(r"card|payment|cvv|cvc|iban|account_?number|routing|pan\b|expiry", re.I)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,19}\b")


def scrub(v):
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items() if not _PAYMENT_KEY.search(str(k))}
    if isinstance(v, list):
        return [scrub(x) for x in v]
    if isinstance(v, str):
        return _CARD_RE.sub("[removed]", v)
    return v


class CustomerMemory:
    def __init__(self, path: Path | str = MEMORY_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS prefs (customer_id TEXT PRIMARY KEY, prefs TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, customer_id TEXT, ts TEXT,"
            " booking_ref TEXT, request TEXT, outcome TEXT);")

    def load(self, customer_id: str, last: int = 5) -> dict:
        row = self.db.execute("SELECT prefs FROM prefs WHERE customer_id=?", (customer_id,)).fetchone()
        reqs = self.db.execute("SELECT ts, booking_ref, request, outcome FROM requests WHERE customer_id=? "
                               "ORDER BY id DESC LIMIT ?", (customer_id, last)).fetchall()
        return {"customer_id": customer_id, "preferences": json.loads(row[0]) if row else {},
                "past_requests": [dict(zip(("ts", "booking_ref", "request", "outcome"), r)) for r in reqs]}

    def set_prefs(self, customer_id: str, prefs: dict) -> None:
        merged = {**self.load(customer_id)["preferences"], **scrub(prefs)}
        self.db.execute("INSERT OR REPLACE INTO prefs VALUES (?, ?)", (customer_id, json.dumps(merged)))
        self.db.commit()

    def record(self, customer_id: str, booking_ref: str | None, request: str, outcome: str) -> None:
        self.db.execute("INSERT INTO requests (customer_id, ts, booking_ref, request, outcome) VALUES (?,?,?,?,?)",
                        (customer_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), booking_ref,
                         scrub(request)[:200], outcome))
        self.db.commit()
