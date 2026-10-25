import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skydesk import db, faults, seed, tools  # noqa: E402

NOW = datetime.fromisoformat("2026-10-25T15:00:00+00:00")


def iso(d):
    return d.isoformat()


@pytest.fixture
def bk():
    """Booking dict factory for the pure policy tests (money in cents)."""
    def make(fare="SkyClassic", status="confirmed", dep_in_h=24 * 20, booked_h_ago=24 * 30, **kw):
        dep = NOW + timedelta(hours=dep_in_h)
        b = {"ref": "TESTAA", "customer_id": "C001", "flight_id": "SD100-X", "fare_class": fare, "status": status,
             "channel": "direct", "passengers": 1, "origin": "AUS", "dest": "DEN",
             "departs_at": iso(dep), "arrives_at": iso(dep + timedelta(hours=2)),
             "booked_at": iso(NOW - timedelta(hours=booked_h_ago)), "base_fare": 20000, "taxes": 2800,
             "ancillaries": 0, "paid_with_credit": 0, "refunded": 0, "dispute_open": 0, "delay_minutes": 0,
             "schedule_change_minutes": 0, "disruption_cause": None, "disrupted_at": None,
             "verify_failures": 0, "fee_waived": 0}
        b.update(kw)
        return b
    return make


def flight(fid="SD101-Y", dep_in_h=24 * 21, seats=10, origin="AUS", dest="DEN", lite=10000, classic=15000,
           flex=22000, business=36000):
    return {"flight_id": fid, "origin": origin, "dest": dest, "departs_at": iso(NOW + timedelta(hours=dep_in_h)),
            "seats_left": seats, "fares": {"SkyLite": lite, "SkyClassic": classic, "SkyFlex": flex,
                                           "SkyBusiness": business}}


@pytest.fixture
def tdb(tmp_path, monkeypatch):
    """A freshly seeded airline DB, no faults, fake help-center search."""
    p = tmp_path / "airline.db"
    monkeypatch.setenv("SKYDESK_DB", str(p))
    seed.build(p)
    faults.set_injector(None)
    tools.set_search_backend(lambda q: [{"doc_id": "policy-change-fees", "title": "Flight Change Fees", "text": q}])
    yield p
    faults.set_injector(None)
    tools.set_search_backend(None)


def verify(ref):
    conn = db.connect()
    b = db.booking(conn, ref)
    last = conn.execute("SELECT last_name FROM customers WHERE customer_id=?", (b["customer_id"],)).fetchone()[0]
    conn.close()
    r = tools.verify_customer(ref, last, b["card_last4"])
    assert r["data"]["verified"], r
    return b


def approve(action, target, params=None):
    aid = tools.request_approval(action, target, params or {}, "test")["data"]["approval_id"]
    assert tools.decide_approval(aid, True, "supervisor@test")["ok"]
    return aid
