"""Agent graph against the REAL backend tools (seeded airline DB), scripted planner, no API."""
import json

from agent_fakes import ScriptedPlanner
from langgraph.checkpoint.memory import InMemorySaver

from skydesk import db, faults
from skydesk.agent.graph import Agent, AgentConfig, default_tools


def creds(ref):
    conn = db.connect()
    b = db.booking(conn, ref)
    last = conn.execute("SELECT last_name FROM customers WHERE customer_id=?", (b["customer_id"],)).fetchone()[0]
    conn.close()
    return {"booking_ref": ref, "last_name": last, "card_last4": b["card_last4"]}


def agent(tmp_path, steps):
    sleeps = []
    a = Agent(tools=default_tools(), planner=ScriptedPlanner(steps), checkpointer=InMemorySaver(),
              cfg=AgentConfig(sleep=sleeps.append, arl_path=tmp_path / "arl.jsonl"))
    return a


def test_cancel_end_to_end_with_backend_approval_and_quote(tdb, tmp_path):
    c = creds("FLXRFD")
    a = agent(tmp_path, [("call", [("verify_customer", c)]),
                         ("call", [("cancel_booking", {"booking_ref": "FLXRFD"})]),
                         ("say", "Your booking FLXRFD has been cancelled.")])
    r = a.send("t1", "please cancel FLXRFD")
    req = r["approval_request"]
    assert req["approval_id"].startswith("APR-") and req["quote"]["lines"] and req["eligibility"]["eligible"]
    r = a.resume("t1", {"decision": "approve", "approver": "supervisor@test"})
    assert r["outcome"] == "executed", r["state"].get("escalation")
    conn = db.connect()
    assert db.booking(conn, "FLXRFD")["status"] == "cancelled"
    conn.close()


def test_injected_quote_outage_escalates_with_real_ticket(tdb, tmp_path):
    faults.set_injector({"quote_amount": {"mode": "503", "nth": [1, 2, 3]}})
    c = creds("FLXRFD")
    a = agent(tmp_path, [("call", [("verify_customer", c)]),
                         ("call", [("cancel_booking", {"booking_ref": "FLXRFD"})])])
    r = a.send("t1", "cancel FLXRFD")
    assert r["outcome"] == "escalated" and "ESC-" in r["reply"] and "$" not in r["reply"]
    conn = db.connect()
    assert db.booking(conn, "FLXRFD")["status"] != "cancelled"
    conn.close()


def test_human_only_waiver_goes_to_the_approver_not_the_bot(tdb, tmp_path):
    c = creds("CLSCHG")
    a = agent(tmp_path, [("call", [("verify_customer", c)]),
                         ("call", [("waive_fee", {"booking_ref": "CLSCHG", "fee_type": "change_fee"})]),
                         ("say", "A supervisor has waived your change fee.")])
    r = a.send("t1", "can you waive the change fee on CLSCHG?")
    req = r["approval_request"]
    assert req["action"] == "waive_fee" and any("WVR-001" in n for n in req["risk_notes"])
    r = a.resume("t1", {"decision": "approve", "approver": "supervisor@test"})
    assert r["outcome"] == "executed", r["state"].get("escalation")
    conn = db.connect()
    assert db.booking(conn, "CLSCHG")["fee_waived"] == 1
    conn.close()


def test_guard_uses_policy_exception_keywords(tdb, tmp_path):
    r = agent(tmp_path, []).send("t1", "my mother is terminally ill, I can't fly next week")
    assert r["outcome"] == "escalated" and r["state"]["escalation"]["team"] == "care"
