"""verify.py: the reply never carries a number, date or condition that this run's tools didn't produce."""
from agent_fakes import VERIFY, FakeTools, ScriptedPlanner, make_agent

from skydesk.agent.verify import verify_reply

QUOTE = {"tool": "quote_amount", "ok": True, "data": {
    "currency": "USD", "lines": [{"label": "Base fare", "amount": 150.0, "rule_id": "REF-002"},
                                 {"label": "Cancellation fee $100 x 1 passenger(s)", "amount": -100.0}],
    "travel_credit": 50.0, "refund_to_original_payment": 0.0}}
ELIG = {"tool": "check_eligibility", "ok": True, "data": {
    "eligible": True, "requires_human": False, "reasons": [],
    "conditions": [{"rule_id": "CHG-001", "condition": "Change must be made at least 2 hours before departure",
                    "passed": True}],
    "exceptions_applied": []}}
BOOK = {"tool": "lookup_booking", "ok": True, "data": {"booking_ref": "SKY123", "departure": "2026-11-20T09:00:00Z"}}
FACTS = [QUOTE, ELIG, BOOK]


def test_instructor_feedback_price_check_failure_withholds_or_escalates_never_keeps_original(tmp_path):
    """Week 2 feedback: 'if a price can't be verified, withhold it or escalate' -- never keep the original."""
    # (a) price is the core of the answer -> escalate, the unverified $95 never reaches the customer
    tools = FakeTools()
    p = ScriptedPlanner([VERIFY, ("call", [("quote_amount", {"booking_ref": "SKY123", "action": "cancel"})]),
                         ("say", "If you cancel you'll get $95.00 back.")])
    r = make_agent(tmp_path, p, tools).send("t1", "how much do I get back if I cancel? Lee SKY123")
    assert r["outcome"] == "escalated" and "$95" not in r["reply"] and tools.n("escalate_to_human") == 1
    assert r["state"]["verify"]["action"] == "escalate"
    # (b) price is incidental -> the sentence is withheld, the rest is kept, agent confirmation promised
    v = verify_reply("Your booking is confirmed for Nov 20. Checked bags cost $35 each.", FACTS, "is my booking ok?")
    assert v["action"] == "withheld" and "$35" not in v["reply"] and "Nov 20" in v["reply"]
    assert "agent will confirm" in v["reply"]
    # (c) no tool result at all -> nothing numeric survives
    v = verify_reply("The change fee is $75.", [], "what's the change fee?")
    assert v["action"] == "escalate" and v["reply"] is None


def test_verified_amounts_dates_percentages_pass():
    v = verify_reply("You'd receive a travel credit of $50.00 after the $100 fee. "
                     "Your flight departs 2026-11-20.", FACTS, "how much credit?")
    assert v["action"] == "pass"


def test_failed_source_tool_results_do_not_count_as_evidence():
    bad = {"tool": "quote_amount", "ok": False, "data": {"total": 95.0}}
    assert verify_reply("You get $95.", [bad], "how much?")["action"] == "escalate"


def test_fare_condition_not_in_check_eligibility_is_removed():
    """Instructor: test fare conditions and policy exceptions directly, not just citations."""
    v = verify_reply("You can change your flight. Changes are allowed only if you are a SkyMiles Gold member.",
                     FACTS, "can I change?")
    assert v["action"] == "withheld" and "Gold" not in v["reply"]


def test_fare_condition_backed_by_check_eligibility_is_kept():
    v = verify_reply("The change must be made at least 2 hours before departure.", FACTS, "can I change?")
    assert v["action"] == "pass"


def test_wrong_deadline_is_caught():
    v = verify_reply("The change must be made at least 3 hours before departure.", FACTS, "deadline?")
    assert v["action"] != "pass" and "3 hours" not in (v["reply"] or "")


def test_eligibility_contradiction_escalates():
    inel = {"tool": "check_eligibility", "ok": True, "data": {"eligible": False, "conditions": [], "reasons": []}}
    assert verify_reply("Good news, you are eligible for a full refund.", [inel], "refund?")["action"] == "escalate"
    assert verify_reply("You are not eligible for a refund.", [ELIG], "refund?")["action"] == "escalate"


def test_policy_exception_claim_without_tool_backing_is_removed():
    v = verify_reply("Hello! As a special exception we can waive the fee for you.", FACTS, "fee?")
    assert "waive" not in v["reply"].lower() or v["action"] == "escalate"


def test_success_claim_without_executed_write_is_blocked():
    assert verify_reply("Your refund has been processed.", FACTS, "refund?", executed=False)["action"] == "escalate"
    assert verify_reply("Your refund has been processed.", FACTS, "refund?", executed=True)["action"] == "pass"


def test_wrong_date_is_caught():
    v = verify_reply("Hi Ana. Your flight leaves on November 21.", FACTS, "when do I fly?")
    assert "November 21" not in (v["reply"] or "")
