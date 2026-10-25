"""SkyDesk Airways demo site (web/) + the Week 3 support agent behind a JSON API.

    .venv/bin/uvicorn server:app --port 9009 --host 127.0.0.1

POST /api/chat     {thread_id, message}  -> runs the LangGraph agent until a reply or an approval interrupt
POST /api/approve  {thread_id, decision, edited_args?, note?, approver?} -> resumes via Command(resume=...)
GET  /api/thread/{id}   current turn state (polled by the widget for live steps and outside decisions)
GET  /api/pending       approvals waiting across threads (the /supervisor page)
GET  /api/cost | /api/log?thread_id= | GET/POST /api/faults | POST /api/reset | GET /api/health
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT = Path(__file__).resolve().parent
UI_ARL = PROJECT / "logs" / "ui" / "agent_runs.arl.jsonl"
UI_MEMORY = PROJECT / "store" / "ui_memory.db"
GENERIC_ERR = "Sorry, something went wrong on our side. Please try again or ask for a human agent."

STEP_LABEL = {
    "verify_customer": "Verifying identity", "lookup_booking": "Looking up booking",
    "list_customer_bookings": "Listing your bookings", "check_eligibility": "Checking policy",
    "quote_amount": "Quoting amount", "search_help_center": "Searching help center",
    "request_approval": "Requesting supervisor approval", "human_approval": "Supervisor decision",
    "rebook_flight": "Rebooking flight", "issue_refund": "Issuing refund", "issue_voucher": "Issuing voucher",
    "waive_fee": "Waiving fee", "update_contact": "Updating contact details", "cancel_booking": "Cancelling booking",
    "escalate_to_human": "Handing off to a person", "verify_claims": "Checking every figure",
}
QUOTE_LABEL = {"cancel": "Quoting refund", "refund_disruption": "Quoting refund", "change": "Quoting change",
               "rebook_disruption": "Quoting change", "delay_compensation": "Quoting compensation"}
TEAM_NAME = {"care": "Care Team", "payments": "Payments Integrity", "customer_relations": "Customer Relations",
             "contact_center": "Contact Center"}
KIND_REASON = {
    "policy_topic": "SkyDesk policy says a person handles this",
    "requires_human": "Policy requires a human decision",
    "tool_failure": "A system the assistant needs is not responding",
    "unverified_number": "A price could not be verified, so it was withheld",
    "write_failed": "The approved change failed in our system",
    "verification_failed": "Identity could not be verified",
    "max_steps": "The assistant could not finish safely",
    "planner_down": "The assistant is unavailable",
}
FAULT_PRESETS = {
    "none": None,
    "quote_flaky": {"quote_amount": {"mode": "503", "nth": [1]}},          # fails once, retry recovers
    "lookup_flaky": {"lookup_booking": {"mode": "timeout", "nth": [1]}},   # times out once, retry recovers
    "quote_down": {"quote_amount": {"mode": "503", "p": 1.0}},             # price can't be verified -> human
    "search_timeout": {"search_help_center": {"mode": "timeout", "p": 1.0}},
}

app = FastAPI(title="SkyDesk Support Agent demo")
_lock = threading.Lock()
AGENT = None            # tests replace this with an agent on fake tools + a scripted planner
PLANNER = "unknown"
MEMORY = None
SEEN: dict[str, float] = {}       # thread_id -> last activity (this process)
FAULT = {"preset": "none"}


# ------------------------------------------------------------------------------------------ agent
class RulesFallback:
    """shortcut: the $0 keyword baseline (eval/rules_planner.py) drives the demo when the OpenAI key fails the
    free preflight; fix OPENAI_API_KEY in .env and restart to get the gpt-4.1-mini planner."""

    def __init__(self):
        sys.path.insert(0, str(PROJECT / "eval"))
        from rules_planner import RulesPlanner
        self.cls = RulesPlanner

    @staticmethod
    def _last_name(messages: list[dict]) -> str:
        from skydesk import db
        conn = db.connect()
        names = {r[0] for r in conn.execute("SELECT last_name FROM customers")}
        conn.close()
        text = " ".join(m.get("content") or "" for m in messages if m.get("role") == "user")
        return next((w for w in re.findall(r"[A-Z][a-z]+", text) if w in names), "")

    def __call__(self, messages, schemas):
        return self.cls({"last_name": self._last_name(messages)})(messages, schemas)


def _pick_planner():
    from skydesk.agent.graph import openai_planner
    mode = os.environ.get("SKYDESK_PLANNER", "auto")
    if mode == "rules":
        return RulesFallback(), "rules"
    if mode == "auto":
        from rag.llm import LLM
        try:
            LLM().client.models.retrieve("gpt-4.1-mini")  # free call: a bad key fails here, not mid-demo
        except Exception:  # noqa: BLE001
            return RulesFallback(), "rules"
    return openai_planner(), "gpt-4.1-mini"


def agent():
    global AGENT, PLANNER, MEMORY
    with _lock:
        if AGENT is None:
            from skydesk import db, seed
            from skydesk.agent.graph import Agent, AgentConfig
            from skydesk.agent.memory import CustomerMemory
            if not Path(os.environ.get("SKYDESK_DB") or db.DB_PATH).exists():
                seed.build()
            planner, PLANNER = _pick_planner()
            MEMORY = CustomerMemory(UI_MEMORY)
            AGENT = Agent(planner=planner, cfg=AgentConfig(memory=MEMORY, arl_path=UI_ARL))
        return AGENT


# ------------------------------------------------------------------------------------------ shaping
def steps(events: list[dict]) -> list[dict]:
    """Turn ARL events into the activity strip: one row per tool call, retries folded into the row."""
    args = {e["call_id"]: e.get("args") or {} for e in events if e.get("type") == "tool_call"}
    out: list[dict] = []
    for e in events:
        tool = e.get("tool")
        if e.get("type") != "tool_result" or tool not in STEP_LABEL:
            continue
        attempt = (e.get("meta") or {}).get("attempt", 1)
        ok = e.get("status") == "ok"
        if tool == "verify_claims":
            res = e.get("result") or {}
            status, summary = ("ok", "all figures backed by tool results") if ok else (
                "withheld", f"{sum(len(r.get('claims') or []) for r in res.get('removed') or [])} unverified claim(s) removed")
        elif tool == "human_approval":
            d = (e.get("result") or {}).get("decision")
            status, summary = ("ok" if d == "approve" else "rejected" if d == "reject" else "edited"), d
        else:
            status = "ok" if ok else ("timeout" if e.get("status") == "timeout" else "failed")
            summary = None if ok else e.get("error")
        label = STEP_LABEL[tool]
        if tool == "quote_amount":
            label = QUOTE_LABEL.get(args.get(e.get("call_id"), {}).get("action"), label)
        if attempt > 1 and out and out[-1]["tool"] == tool:   # a retry of the row above
            out[-1].update(status=status, summary=summary, retries=attempt - 1,
                           ms=round(out[-1]["ms"] + (e.get("latency_ms") or 0), 1))
            continue
        out.append({"tool": tool, "label": label, "status": status, "summary": summary, "retries": 0,
                    "ms": e.get("latency_ms") or 0})
    return out


def _interrupt(req: dict | None) -> dict | None:
    if not req:
        return None
    return {"approval_id": req.get("approval_id"), "action": req.get("action"), "request_type": req.get("request_type"),
            "args": {k: v for k, v in (req.get("args") or {}).items() if k not in ("basis",)},
            "eligibility": req.get("eligibility"), "quote": req.get("quote"), "risk": req.get("risk_notes") or [],
            "customer": {k: v for k, v in (req.get("customer") or {}).items() if k in ("customer_id", "first_name")},
            "thread_id": req.get("thread_id")}


def _escalation(st: dict) -> dict | None:
    esc = st.get("escalation")
    if st.get("outcome") != "escalated" or not esc:
        return None
    t = esc.get("ticket") or {}
    team = t.get("team") or esc.get("team_name") or TEAM_NAME.get(esc.get("team"), "Contact Center")
    reason = KIND_REASON.get(esc.get("kind"), "The assistant handed this to a person")
    if esc.get("kind") == "tool_failure" and esc.get("tool") == "quote_amount":
        reason = "The price could not be verified, so none was quoted"
    if esc.get("kind") in ("policy_topic", "requires_human"):
        reason += ": " + re.sub(r"^must-escalate topic: ", "", esc.get("reason") or "").replace("_", " ")
    return {"team": team, "kind": esc.get("kind"), "reason": reason, "ticket_id": t.get("ticket_id"),
            "expected_response": t.get("expected_response"),
            "priority": "high" if esc.get("team") in ("care", "payments", "customer_relations") else "normal"}


def _verified(st: dict) -> dict:
    v = st.get("verify") or {}
    return {"numbers_checked": v.get("checked", 0), "action": v.get("action"),
            "withheld": [c for r in v.get("removed") or [] for c in r.get("claims") or []]}


def shape(r: dict, since: int = 0) -> dict:
    st = r["state"]
    return {"thread_id": r["thread_id"], "run_id": st.get("run_id"), "reply": r["reply"], "outcome": r["outcome"],
            "steps": steps((st.get("events") or [])[since:]), "interrupt": _interrupt(r["approval_request"]),
            "escalation": None if r["approval_request"] else _escalation(st), "verified": _verified(st)}


def _cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


def _tid(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", t)[:48]


# ------------------------------------------------------------------------------------------ API
class Chat(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=2000)


class Decision(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64)
    decision: str
    edited_args: dict | None = None
    note: str | None = Field(default=None, max_length=500)
    approver: str = Field(default="supervisor@skydesk", max_length=80)


def _run(fn):
    from rag import llm as llm_mod
    from skydesk.agent.graph import NotPending
    try:
        return fn()
    except NotPending:  # double-clicked Approve, or decided on /supervisor meanwhile
        return JSONResponse({"error": "This approval was already decided."}, status_code=409)
    except llm_mod.BudgetExceeded:
        return JSONResponse({"error": "The demo has reached its $1.00 API budget. A SkyDesk agent can still help."},
                            status_code=503)
    except Exception:  # noqa: BLE001 -- never leak a traceback to the customer
        return JSONResponse({"error": GENERIC_ERR}, status_code=500)


@app.post("/api/chat")
def chat(body: Chat):
    tid, text = _tid(body.thread_id), body.message.strip()
    if not tid or not text:
        return JSONResponse({"error": "Please type a message."}, status_code=400)
    a = agent()
    if a.pending(tid):
        return JSONResponse({"error": "An action is waiting for supervisor approval on this chat."}, status_code=409)
    SEEN[tid] = time.time()
    return _run(lambda: shape(a.send(tid, text)))


@app.post("/api/approve")
def approve(body: Decision):
    tid = _tid(body.thread_id)
    if body.decision not in ("approve", "reject", "edit"):
        return JSONResponse({"error": "decision must be approve, reject or edit"}, status_code=400)
    if body.decision == "edit" and not body.edited_args:
        return JSONResponse({"error": "edit needs edited_args"}, status_code=400)
    a = agent()
    if not a.pending(tid):
        return JSONResponse({"error": "Nothing is waiting for approval on this chat."}, status_code=409)
    SEEN[tid] = time.time()
    since = len(a.graph.get_state(_cfg(tid)).values.get("events") or [])
    d = {"decision": body.decision, "approver": body.approver, "note": body.note}
    if body.decision == "edit":
        d["edited_args"] = body.edited_args
    return _run(lambda: shape(a.resume(tid, d), since))


@app.get("/api/thread/{thread_id}")
def thread(thread_id: str):
    tid, a = _tid(thread_id), agent()
    st = a.graph.get_state(_cfg(tid)).values
    if not st:
        return JSONResponse({"error": "unknown thread"}, status_code=404)
    req = a.pending(tid)
    return shape({"thread_id": tid, "reply": None if req else st.get("reply"), "approval_request": req,
                  "outcome": st.get("outcome"), "state": st})


def _threads() -> list[str]:
    ids = set(SEEN)
    conn = getattr(agent().graph.checkpointer, "conn", None)
    if conn is not None:
        try:
            ids |= {r[0] for r in conn.execute("SELECT thread_id, MAX(checkpoint_id) m FROM checkpoints "
                                               "GROUP BY thread_id ORDER BY m DESC LIMIT 200")}
        except Exception:  # noqa: BLE001
            pass
    return sorted(ids)


@app.get("/api/pending")
def pending():
    a, out = agent(), []
    for tid in _threads():
        req = a.pending(tid)
        if req:
            st = a.graph.get_state(_cfg(tid)).values
            last = next((m["content"] for m in reversed(st.get("messages") or []) if m.get("role") == "user"), "")
            out.append({**_interrupt(req), "thread_id": tid, "customer_message": last[:300]})
    return {"pending": out}


@app.get("/api/cost")
def cost():
    from rag import llm as llm_mod
    agent()
    return {**llm_mod.summary(), "planner": PLANNER}


@app.get("/api/log")
def log(thread_id: str):
    tid = _tid(thread_id)
    if not UI_ARL.exists():
        return JSONResponse({"error": "No runs logged yet."}, status_code=404)
    lines, runs = [], set()
    for ln in UI_ARL.read_text(encoding="utf-8").splitlines():
        ev = json.loads(ln)
        if ev.get("type") == "run_start" and (ev.get("meta") or {}).get("thread_id") == tid:
            runs.add(ev["run_id"])
        if ev.get("run_id") in runs:
            lines.append(ln)
    if not lines:
        return JSONResponse({"error": "No runs logged for this chat yet."}, status_code=404)
    return Response("\n".join(lines) + "\n", media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="{tid}.arl.jsonl"'})


class Faults(BaseModel):
    preset: str


@app.get("/api/faults")
def get_faults():
    return {**FAULT, "presets": list(FAULT_PRESETS)}


@app.post("/api/faults")
def set_faults(body: Faults):
    from skydesk import faults
    if body.preset not in FAULT_PRESETS:
        return JSONResponse({"error": f"preset must be one of {list(FAULT_PRESETS)}"}, status_code=400)
    faults.set_injector(FAULT_PRESETS[body.preset])
    FAULT["preset"] = body.preset
    return get_faults()


@app.post("/api/reset")
def reset():
    """Demo only: reseed the airline DB, drop the web chats' threads, clear customer memory and faults,
    so a recording can be repeated from a clean state."""
    from skydesk import faults, seed
    a = agent()
    for tid in _threads():
        if tid.startswith("web-"):
            a.graph.checkpointer.delete_thread(tid)
    SEEN.clear()
    seed.build()
    if MEMORY is not None:
        MEMORY.db.executescript("DELETE FROM requests; DELETE FROM prefs;")
        MEMORY.db.commit()
    faults.set_injector(None)
    FAULT["preset"] = "none"
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/supervisor")
def supervisor():
    return FileResponse(PROJECT / "web" / "supervisor.html")


app.mount("/", StaticFiles(directory=PROJECT / "web", html=True), name="web")
