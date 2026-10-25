"""FaultInjector behaviour and deterministic seeding."""
import json

import pytest

from conftest import approve, verify
from skydesk import db, faults, seed, tools as T


@pytest.mark.parametrize("mode,ok,retryable", [("timeout", False, True), ("503", False, True),
                                               ("empty", True, False), ("malformed", True, False)])
def test_each_fault_mode(tdb, mode, ok, retryable):
    verify("RSK7FT")
    faults.set_injector({"lookup_booking": {"mode": mode, "nth": 1}})
    r = T.lookup_booking("RSK7FT")
    assert set(r) == {"ok", "data", "error", "retryable"} and r["ok"] is ok and r["retryable"] is retryable
    if mode == "empty":
        assert r["data"] is None
    if mode == "malformed":
        assert not isinstance(r["data"], dict)
    assert T.lookup_booking("RSK7FT")["data"]["booking_ref"] == "RSK7FT"  # 2nd call recovers


def test_nth_list_and_wildcard(tdb):
    faults.set_injector({"search_help_center": {"mode": "503", "nth": [2, 3]}, "*": {"mode": "timeout", "nth": 1}})
    res = [T.search_help_center("bags")["ok"] for _ in range(4)]
    assert res == [True, False, False, True]
    assert T.escalate_to_human("r", "care", "s")["error"].startswith("TIMEOUT")


def test_probability_is_seeded_and_reproducible(tdb):
    def run():
        faults.set_injector(faults.FaultInjector({"search_help_center": {"mode": "503", "p": 0.5}}, seed=3))
        return [T.search_help_center("x")["ok"] for _ in range(30)]
    a, b = run(), run()
    assert a == b and 5 < a.count(False) < 25


def test_env_spec(monkeypatch):
    monkeypatch.setenv("SKYDESK_FAULTS", json.dumps({"quote_amount": {"mode": "empty", "p": 1.0}}))
    fi = faults.FaultInjector.from_env()
    assert fi.check("quote_amount")["data"] is None and fi.check("lookup_booking") is None


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        faults.FaultInjector({"x": {"mode": "explode"}})


def test_fault_on_write_has_no_side_effect_and_retry_is_safe(tdb):
    verify("FLXRFD")
    aid = approve("issue_refund", "FLXRFD")
    faults.set_injector({"issue_refund": {"mode": "timeout", "nth": 1}})
    assert T.issue_refund("FLXRFD", 290.4, aid, "k")["retryable"]
    c = db.connect()
    assert c.execute("SELECT COUNT(*) FROM payments WHERE ref='FLXRFD' AND kind='refund'").fetchone()[0] == 0
    assert T.issue_refund("FLXRFD", 290.4, aid, "k")["ok"]
    assert T.issue_refund("FLXRFD", 290.4, aid, "k")["data"]["duplicate"]
    assert c.execute("SELECT COUNT(*) FROM payments WHERE ref='FLXRFD' AND kind='refund'").fetchone()[0] == 1


def test_seed_is_deterministic_and_well_formed(tmp_path):
    def dump(p):
        c = db.connect(p)
        return [list(map(tuple, c.execute(f"SELECT * FROM {t} ORDER BY 1"))) for t in
                ("customers", "flights", "bookings", "vouchers", "payments")]
    a, b = dump(seed.build(tmp_path / "a.db")), dump(seed.build(tmp_path / "b.db"))
    assert a == b
    customers, flights, bookings = a[0], a[1], a[2]
    assert len(customers) == 25 and len(bookings) == 45
    assert all(seed.REF_RE.match(r[0]) for r in bookings)
    statuses = {r[4] for r in bookings}
    assert {"confirmed", "airline_cancelled", "delayed", "schedule_changed", "flown", "refunded"} <= statuses
    assert {r[3] for r in bookings} == {"SkyLite", "SkyClassic", "SkyFlex", "SkyBusiness"}
