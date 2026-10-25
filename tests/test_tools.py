"""Tool contract, verification, approval gate, idempotency, write side effects (seeded SQLite, no API)."""
import pytest

from conftest import approve, verify
from skydesk import db, seed, tools as T

WRITES = {
    "rebook_flight": dict(booking_ref="CLSCHG", new_flight_id="SD300-20261106"),
    "issue_refund": dict(booking_ref="FLXRFD", amount=290.4),
    "issue_voucher": dict(booking_ref="DLY3H2", amount=100, kind="travel_credit"),
    "waive_fee": dict(booking_ref="CLSCHG", fee_type="change_fee"),
    "update_contact": dict(customer_id="C001", email="new@example.com"),
    "cancel_booking": dict(booking_ref="CLSCXL"),
}


def rows(sql, *a):
    c = db.connect()
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def test_every_tool_returns_contract_shape_and_has_schema(tdb):
    for name, t in T.TOOLS.items():
        assert t["schema"]["function"]["name"] == name and t["kind"] in ("read", "write", "handoff")
    r = T.lookup_booking("ZZZZZZ")
    assert set(r) == {"ok", "data", "error", "retryable"} and not r["ok"] and r["error"].startswith("NOT_FOUND")
    for name in T.WRITE_TOOLS:
        req = T.TOOLS[name]["schema"]["function"]["parameters"]["required"]
        assert {"approval_id", "idempotency_key"} <= set(req)


@pytest.mark.parametrize("name", list(WRITES))
def test_write_tools_refuse_without_approval(tdb, name):
    verify("CLSCHG"), verify("FLXRFD"), verify("DLY3H2"), verify("CLSCXL")
    r = T.TOOLS[name]["fn"](**WRITES[name], approval_id="", idempotency_key="k1")
    assert not r["ok"] and r["error"].startswith("APPROVAL_REQUIRED")
    assert rows("SELECT COUNT(*) FROM actions")[0][0] == 0


@pytest.mark.parametrize("decision", [None, False])
def test_pending_or_rejected_approval_is_not_enough(tdb, decision):
    verify("FLXRFD")
    aid = T.request_approval("issue_refund", "FLXRFD", {"amount": 290.4})["data"]["approval_id"]
    if decision is False:
        T.decide_approval(aid, False, "supervisor")
    r = T.issue_refund("FLXRFD", 290.4, aid, "k1")
    assert r["error"].startswith("APPROVAL_NOT_GRANTED")


def test_approval_for_other_action_or_booking_or_amount_rejected(tdb):
    verify("FLXRFD")
    assert T.issue_refund("FLXRFD", 290.4, approve("cancel_booking", "FLXRFD"), "k1")["error"].startswith("APPROVAL_MISMATCH")
    assert T.issue_refund("FLXRFD", 290.4, approve("issue_refund", "BUSFUL"), "k2")["error"].startswith("APPROVAL_MISMATCH")
    assert T.issue_refund("FLXRFD", 290.4, approve("issue_refund", "FLXRFD", {"amount": 100}), "k3")["error"].startswith("APPROVAL_MISMATCH")


def test_refund_happy_path_then_idempotent_replay(tdb):
    verify("FLXRFD")
    aid = approve("issue_refund", "FLXRFD", {"amount": 290.4, "expected": {"refund_to_original_payment": 290.4}})
    r = T.issue_refund("FLXRFD", 290.4, aid, "refund-FLXRFD-1")
    assert r["ok"] and r["data"]["refunded_to_original_payment"] == 290.4 and r["data"]["travel_credit"] == 120.0
    assert r["data"]["travel_credit_code"].startswith("TC-")
    again = T.issue_refund("FLXRFD", 290.4, aid, "refund-FLXRFD-1")
    assert again["ok"] and again["data"]["duplicate"]
    assert len(rows("SELECT * FROM payments WHERE ref='FLXRFD' AND kind='refund'")) == 1
    assert rows("SELECT status FROM bookings WHERE ref='FLXRFD'")[0][0] == "refunded"
    # approval is single use: a new key cannot reuse it
    assert T.issue_refund("FLXRFD", 290.4, aid, "refund-FLXRFD-2")["error"].startswith("APPROVAL_NOT_GRANTED")


def test_idempotency_key_cannot_be_reused_for_another_action(tdb):
    verify("FLXRFD"), verify("CLSCXL")
    assert T.issue_refund("FLXRFD", 290.4, approve("issue_refund", "FLXRFD"), "same")["ok"]
    r = T.cancel_booking("CLSCXL", approve("cancel_booking", "CLSCXL"), "same")
    assert r["error"].startswith("IDEMPOTENCY_KEY_REUSED")


def test_refund_above_policy_amount_is_refused(tdb):
    verify("PRTRFD")
    b = db.booking(db.connect(), "PRTRFD")
    allowed = round((b["base_fare"] + b["taxes"] - 5000) / 100, 2)
    r = T.issue_refund("PRTRFD", allowed + 50, approve("issue_refund", "PRTRFD"), "k")
    assert r["error"].startswith("REFUND_AMOUNT_MISMATCH") and r["data"]["quote"]["refund_cap"] == allowed
    assert T.issue_refund("PRTRFD", allowed, approve("issue_refund", "PRTRFD"), "k2")["ok"]


def test_classic_has_no_cash_refund_but_cancel_gives_credit(tdb):
    verify("CLSCXL")
    assert T.issue_refund("CLSCXL", 1, approve("issue_refund", "CLSCXL"), "k")["error"].startswith("NO_CASH_REFUND")
    r = T.cancel_booking("CLSCXL", approve("cancel_booking", "CLSCXL"), "k2")
    assert r["ok"] and r["data"]["refunded_to_original_payment"] == 0 and r["data"]["travel_credit"] > 0


def test_price_changed_since_approval_stops_the_write(tdb):
    """Instructor ask: if the price cannot be verified, withhold it -- never execute an unapproved number."""
    verify("CLSCHG")
    fid = T.lookup_booking("CLSCHG")["data"]["alternatives"][-1]["flight_id"]
    q = T.quote_amount("CLSCHG", "change", {"new_flight_id": fid})["data"]
    aid = approve("rebook_flight", "CLSCHG", {"new_flight_id": fid,
                                              "expected": {"collect_from_customer": q["collect_from_customer"] - 10}})
    r = T.rebook_flight("CLSCHG", fid, aid, "k")
    assert r["error"].startswith("PRICE_CHANGED")
    assert rows("SELECT flight_id FROM bookings WHERE ref='CLSCHG'")[0][0] != fid


def test_rebook_moves_seat_and_charges_quote(tdb):
    verify("CLSCHG")
    b0 = db.booking(db.connect(), "CLSCHG")
    fid = T.lookup_booking("CLSCHG")["data"]["alternatives"][0]["flight_id"]
    q = T.quote_amount("CLSCHG", "change", {"new_flight_id": fid})["data"]
    seats = rows("SELECT seats_left FROM flights WHERE flight_id=?", fid)[0][0]
    r = T.rebook_flight("CLSCHG", fid, approve("rebook_flight", "CLSCHG", {"new_flight_id": fid}), "k")
    assert r["ok"] and r["data"]["charged"] == q["collect_from_customer"] and r["data"]["policy"] == "change"
    assert rows("SELECT seats_left FROM flights WHERE flight_id=?", fid)[0][0] == seats - 1
    assert rows("SELECT flight_id FROM bookings WHERE ref='CLSCHG'")[0][0] == fid != b0["flight_id"]


def test_same_day_rebook_uses_same_day_fee(tdb):
    verify("SAMEDY")
    alt = [a for a in T.lookup_booking("SAMEDY")["data"]["alternatives"] if a["departs_at"].startswith("2026-10-25")]
    r = T.rebook_flight("SAMEDY", alt[0]["flight_id"], approve("rebook_flight", "SAMEDY"), "k")
    assert r["data"]["policy"] == "same_day_change" and r["data"]["charged"] == 50.0


def test_disruption_rebook_is_free_plus_credit(tdb):
    verify("CXLCTL")
    fid = T.lookup_booking("CXLCTL")["data"]["alternatives"][0]["flight_id"]
    r = T.rebook_flight("CXLCTL", fid, approve("rebook_flight", "CXLCTL"), "k")
    assert r["data"]["charged"] == 0 and r["data"]["travel_credit"] == 250.0


def test_sold_out_flight_refused(tdb):
    verify("FLXCHG")  # AUS-DEN, same route as the seeded sell-out
    e = T.check_eligibility("FLXCHG", "change", {"new_flight_id": seed.SOLD_OUT})["data"]
    assert not e["eligible"] and any(c["rule_id"] == "FLT-002" and not c["passed"] for c in e["conditions"])
    r = T.rebook_flight("FLXCHG", seed.SOLD_OUT, approve("rebook_flight", "FLXCHG"), "k")
    assert r["error"].startswith("NOT_ELIGIBLE")


def test_write_requires_db_verification(tdb):
    r = T.issue_refund("FLXRFD", 290.4, approve("issue_refund", "FLXRFD"), "k")
    assert r["error"].startswith("NOT_VERIFIED")


def test_llm_supplied_verified_flag_is_ignored(tdb):
    e = T.check_eligibility("FLXRFD", "cancel", {"verified": True})
    assert e["ok"] and not e["data"]["eligible"] and "VER-001" in e["data"]["rule_ids"]


def test_verification_needs_card_and_locks_after_3_failures(tdb):
    b = db.booking(db.connect(), "CLSCXL")
    last = rows("SELECT last_name FROM customers WHERE customer_id=?", b["customer_id"])[0][0]
    d = T.verify_customer("CLSCXL", last)["data"]  # ref + last name alone is not enough
    assert d["verified"] is False and d["needs"] == "card_last4"
    assert not T.lookup_booking("CLSCXL")["ok"]
    for i in range(3):
        d = T.verify_customer("CLSCXL", last, "9999" if b["card_last4"] != "9999" else "8888")["data"]
    assert d["locked"] and d["attempts_left"] == 0
    r = T.verify_customer("CLSCXL", last, b["card_last4"])  # even the right answer is locked out now
    assert r["error"].startswith("LOCKED")


def test_lookup_after_verification_has_alternatives_and_clock(tdb):
    verify("SCH95M")
    d = T.lookup_booking("SCH95M")["data"]
    assert d["now"] == "2026-10-25T15:00:00+00:00" and d["alternatives"] and "card_last4" not in d


def test_list_customer_bookings(tdb):
    assert not T.list_customer_bookings("C001")["ok"]
    verify("RSK7FT")
    refs = {b["booking_ref"] for b in T.list_customer_bookings("C001")["data"]}
    assert {"RSK7FT", "CLSCHG", "DLY3H2"} <= refs


def test_quote_not_eligible_returns_reasons(tdb):
    verify("LTEXPD")
    r = T.quote_amount("LTEXPD", "change", {"new_flight_id": "SD202-20261105"})
    assert not r["ok"] and r["error"].startswith("NOT_ELIGIBLE") and not r["retryable"]
    assert "CHG-004" in r["error"] and r["data"]["eligible"] is False


def test_delay_voucher_exact_once_and_goodwill_refused(tdb):
    verify("DLY3H2")
    r = T.issue_voucher("DLY3H2", 100, "travel_credit", approve("issue_voucher", "DLY3H2"), "k1")
    assert r["ok"] and r["data"]["amount"] == 100.0
    r2 = T.issue_voucher("DLY3H2", 100, "travel_credit", approve("issue_voucher", "DLY3H2"), "k2")
    assert r2["error"].startswith("ALREADY_COMPENSATED")
    verify("DLY2H5")
    r3 = T.issue_voucher("DLY2H5", 50, "travel_credit", approve("issue_voucher", "DLY2H5"), "k3", reason="goodwill")
    assert r3["error"].startswith("NOT_ALLOWED")
    r4 = T.issue_voucher("DLY2H5", 50, "meal_voucher", approve("issue_voucher", "DLY2H5"), "k4")
    assert r4["error"].startswith("AMOUNT_MISMATCH")


def test_miles_compensation_credits_balance(tdb):
    verify("DLY3H2")
    before = rows("SELECT skymiles_balance FROM customers WHERE customer_id='C001'")[0][0]
    assert T.issue_voucher("DLY3H2", 0, "miles", approve("issue_voucher", "DLY3H2"), "k")["ok"]
    assert rows("SELECT skymiles_balance FROM customers WHERE customer_id='C001'")[0][0] == before + 7500


def test_waive_fee_once_then_change_quote_has_no_fee(tdb):
    verify("CLSCHG")
    assert T.waive_fee("CLSCHG", "change_fee", approve("waive_fee", "CLSCHG"), "k1")["ok"]
    assert T.waive_fee("CLSCHG", "change_fee", approve("waive_fee", "CLSCHG"), "k2")["error"].startswith("ALREADY_WAIVED")
    fid = T.lookup_booking("CLSCHG")["data"]["alternatives"][0]["flight_id"]
    lines = T.quote_amount("CLSCHG", "change", {"new_flight_id": fid})["data"]["lines"]
    assert lines[0]["amount"] == 0


def test_update_contact_validates_and_confirms_to_old_address(tdb):
    verify("CNTCT5")
    cid = db.booking(db.connect(), "CNTCT5")["customer_id"]
    assert T.update_contact(cid, approve("update_contact", cid), "k0", email="nope")["error"].startswith("BAD_ARGS")
    r = T.update_contact(cid, approve("update_contact", cid, {"email": "new@example.com"}), "k1", email="new@example.com")
    assert r["ok"] and r["data"]["confirmation_sent_to"].endswith(".com")
    assert rows("SELECT email FROM customers WHERE customer_id=?", cid)[0][0] == "new@example.com"


def test_escalation_ticket_and_sla(tdb):
    r = T.escalate_to_human("bereavement", "care", "Customer's mother died; trip BRVMNT", "BRVMNT")
    assert r["ok"] and r["data"]["ticket_id"] == "ESC-00001" and r["data"]["resolution_code"] == "ESC-CARE"
    assert not T.escalate_to_human("x", "nobody", "y")["ok"]


def test_call_tool_handles_bad_input(tdb):
    assert T.call_tool("nope", {})["error"].startswith("UNKNOWN_TOOL")
    assert T.call_tool("lookup_booking", {"ref": "X"})["error"].startswith("BAD_ARGS")
    assert T.call_tool("lookup_booking", "RSK7FT")["error"].startswith("BAD_ARGS")
    assert T.call_tool("search_help_center", {"query": "change fee"})["data"][0]["doc_id"] == "policy-change-fees"
    assert T.openai_tools(("read",)) and all(s["type"] == "function" for s in T.openai_tools())
