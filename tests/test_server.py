"""server.py over a real graph with fake tools + a scripted planner (no API, no airline DB writes)."""
import pytest
from agent_fakes import VERIFY, FakeTools, ScriptedPlanner, err, make_agent
from fastapi.testclient import TestClient

import server
from skydesk import faults

CANCEL = [VERIFY, ("call", [("cancel_booking", {"booking_ref": "SKY123"})]),
          ("say", "Your booking SKY123 is cancelled and $120.00 goes back to your card.")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    def use(steps, tools=None):
        tools = tools or FakeTools()
        monkeypatch.setattr(server, "AGENT", make_agent(tmp_path, ScriptedPlanner(steps), tools))
        monkeypatch.setattr(server, "UI_ARL", tmp_path / "arl.jsonl")
        monkeypatch.setattr(server, "SEEN", {})
        return TestClient(server.app), tools
    yield use
    faults.set_injector(None)


def test_write_pauses_for_approval_then_executes(client):
    c, tools = client(CANCEL)
    r = c.post("/api/chat", json={"thread_id": "t1", "message": "Lee, SKY123, card 4242: cancel please"}).json()
    assert r["reply"] is None and r["escalation"] is None
    i = r["interrupt"]
    assert i["action"] == "cancel_booking" and i["approval_id"].startswith("APR-")
    assert i["quote"]["refund_to_original_payment"] == 120.0 and i["eligibility"]["eligible"] is True
    assert [s["label"] for s in r["steps"]][:2] == ["Verifying identity", "Looking up booking"]
    assert "Quoting refund" in [s["label"] for s in r["steps"]]
    assert tools.n("cancel_booking") == 0                       # nothing written before approval
    assert c.post("/api/chat", json={"thread_id": "t1", "message": "hello?"}).status_code == 409
    assert c.get("/api/pending").json()["pending"][0]["thread_id"] == "t1"

    r = c.post("/api/approve", json={"thread_id": "t1", "decision": "approve"}).json()
    assert r["outcome"] == "executed" and "$120.00" in r["reply"] and r["interrupt"] is None
    assert [s["label"] for s in r["steps"]][:2] == ["Supervisor decision", "Cancelling booking"]  # only new steps
    assert r["verified"]["action"] == "pass" and r["verified"]["numbers_checked"] >= 1
    assert tools.n("cancel_booking") == 1
    assert c.get("/api/pending").json()["pending"] == []
    assert c.post("/api/approve", json={"thread_id": "t1", "decision": "approve"}).status_code == 409


def test_reject_changes_nothing(client):
    c, tools = client(CANCEL[:2] + [("say", "Okay, nothing was changed.")])
    c.post("/api/chat", json={"thread_id": "t2", "message": "cancel SKY123"})
    assert c.post("/api/approve", json={"thread_id": "t2", "decision": "maybe"}).status_code == 400
    assert c.post("/api/approve", json={"thread_id": "t2", "decision": "edit"}).status_code == 400
    r = c.post("/api/approve", json={"thread_id": "t2", "decision": "reject", "note": "no"}).json()
    assert r["steps"][0]["status"] == "rejected" and tools.n("cancel_booking") == 0


def test_policy_topic_hands_off_with_ticket(client):
    c, _ = client([])
    r = c.post("/api/chat", json={"thread_id": "t3", "message": "my father passed away, I need to cancel"}).json()
    e = r["escalation"]
    assert r["outcome"] == "escalated" and e["ticket_id"] == "ESC-0001" and e["priority"] == "high"
    assert "bereavement" in e["reason"] and r["steps"][-1]["label"] == "Handing off to a person"


def test_retry_is_folded_into_one_step_with_a_badge(client):
    tools = FakeTools(lookup_booking=[err("TIMEOUT: lookup_booking did not respond in time", True)])
    c, _ = client([VERIFY, ("call", [("lookup_booking", {"booking_ref": "SKY123"})]),
                   ("say", "Your booking is confirmed.")], tools)
    r = c.post("/api/chat", json={"thread_id": "t4", "message": "status of SKY123?"}).json()
    look = [s for s in r["steps"] if s["tool"] == "lookup_booking"]
    assert len(look) == 1 and look[0]["retries"] == 1 and look[0]["status"] == "ok"


def test_thread_log_cost_and_faults(client):
    c, _ = client(CANCEL)
    assert c.get("/api/thread/nope").status_code == 404
    c.post("/api/chat", json={"thread_id": "t5", "message": "cancel SKY123"})
    t = c.get("/api/thread/t5").json()
    assert t["interrupt"]["action"] == "cancel_booking" and t["steps"]
    assert c.get("/api/log", params={"thread_id": "t5"}).status_code == 404   # ARL is written when the turn ends
    c.post("/api/approve", json={"thread_id": "t5", "decision": "approve"})
    log = c.get("/api/log", params={"thread_id": "t5"})
    assert log.status_code == 200 and '"run_start"' in log.text
    assert c.get("/api/log", params={"thread_id": "other"}).status_code == 404
    assert "total_usd" in c.get("/api/cost").json()
    assert c.post("/api/faults", json={"preset": "bogus"}).status_code == 400
    assert c.post("/api/faults", json={"preset": "quote_down"}).json()["preset"] == "quote_down"
    assert faults.injector().spec == {"quote_amount": {"mode": "503", "p": 1.0}}
    c.post("/api/faults", json={"preset": "none"})
    assert faults.injector().spec == {}


def test_steps_label_failures_and_withheld_figures():
    evs = [{"type": "tool_call", "tool": "quote_amount", "call_id": "q1", "args": {"action": "cancel"}},
           {"type": "tool_result", "tool": "quote_amount", "call_id": "q1", "status": "error", "error": "503",
            "meta": {"attempt": 1}, "latency_ms": 2},
           {"type": "tool_result", "tool": "quote_amount", "call_id": "q2", "status": "error", "error": "503",
            "meta": {"attempt": 2}, "latency_ms": 3},
           {"type": "tool_result", "tool": "decide_approval", "call_id": "d", "status": "ok"},
           {"type": "tool_result", "tool": "verify_claims", "call_id": "v", "status": "refused",
            "result": {"removed": [{"claims": ["amount $35"]}]}}]
    s = server.steps(evs)
    assert [x["label"] for x in s] == ["Quoting refund", "Checking every figure"]
    assert s[0]["status"] == "failed" and s[0]["retries"] == 1 and s[0]["ms"] == 5
    assert s[1]["status"] == "withheld"
