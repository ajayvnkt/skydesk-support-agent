"""Regression tests for the fixes made after the first real-planner (gpt-4.1-mini) eval run. API-free."""
import json

from agent_fakes import BOOKINGS, VERIFY, FakeTools, ScriptedPlanner, err, make_agent, ok

from skydesk.agent import prompts as P
from skydesk.agent.verify import PRICE_Q_RE, verify_reply


def _tools_msgs(planner):
    return [json.loads(m["content"]) for m in planner.seen[-1] if m.get("role") == "tool"]


# ---- verify.py -------------------------------------------------------------------------------------------------
def test_successful_quote_is_a_later_policy_decision_than_an_earlier_failed_check():
    # turn 1: check on a not-yet-verified booking said eligible=false; turn 2: verified, quote succeeded
    tr = [{"tool": "check_eligibility", "ok": True, "data": {"eligible": False, "conditions": []}},
          {"tool": "quote_amount", "ok": True, "data": {"travel_credit": 100.0}}]
    assert verify_reply("You are eligible for a travel credit of $100.", tr, "what compensation?")["action"] == "pass"
    tr2 = tr + [{"tool": "quote_amount", "ok": False, "data": None, "error": "NOT_ELIGIBLE: already compensated"}]
    assert verify_reply("You are eligible for a travel credit of $100.", tr2, "again?")["action"] == "escalate"


def test_customer_amount_echoed_in_policy_backed_refusal_is_not_a_claim():
    tr = [{"tool": "check_eligibility", "ok": True, "data": {
        "eligible": False, "reasons": ["DLY-001: delay compensation only when the delay was caused by SkyDesk; "
                                       "weather is outside SkyDesk control -- not met"]}}]
    q = "My flight was 4 hours late because of the storm. I want the $100 credit."
    s = "The delay was caused by weather, so you are not eligible for the $100 credit."
    assert verify_reply(s, tr, q)["action"] == "pass"
    # the same figure stated as something we will give is still unbacked
    assert verify_reply("You will get a $100 credit.", tr, q)["action"] == "escalate"
    # and a refusal is only backed when policy really said no
    tr_yes = [{"tool": "check_eligibility", "ok": True, "data": {"eligible": True}}]
    assert verify_reply(s, tr_yes, q)["action"] == "escalate"


def test_done_claim_backed_by_a_successful_write_earlier_in_the_thread():
    tr = [{"tool": "cancel_booking", "ok": True, "data": {"status": "cancelled", "refund": 290.4}}]
    s = "The cancellation has already been processed; the booking has been cancelled."
    assert verify_reply(s, tr, "did it go through?", executed=False)["action"] == "pass"
    failed = [{"tool": "cancel_booking", "ok": False, "data": None, "error": "boom"}]
    assert verify_reply(s, failed, "did it go through?", executed=False)["action"] == "escalate"


def test_amount_questions_include_get_back_and_compensation():
    for q in ("What would I get back if I cancelled?", "What compensation am I owed?"):
        assert PRICE_Q_RE.search(q)


# ---- planner guardrails in the graph ------------------------------------------------------------------------------
def test_figure_less_answer_to_an_amount_question_is_nudged_to_quote(tmp_path):
    tools = FakeTools()
    p = ScriptedPlanner([VERIFY, ("say", "You would get the taxes back."),
                         ("call", [("quote_amount", {"booking_ref": "SKY123", "action": "cancel"})]),
                         ("say", "You would get $120.00 back.")])
    a = make_agent(tmp_path, p, tools)
    r = a.send("t1", "Lee, SKY123, card 4242. What would I get back if I cancel?")
    assert r["reply"] == "You would get $120.00 back." and tools.n("quote_amount") == 1
    assert any(m.get("content") == P.QUOTE_NUDGE for m in p.seen[2])
    # only once per turn: a second figure-less answer is accepted (then verified as usual)
    p2 = ScriptedPlanner([VERIFY, ("say", "Taxes only."), ("say", "Taxes only, sorry.")])
    r = make_agent(tmp_path, p2, FakeTools()).send("t2", "Lee, SKY123, card 4242. How much would I get back?")
    assert r["reply"] == "Taxes only, sorry."


def test_no_nudge_when_policy_already_said_not_eligible(tmp_path):
    tools = FakeTools()
    tools.eligibility = {**tools.eligibility, "eligible": False, "reasons": ["weather"]}
    p = ScriptedPlanner([VERIFY, ("call", [("check_eligibility", {"booking_ref": "SKY123",
                                                                  "request_type": "delay_compensation"})]),
                         ("say", "Weather delays are not compensated.")])
    r = make_agent(tmp_path, p, tools).send("t1", "Lee, SKY123, 4242. What compensation do I get?")
    assert r["reply"] == "Weather delays are not compensated." and p.i == 3


def test_check_eligibility_carries_a_policy_derived_next_step(tmp_path):
    tools = FakeTools()
    tools.eligibility = {**tools.eligibility, "eligible": False, "requires_human": True,
                         "team_key": "contact_center", "exceptions_applied": []}
    p = ScriptedPlanner([VERIFY, ("call", [("check_eligibility", {"booking_ref": "SKY123",
                                                                  "request_type": "waive_fee"})]), ("say", "ok")])
    make_agent(tmp_path, p, tools).send("t1", "Lee SKY123 4242, waive my change fee?")
    assert "call waive_fee" in _tools_msgs(p)[-1]["next_step"]
    unverified = {"eligible": False, "conditions": [{"rule_id": "VER-001", "passed": False}]}
    assert "verify_customer" in P.eligibility_hint("delay_compensation", unverified)
    assert "escalate_to_human with team=care" in P.eligibility_hint(
        "cancel", {"requires_human": True, "team_key": "care", "exceptions_applied": ["bereavement"]})
    assert "Not eligible" in P.eligibility_hint("cancel", {"eligible": False})


def test_change_quote_on_a_disrupted_booking_becomes_the_free_rebooking(tmp_path, monkeypatch):
    monkeypatch.setitem(BOOKINGS, "SCH001", {"booking_ref": "SCH001", "customer_id": "C1", "status": "schedule_changed",
                                             "schedule_change_minutes": 95, "alternatives": [{"flight_id": "F1-1027"}]})
    tools = FakeTools()
    p = ScriptedPlanner([VERIFY, ("call", [("lookup_booking", {"booking_ref": "SCH001"})]),
                         ("call", [("quote_amount", {"booking_ref": "SCH001", "action": "change",
                                                     "params": {"new_flight_id": "F1-1027"}})]), ("say", "ok")])
    make_agent(tmp_path, p, tools).send("t1", "Lee SKY123 4242, move SCH001 to F1 please")
    assert [kw["action"] for n, kw in tools.calls if n == "quote_amount"] == ["rebook_disruption"]


def test_unknown_flight_error_lists_the_valid_flight_ids(tmp_path, monkeypatch):
    monkeypatch.setitem(BOOKINGS, "SKY124", {"booking_ref": "SKY124", "customer_id": "C1", "status": "confirmed",
                                             "alternatives": [{"flight_id": "SD301-20261103"}]})
    tools = FakeTools(quote_amount=[err("NOT_FOUND: no flight 'SD301'", False)])
    p = ScriptedPlanner([VERIFY, ("call", [("lookup_booking", {"booking_ref": "SKY124"}),
                                           ("quote_amount", {"booking_ref": "SKY124", "action": "change",
                                                             "params": {"new_flight_id": "SD301"}})]), ("say", "ok")])
    make_agent(tmp_path, p, tools).send("t1", "Lee SKY123 4242, cost to move SKY124 to SD301?")
    assert _tools_msgs(p)[-1]["valid_new_flight_ids"] == ["SD301-20261103"]


def test_asking_again_for_a_booking_reference_the_customer_gave_is_nudged_to_verify(tmp_path):
    tools = FakeTools()
    p = ScriptedPlanner([("say", "Could you give me your 6-character booking reference?"), VERIFY,
                         ("say", "Thanks Ana, you are verified.")])
    r = make_agent(tmp_path, p, tools).send("t1", "My flight on SKY123 was late. Lee, card 4242.")
    assert tools.n("verify_customer") == 1 and r["reply"] == "Thanks Ana, you are verified."
    assert "SKY123" in [m for m in p.seen[1] if m.get("role") == "system"][-1]["content"]
    # no reference-shaped token in the message: asking for it is the right answer, no nudge
    p2 = ScriptedPlanner([("say", "Could you give me your booking reference?")])
    r = make_agent(tmp_path, p2, FakeTools()).send("t2", "My flight was late, I'm Lee.")
    assert r["reply"] == "Could you give me your booking reference?" and p2.i == 1


def test_quote_replaced_by_an_approver_edit_is_never_told_as_the_moved_amount(tmp_path):
    tools = FakeTools()
    p = ScriptedPlanner([VERIFY, ("call", [("issue_refund", {"booking_ref": "SKY123", "basis": "cancel"})]),
                         ("say", "Your refund of $120.00 has been processed.")])
    a = make_agent(tmp_path, p, tools)
    a.send("t1", "Lee SKY123 4242, refund me please")
    a.resume("t1", {"decision": "edit", "edited_args": {"amount": 100.0}, "approver": "sup"})
    r = a.resume("t1", {"decision": "approve", "approver": "sup"})
    assert [kw["amount"] for n, kw in tools.calls if n == "issue_refund"] == [100.0]
    assert "100.0 was moved" in _tools_msgs(p)[-1]["note"]          # the planner is told what really moved
    assert "$120.00" not in (r["reply"] or "") and r["state"]["verify"]["action"] != "pass"
    tr = [{"tool": "quote_amount", "ok": True, "data": {"refund_to_original_payment": 120.0}},
          {"tool": "issue_refund", "ok": True, "data": {"amount": 100.0}}]
    assert verify_reply("Your refund of $100.00 has been processed.", tr, "refund", True, exclude={120.0})["action"] == "pass"
