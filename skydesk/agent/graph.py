"""SkyDesk support agent: a LangGraph state machine around a tool-calling planner.

guard -> planner <-> tools (READ tools, autonomous) ; WRITE -> propose (check_eligibility + quote_amount)
-> human_approval (interrupt) -> execute -> planner ; final text -> verify -> respond ; escalate -> respond.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import inspect
import json
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from rag.llm import PLANNER_MODEL, PROJECT, STORE
from . import prompts as P
from .memory import CustomerMemory, scrub
from .state import TURN_RESET, AgentState
from .verify import MONEY_RE, PRICE_Q_RE, verify_reply

AGENT = "skydesk-support-agent"
ARL_PATH = PROJECT / "logs" / "agent_runs.arl.jsonl"
CHECKPOINT_DB = STORE / "checkpoints.db"
Planner = Callable[[list[dict], list[dict]], tuple[dict, dict]]
_POOL = cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


@dataclass
class AgentConfig:
    max_steps: int = 8              # planner steps per customer turn
    max_attempts: int = 3           # per READ tool call (retryable errors only)
    write_attempts: int = 2         # writes: only retryable errors, SAME idempotency key
    tool_timeout: float = 8.0       # seconds, per attempt
    timeouts: dict = field(default_factory=lambda: {"search_help_center": 15.0})
    backoff_base: float = 0.25
    backoff_max: float = 2.0
    max_verify_failures: int = 2
    sleep: Callable[[float], None] = time.sleep
    arl_path: Path | str | None = ARL_PATH
    memory: CustomerMemory | None = None


# ----------------------------------------------------------------------------------- tool running
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ev(type_: str, **f) -> dict:
    return {"type": type_, "ts": _now(), "agent": AGENT, **{k: v for k, v in f.items() if v is not None}}


def _call(fn: Callable, args: dict) -> dict:
    """Call with only the kwargs the function accepts (tool signatures may be narrower than our args)."""
    try:
        ps = inspect.signature(fn).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in ps.values()):
            args = {k: v for k, v in args.items() if k in ps}
    except (TypeError, ValueError):
        pass
    return fn(**args)


def _validate(res, tool: str) -> dict:
    """Tool contract: {ok: bool, data, error, retryable}. Anything else is malformed (retryable once more)."""
    if not isinstance(res, dict) or not isinstance(res.get("ok"), bool):
        return {"ok": False, "data": None, "error": f"malformed result from {tool}", "retryable": True}
    want = list if tool in ("list_customer_bookings", "search_help_center") else dict
    if res["ok"] and res.get("data") in (None, {}, ""):
        return {"ok": False, "data": None, "error": f"empty result from {tool}", "retryable": True}
    if res["ok"] and not isinstance(res["data"], want):
        return {"ok": False, "data": None, "error": f"malformed data from {tool}", "retryable": True}
    if res["ok"] and tool == "check_eligibility" and not (
            isinstance(res["data"], dict) and isinstance(res["data"].get("eligible"), bool)):
        return {"ok": False, "data": None, "error": "malformed eligibility result", "retryable": True}
    return {"ok": res["ok"], "data": res.get("data"), "error": res.get("error"),
            "retryable": bool(res.get("retryable"))}


def run_tool(name: str, fn: Callable, args: dict, cfg: AgentConfig, write: bool = False) -> tuple[dict, list[dict]]:
    """Timeout + retry with exponential backoff and jitter, only while the error is retryable.
    Writes keep the same args (incl. idempotency_key) on every attempt. Returns (result, ARL events)."""
    events, max_n = [], cfg.write_attempts if write else cfg.max_attempts
    timeout = cfg.timeouts.get(name, cfg.tool_timeout)
    for attempt in range(1, max_n + 1):
        cid = f"{name}-{uuid.uuid4().hex[:6]}"
        events.append(_ev("tool_call", tool=name, call_id=cid, args=scrub(args), meta={"attempt": attempt}))
        t0 = time.perf_counter()
        try:
            res = _validate(_POOL.submit(_call, fn, args).result(timeout=timeout), name)
        except cf.TimeoutError:
            res = {"ok": False, "data": None, "error": f"timeout after {timeout}s", "retryable": True}
        except Exception as e:  # a raising tool is a bug, not a transient fault
            res = {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}", "retryable": False}
        ms = round((time.perf_counter() - t0) * 1000, 1)
        status = "ok" if res["ok"] else ("timeout" if "timeout" in str(res["error"]).lower() else "error")
        events.append(_ev("tool_result", tool=name, call_id=cid, result=res["data"] if res["ok"] else None,
                          status=status, error=res["error"], latency_ms=ms,
                          meta={"attempt": attempt, "retryable": res["retryable"]}))
        if res["ok"] or not res["retryable"] or attempt == max_n:
            res["attempts"] = attempt
            return res, events
        cfg.sleep(min(cfg.backoff_max, cfg.backoff_base * 2 ** (attempt - 1)) * (0.5 + random.random() / 2))
    raise AssertionError("unreachable")


def _sanitize(v, hits: list):
    """Tool output is data: strip instruction-like text before the planner sees it."""
    if isinstance(v, str):
        if P.INJECTION_RE.search(v):
            hits.append(v[:80])
            return "[instruction-like text removed]"
        return v
    if isinstance(v, dict):
        return {k: _sanitize(x, hits) for k, x in v.items()}
    if isinstance(v, list):
        return [_sanitize(x, hits) for x in v]
    return v


def _tool_msg(call_id: str, payload: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, default=str)[:4000]}


def _prompt(state: AgentState) -> list[dict]:
    """System prompt + memory + history, with every tool_call answered right after its assistant message
    (outcomes of writes arrive later, after approval; OpenAI requires that ordering)."""
    msgs = state.get("messages", [])
    answers = {m["tool_call_id"]: m for m in msgs if m.get("role") == "tool"}
    out = [{"role": "system", "content": P.SYSTEM_PROMPT}]
    if state.get("customer"):
        out.append({"role": "system", "content": "Verified customer: " + json.dumps(state["customer"])
                    + (" | Memory: " + json.dumps(state["memory"]) if state.get("memory") else "")})
    for m in msgs:
        if m.get("role") == "tool":
            continue
        out.append(m)
        for tc in m.get("tool_calls") or []:
            out.append(answers.get(tc["id"]) or _tool_msg(tc["id"], {"ok": False, "error": "no result"}))
    return out


def _backend_basis(tool: str, args: dict, b: dict) -> str | None:
    """The quote action the backend write will itself apply (tools.issue_refund / rebook_flight pick it from the
    booking, not from the caller). Quoting the same action means the human approves the amount that will move."""
    if tool not in ("issue_refund", "rebook_flight") or "status" not in b:
        return None
    from skydesk import policy
    disrupted = policy.disruption(b)
    if tool == "issue_refund":
        return "refund_disruption" if disrupted or policy.tier(b.get("delay_minutes") or 0).get("alt_refund") \
            else "cancel"
    if disrupted:
        return "rebook_disruption"
    nf = next((a for a in b.get("alternatives") or [] if a.get("flight_id") == args.get("new_flight_id")), None)
    try:
        dep, now = policy.ts(b["departs_at"]), policy.ts(b["now"])
        same_day = nf and 0 < (dep - now).total_seconds() <= 86400 and policy.ts(nf["departs_at"]).date() == dep.date()
    except (KeyError, TypeError, ValueError):
        same_day = False
    return "same_day_change" if same_day else "change"


def _last_user(state: AgentState) -> str:
    return next((m["content"] for m in reversed(state.get("messages", [])) if m.get("role") == "user"), "")


def _nudge(state: AgentState, draft: str) -> str | None:
    """Planner guardrails, at most one note per turn, when a final answer skips a step the request needs:
    asking again for a booking reference the customer already gave, or answering an amount question without a quote."""
    msgs = state.get("messages", [])
    since_user = msgs[max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=0):]
    if any(m.get("role") == "system" and str(m.get("content")).startswith(P.NUDGE) for m in since_user):
        return None
    ref = P.REF_RE.search(_last_user(state))
    if not state.get("customer") and ref and "reference" in (draft or "").lower():
        return P.REF_NUDGE.format(ref=ref[0])
    return P.QUOTE_NUDGE if _needs_quote(state, draft) else None


def _superseded(pa: dict) -> list[float]:
    """The quoted figure an approver's edit replaced: no longer a fact the customer may be told."""
    if not isinstance(pa.get("edited_amount"), (int, float)):
        return []
    field_ = P.amount_field(pa.get("tool"), pa.get("args") or {})
    q = (pa.get("quote") or {}).get(field_) if field_ else None
    moved = (pa.get("args") or {}).get("amount")
    return [q] if isinstance(q, (int, float)) and moved is not None and abs(q - moved) > 0.005 else []


def _needs_quote(state: AgentState, draft: str) -> bool:
    """A verified customer asked about an amount, the draft states none, and no quote was tried this turn
    (and policy did not already say no) -> nudge to quote instead of a figure-less answer."""
    if not state.get("customer") or state.get("pending_action") or MONEY_RE.search(draft or "") \
            or not PRICE_Q_RE.search(_last_user(state)):
        return False
    turn = [r for r in state.get("tool_results", []) if r.get("run_id") == state.get("run_id")]
    if any(r["tool"] == "quote_amount" for r in turn):
        return False
    elig = [r for r in turn if r["tool"] == "check_eligibility" and r.get("ok")]
    return not elig or bool((elig[-1].get("data") or {}).get("eligible"))


# ----------------------------------------------------------------------------------- the graph
def build_graph(tools: dict[str, Callable], planner: Planner, checkpointer=None,
                cfg: AgentConfig | None = None):
    cfg = cfg or AgentConfig()

    def tr(state, tool, args, res) -> dict:
        return {"run_id": state.get("run_id"), "tool": tool, "args": args, "ok": res["ok"],
                "data": res.get("data"), "error": res.get("error")}

    # ---------------------------------------------------------------- guard
    def guard(state: AgentState) -> dict:
        text = _last_user(state)
        run_id = f"sda-{uuid.uuid4().hex[:12]}"
        out = {**TURN_RESET, "run_id": run_id,
               "events": [_ev("run_start", model=PLANNER_MODEL, task_type="support_agent",
                              meta={"thread_id": state.get("thread_id")}),
                          _ev("user_input", content=text)]}
        injection = bool(P.INJECTION_RE.search(text))
        for topic, team, rx in P.MUST_ESCALATE:
            if rx.search(text):
                out["guard"] = {"escalate": True, "topic": topic, "injection": injection}
                out["escalation"] = {"kind": "policy_topic", "reason": f"must-escalate topic: {topic}",
                                     "team": team}
                return out
        out["guard"] = {"escalate": False, "topic": None, "injection": injection}
        if injection:  # a guardrail note, not a run error: logged on the user_input event
            out["messages"] = [{"role": "system", "content": P.INJECTION_NOTE}]
            out["events"][-1]["meta"] = {"injection_detected": True, "handling": "treated as data"}
        return out

    # ---------------------------------------------------------------- planner
    def planner_node(state: AgentState) -> dict:
        steps = state.get("steps", 0)
        if steps >= cfg.max_steps:
            return {"escalation": {"kind": "max_steps", "reason": f"no resolution after {steps} steps",
                                   "team": "contact_center"}}
        try:
            msg, meta = planner(_prompt(state), P.TOOL_SCHEMAS)
        except Exception as e:  # budget cap, API outage: hand off, never guess
            return {"escalation": {"kind": "planner_down", "reason": f"planner unavailable: {type(e).__name__}",
                                   "team": "contact_center"},
                    "events": [_ev("error", content=f"planner failed: {type(e).__name__}: {e}", status="error")]}
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            args = c.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {"__invalid_json__": args[:200]}
            calls.append({"id": c.get("id") or f"call_{steps}_{i}", "name": c["name"], "arguments": args})
        am = {"role": "assistant", "content": msg.get("content")}
        if calls:
            am["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                "name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]
        nudge = None if calls else _nudge(state, msg.get("content") or "")
        return {"messages": [am] + ([{"role": "system", "content": nudge}] if nudge else []),
                "steps": steps + 1, "cost": state.get("cost", 0.0) + meta.get("cost_usd", 0.0),
                "draft": None if calls or nudge else (msg.get("content") or ""),
                "plan": state.get("plan", []) + [", ".join(c["name"] for c in calls) or "answer"],
                "events": [_ev("llm_response", model=meta.get("model", PLANNER_MODEL), content=msg.get("content"),
                               tokens_in=meta.get("tokens_in"), tokens_out=meta.get("tokens_out"),
                               cost_usd=meta.get("cost_usd"), latency_ms=meta.get("latency_ms"),
                               meta={"purpose": "planner", "tool_calls": [c["name"] for c in calls]})]}

    def after_planner(state: AgentState) -> str:
        if state.get("escalation"):
            return "escalate"
        if str(state["messages"][-1].get("content")).startswith(P.NUDGE):
            return "planner"
        return "verify" if state.get("draft") is not None else "tools"

    # ---------------------------------------------------------------- tools (READs autonomous)
    def tools_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        upd: dict = {"messages": [], "tool_results": [], "events": [], "errors": []}
        customer, retries = state.get("customer"), dict(state.get("retries") or {})
        vfail = state.get("verify_failures", 0)
        proposal = None
        for tc in last.get("tool_calls") or []:
            name, cid = tc["function"]["name"], tc["id"]
            args = json.loads(tc["function"]["arguments"])
            if name == "escalate_to_human":
                upd["escalation"] = {"kind": "planner", "reason": args.get("reason") or "planner escalated",
                                     "team": args.get("team") if args.get("team") in P.TEAMS else "contact_center",
                                     "summary": args.get("summary"), "call_id": cid}
                continue
            if name in P.WRITE_TOOLS:
                if proposal:
                    upd["messages"].append(_tool_msg(cid, {"ok": False, "error": "one change at a time; "
                                                           "propose this again after the first is decided"}))
                else:
                    proposal = {"tool": name, "args": args, "call_id": cid}
                continue
            if name not in P.READ_TOOLS or name not in tools:
                upd["messages"].append(_tool_msg(cid, {"ok": False, "error": f"unknown tool {name}"}))
                continue
            if name != "search_help_center" and name != "verify_customer" and not customer:
                upd["messages"].append(_tool_msg(cid, {"ok": False, "error": "verify the customer first"}))
                continue
            if name == "list_customer_bookings":
                args = {**args, "customer_id": customer["customer_id"]}  # only the verified customer's
            b = upd.get("booking") or state.get("booking") or {}
            b_loaded = "status" in b and b.get("booking_ref") == str(args.get("booking_ref", "")).upper()
            if name == "quote_amount" and b_loaded and args.get("action") in ("change", "same_day_change"):
                from skydesk import policy
                if policy.disruption(b):  # airline-caused: the free rebooking the write will apply, not a fee quote
                    args = {**args, "action": "rebook_disruption"}
            res, evs = run_tool(name, tools[name], args, cfg)
            upd["events"] += evs
            retries[name] = retries.get(name, 0) + res["attempts"] - 1
            if res["ok"] and name == "lookup_booking" and isinstance(res["data"], dict) and customer \
                    and res["data"].get("customer_id") not in (None, customer["customer_id"]):
                res = {"ok": False, "data": None, "error": "booking does not belong to the verified customer",
                       "retryable": False, "attempts": res["attempts"]}
            upd["tool_results"].append(tr(state, name, args, res))
            if not res["ok"]:
                upd["errors"].append({"tool": name, "error": res["error"], "retryable": res["retryable"],
                                      "attempts": res["attempts"]})
            permanent = not res["ok"] and (res["retryable"] or "malformed" in str(res["error"]))
            if name == "verify_customer":
                d = res.get("data") if res["ok"] else None
                if isinstance(d, dict) and d.get("verified") is True and d.get("customer_id"):
                    # sanitized too: this dict is put into the system prompt, so backend text must not carry orders
                    customer = _sanitize({k: d.get(k) for k in ("customer_id", "first_name", "name") if d.get(k)}, [])
                    customer["booking_ref"] = (d.get("booking_ref") or args.get("booking_ref") or "").upper()
                    upd["customer"] = customer
                    if cfg.memory:
                        upd["memory"] = cfg.memory.load(customer["customer_id"])
                elif "LOCKED" in str(res.get("error")) or (isinstance(d, dict) and d.get("locked")):
                    upd["escalation"] = {"kind": "verification_failed", "team": "payments",
                                         "reason": "verification locked after repeated failures"}
                elif isinstance(d, dict) and d.get("needs"):
                    pass  # partial match: planner asks for the missing detail, not a failure
                elif not permanent:
                    vfail += 1
                    upd["verify_failures"] = vfail
                    if vfail >= cfg.max_verify_failures:
                        upd["escalation"] = {"kind": "verification_failed", "team": "contact_center",
                                             "reason": f"identity not verified after {vfail} attempts"}
            if name == "lookup_booking" and res["ok"]:
                upd["booking"] = res["data"]
            if permanent and name in P.CRITICAL_READS:
                upd["escalation"] = {"kind": "tool_failure", "team": "contact_center", "tool": name,
                                     "reason": f"{name} failed after {res['attempts']} attempts: {res['error']}"}
            payload = {"ok": res["ok"], "data": res.get("data"), "error": res.get("error")}
            if not res["ok"] and "no flight" in str(res["error"]) and b_loaded:
                payload["valid_new_flight_ids"] = [a.get("flight_id") for a in b.get("alternatives") or []]
            if not res["ok"] and name == "search_help_center":
                payload["fallback"] = ("Help center unavailable. Answer from check_eligibility/quote_amount "
                                       "results, or apologise and offer an agent. Do not guess.")
            if name == "verify_customer" and not res["ok"] and not permanent:
                payload["note"] = "identity not verified; ask the customer to re-check the details"
            hits: list = []
            payload = _sanitize(payload, hits)
            if hits:  # guardrail note on the tool_result itself (not an error event)
                upd["events"][-1].setdefault("meta", {})["instruction_text_removed"] = [h[:80] for h in hits[:3]]
            if name == "check_eligibility" and res["ok"]:  # added after _sanitize: our own text names write tools
                payload["next_step"] = P.eligibility_hint(args.get("request_type"), res["data"])
            upd["messages"].append(_tool_msg(cid, payload))
        upd["retries"] = retries
        if proposal and not upd.get("escalation"):
            upd["pending_action"] = {"status": "proposed", **proposal}
        return upd

    def after_tools(state: AgentState) -> str:
        if state.get("escalation"):
            return "escalate"
        pa = state.get("pending_action")
        return "propose" if pa and pa.get("status") == "proposed" else "planner"

    # ---------------------------------------------------------------- propose (policy + quote, no model numbers)
    def propose(state: AgentState) -> dict:
        pa = dict(state["pending_action"])
        tool, args, cid = pa["tool"], dict(pa["args"]), pa["call_id"]
        customer, booking = state.get("customer"), state.get("booking") or {}
        upd: dict = {"tool_results": [], "events": [], "messages": []}

        def back_to_planner(payload: dict, outcome: str | None = None) -> dict:
            upd["messages"].append(_tool_msg(cid, payload))
            upd["pending_action"] = {**pa, "status": "declined_by_policy"}
            if outcome:
                upd["outcome"] = outcome
            return upd

        if not customer:
            return back_to_planner({"ok": False, "error": "verify the customer before any change"})
        ref = args.get("booking_ref") or booking.get("booking_ref") or booking.get("ref") or customer.get("booking_ref")
        if tool != "update_contact":
            if not ref:
                return back_to_planner({"ok": False, "error": "booking_ref is required"})
            if (booking.get("booking_ref") or booking.get("ref")) != ref:  # load + ownership check
                res, evs = run_tool("lookup_booking", tools["lookup_booking"], {"booking_ref": ref}, cfg)
                upd["events"] += evs
                upd["tool_results"].append(tr(state, "lookup_booking", {"booking_ref": ref}, res))
                if not res["ok"]:
                    if res["retryable"]:
                        upd["escalation"] = {"kind": "tool_failure", "team": "contact_center", "tool": "lookup_booking",
                                             "reason": f"lookup_booking failed: {res['error']}"}
                        return upd
                    return back_to_planner({"ok": False, "error": res["error"]})
                booking = res["data"]
                if booking.get("customer_id") not in (None, customer["customer_id"]):
                    return back_to_planner({"ok": False, "error": "booking does not belong to the verified customer"})
                upd["booking"] = booking
            args["booking_ref"] = ref
        else:
            args["customer_id"] = customer["customer_id"]
        if tool == "waive_fee":
            args.setdefault("fee_type", "change_fee")  # the only waivable fee; the backend requires it
        bases = P.WRITE_TOOLS[tool]
        basis = _backend_basis(tool, args, booking) or (args.get("basis") if args.get("basis") in bases else bases[0])
        req = {"rebook_disruption": None, "refund_disruption": None}.get(basis, basis) if basis else \
            ("waive_fee" if tool == "waive_fee" else "update_contact")
        if req is None:  # disruption: request type follows the booking status
            req = {"airline_cancelled": "airline_cancellation", "schedule_changed": "schedule_change",
                   "delayed": "delay_compensation"}.get(booking.get("status"), "schedule_change")
        ctx = {k: v for k, v in args.items() if k in ("reason", "new_flight_id", "email", "phone")}
        ctx["message"] = _last_user(state)[:500]  # lets policy catch bereavement / fraud / legal
        el_args = {"booking_ref": ref or customer.get("booking_ref"), "request_type": req, "context": ctx}
        el, evs = run_tool("check_eligibility", tools["check_eligibility"], el_args, cfg)
        upd["events"] += evs
        upd["tool_results"].append(tr(state, "check_eligibility", el_args, el))
        if not el["ok"]:
            if el["retryable"] or "malformed" in str(el["error"]):
                upd["escalation"] = {"kind": "tool_failure", "team": "contact_center", "tool": "check_eligibility",
                                     "reason": f"policy check failed: {el['error']}"}
                return upd
            return back_to_planner({"ok": False, "error": el["error"]})
        e = el["data"]
        # WVR-001: a waiver is human-only, and the approver IS that human -> approval queue, not a hand-off
        human_decides = tool == "waive_fee" and e.get("requires_human") and not e.get("exceptions_applied") \
            and e.get("team_key") == "contact_center"
        if e.get("requires_human") and not human_decides:
            upd["messages"].append(_tool_msg(cid, {"ok": False, "error": "policy requires a human decision",
                                                   "reasons": e.get("reasons")}))
            upd["pending_action"] = {**pa, "status": "needs_human"}
            upd["escalation"] = {"kind": "requires_human", "reason": "; ".join(e.get("reasons") or [])
                                 or f"{req} requires a human", "team": e.get("team_key") or "contact_center",
                                 "team_name": e.get("team")}
            return upd
        if not e.get("eligible") and not human_decides:
            return back_to_planner({"ok": False, "error": "not eligible under policy", "reasons": e.get("reasons"),
                                    "rule_ids": e.get("rule_ids")}, outcome="not_eligible")
        quote = None
        if basis:
            params = {k: args[k] for k in ("new_flight_id",) if k in args}
            if tool == "issue_voucher":
                params["choice"] = "miles" if args.get("kind") == "miles" else "credit"
            q_args = {"booking_ref": ref, "action": basis, "params": params}
            q, evs = run_tool("quote_amount", tools["quote_amount"], q_args, cfg)
            upd["events"] += evs
            upd["tool_results"].append(tr(state, "quote_amount", q_args, q))
            if not q["ok"]:
                if q["retryable"] or "malformed" in str(q["error"]):  # price can't be verified -> human, not a guess
                    upd["escalation"] = {"kind": "tool_failure", "team": "contact_center", "tool": "quote_amount",
                                         "reason": f"price could not be verified: {q['error']}"}
                    return upd
                return back_to_planner({"ok": False, "error": q["error"]})
            quote = q["data"]
            field_ = P.amount_field(tool, args)
            if field_:
                amt = quote.get(field_)
                if not isinstance(amt, (int, float)) or (amt <= 0 and field_ != "skymiles"):
                    return back_to_planner({"ok": False, "error": f"quote gives no {field_} for this "
                                            "booking", "quote": quote}, outcome="not_eligible")
                edited = pa.get("edited_amount")
                args["amount"] = 0 if field_ == "skymiles" else round(
                    min(edited, amt) if isinstance(edited, (int, float)) and edited > 0 else amt, 2)
        args["basis"] = basis  # dropped by _call if the write tool doesn't take it
        risk = []
        if tool in ("issue_refund", "cancel_booking"):
            risk.append("irreversible: money leaves / booking is cancelled")
        if quote and quote.get("collect_from_customer"):
            risk.append(f"customer must pay {quote['collect_from_customer']} {quote.get('currency', 'USD')}")
        if human_decides:
            risk.append("policy WVR-001: the assistant may not decide this; your approval is the decision")
        if tool == "update_contact":
            risk.append("account-takeover risk: confirm the one-time code went to the EXISTING contact")
        if (state.get("guard") or {}).get("injection"):
            risk.append("customer message contained instruction-like text")
        key_src = json.dumps([state.get("thread_id"), tool, {k: v for k, v in args.items()}], sort_keys=True)
        pa.update({"status": "awaiting_approval", "args": args, "request_type": req,
                   "action_id": pa.get("action_id") or f"act-{uuid.uuid4().hex[:8]}",
                   "idempotency_key": "idem-" + hashlib.sha256(key_src.encode()).hexdigest()[:24],
                   "eligibility": {k: e.get(k) for k in ("eligible", "outcome", "conditions", "exceptions_applied",
                                                          "reasons", "rule_ids")},
                   "quote": quote and {k: quote.get(k) for k in (
                       "currency", "lines", "refund_to_original_payment", "travel_credit", "collect_from_customer",
                       "meal_voucher", "skymiles", "refund_cap", "rule_ids")},
                   "risk_notes": risk})
        expected = {k: quote[k] for k in P.EXPECTED_FIELDS if quote and isinstance(quote.get(k), (int, float))}
        apr_params = {**{k: v for k, v in args.items()
                         if k not in ("booking_ref", "customer_id", "basis", "reason")}, "expected": expected}
        ar_args = {"action": tool, "target": ref if tool != "update_contact" else customer["customer_id"],
                   "params": apr_params, "summary": f"{tool} for {customer['customer_id']}: {json.dumps(apr_params)}"}
        ar, evs = run_tool("request_approval", tools["request_approval"], ar_args, cfg)
        upd["events"] += evs
        if not ar["ok"] or not (ar["data"] or {}).get("approval_id"):
            upd["escalation"] = {"kind": "tool_failure", "team": "contact_center", "tool": "request_approval",
                                 "reason": f"could not register the approval request: {ar['error']}"}
            return upd
        pa["approval_id"] = ar["data"]["approval_id"]
        upd["pending_action"] = pa
        return upd

    def after_propose(state: AgentState) -> str:
        if state.get("escalation"):
            return "escalate"
        return "human_approval" if state["pending_action"]["status"] == "awaiting_approval" else "planner"

    # ---------------------------------------------------------------- human approval (interrupt)
    def approval(state: AgentState) -> dict:
        pa = state["pending_action"]
        payload = {"type": "approval_request", "action_id": pa["action_id"], "approval_id": pa["approval_id"],
                   "action": pa["tool"],
                   "args": pa["args"], "request_type": pa["request_type"], "eligibility": pa["eligibility"],
                   "quote": pa["quote"], "risk_notes": pa["risk_notes"], "customer": state.get("customer"),
                   "thread_id": state.get("thread_id"),
                   "resume_with": {"decision": "approve|reject|edit", "edited_args": "dict (edit only)",
                                   "approver": "str", "note": "str"}}
        d = interrupt(payload)
        d = d if isinstance(d, dict) else {"decision": str(d)}
        decision = d.get("decision") if d.get("decision") in ("approve", "reject", "edit") else "reject"
        rec = {"action_id": pa["action_id"], "action": pa["tool"], "decision": decision,
               "approver": d.get("approver") or "unknown", "note": d.get("note"), "ts": _now()}
        evs = [_ev("tool_call", tool="human_approval", call_id=pa["action_id"], args=scrub(payload),
                   meta={"event": "approval_requested"}),
               _ev("tool_result", tool="human_approval", call_id=pa["action_id"], result=rec,
                   status="ok", meta={"event": "approval_decision"})]  # a rejection is a decision, not a failure
        dres, devs = run_tool("decide_approval", tools["decide_approval"], {
            "approval_id": pa["approval_id"], "approved": decision == "approve", "approver": rec["approver"]}, cfg)
        evs += devs
        upd: dict = {"approvals": [rec], "events": evs}
        if decision == "approve" and not dres["ok"]:
            upd["pending_action"] = {**pa, "status": "failed", "error": dres["error"]}
            upd["escalation"] = {"kind": "write_failed", "team": "contact_center", "tool": pa["tool"],
                                 "reason": f"approval could not be recorded: {dres['error']}"}
        elif decision == "approve":
            upd["pending_action"] = {**pa, "status": "approved", "approver": rec["approver"]}
        elif decision == "edit":
            edited = {k: v for k, v in (d.get("edited_args") or {}).items()
                      if k not in ("approval_id", "idempotency_key", "customer_id")}
            amount = edited.pop("amount", None)  # may only lower the quoted amount; propose re-quotes
            upd["pending_action"] = {**pa, "status": "proposed", "args": {**pa["args"], **edited}, "approval_id": None,
                                     "edited_amount": amount, "edited_by": rec["approver"]}
        else:
            upd["pending_action"] = {**pa, "status": "rejected"}
            upd["outcome"] = "rejected"
            upd["messages"] = [_tool_msg(pa["call_id"], {"ok": False, "error": "rejected by the human approver; "
                                                         "nothing was changed", "note": d.get("note")})]
        return upd

    def after_approval(state: AgentState) -> str:
        if state.get("escalation"):
            return "escalate"
        return {"approved": "execute", "proposed": "propose"}.get(state["pending_action"]["status"], "planner")

    # ---------------------------------------------------------------- execute WRITE (approved only)
    def execute(state: AgentState) -> dict:
        pa = state["pending_action"]
        assert pa.get("status") == "approved" and pa.get("approval_id"), "write without approval"
        args = {**pa["args"], "approval_id": pa["approval_id"], "idempotency_key": pa["idempotency_key"]}
        res, evs = run_tool(pa["tool"], tools[pa["tool"]], args, cfg, write=True)
        payload = {"ok": res["ok"], "data": res.get("data"), "error": res.get("error")}
        if res["ok"] and _superseded(pa):
            payload["note"] = (f"The approver changed the amount: {args.get('amount')} was moved, not the earlier "
                               f"quote of {_superseded(pa)[0]}. Tell the customer only the amount that was moved.")
        upd = {"events": evs, "tool_results": [tr(state, pa["tool"], args, res)],
               "messages": [_tool_msg(pa["call_id"], payload)]}
        if res["ok"]:
            upd["pending_action"] = {**pa, "status": "executed", "result": res["data"]}
            upd["outcome"] = "executed"
        else:
            upd["pending_action"] = {**pa, "status": "failed", "error": res["error"]}
            upd["errors"] = [{"tool": pa["tool"], "error": res["error"], "retryable": res["retryable"],
                              "attempts": res["attempts"]}]
            upd["escalation"] = {"kind": "write_failed", "team": "contact_center", "tool": pa["tool"],
                                 "reason": f"{pa['tool']} failed after approval: {res['error']}"}
        return upd

    # ---------------------------------------------------------------- verify the draft (instructor feedback)
    def verify_node(state: AgentState) -> dict:
        if not (state.get("draft") or "").strip():
            return {"escalation": {"kind": "planner_down", "team": "contact_center", "reason": "empty answer"}}
        pa = state.get("pending_action") or {}
        executed = pa.get("status") == "executed"
        v = verify_reply(state.get("draft") or "", state.get("tool_results", []), _last_user(state), executed,
                         exclude=set(_superseded(pa)) if executed else set())
        ev = _ev("tool_call", tool="verify_claims", call_id=f"verify-{state.get('steps')}",
                 args={"checked": v["checked"]})
        res = _ev("tool_result", tool="verify_claims", call_id=f"verify-{state.get('steps')}",
                  result={"action": v["action"], "removed": v["removed"]},
                  status="ok" if v["action"] == "pass" else "refused")
        upd = {"verify": v, "events": [ev, res]}
        if v["action"] == "escalate":
            upd["escalation"] = {"kind": "unverified_number", "team": "contact_center",
                                 "reason": "answer depends on a figure that could not be verified: "
                                 + "; ".join(c for r in v["removed"] for c in r["claims"])}
        else:
            upd["reply"] = v["reply"]
            upd["outcome"] = state.get("outcome") or "answered"
        return upd

    # ---------------------------------------------------------------- escalate
    HONEST = {
        "policy_topic": "I'm sorry you're dealing with this. This is something a member of our {team} has to "
                        "handle personally, so I've passed your case to them.",
        "requires_human": "Under SkyDesk policy this request has to be decided by a person, not by me, so I've "
                          "passed it to our {team}.",
        "tool_failure": "I couldn't get the information I need from our systems right now, and I won't guess. "
                        "I've passed your case to a SkyDesk agent.",
        "write_failed": "I'm sorry, the change was NOT completed because our system returned an error. Nothing "
                        "else was changed. I've passed your case to a SkyDesk agent to finish it.",
        "unverified_number": "I can't verify the exact figure from our systems right now, so I won't quote one. "
                             "A SkyDesk agent will confirm it with you.",
        "verification_failed": "I couldn't verify your identity with the details provided, so I can't access the "
                               "booking. I've passed this to a SkyDesk agent who can help verify you.",
        "max_steps": "I wasn't able to finish this safely, so I've passed your case to a SkyDesk agent.",
        "planner_down": "I'm having trouble right now, so I've passed your case to a SkyDesk agent.",
        "planner": "I've passed your case to our {team}.",
    }

    def escalate(state: AgentState) -> dict:
        esc = dict(state["escalation"])
        customer, booking = state.get("customer") or {}, state.get("booking") or {}
        summary = esc.get("summary") or (
            f"[{esc['kind']}] {esc['reason']} | customer={customer.get('customer_id', 'unverified')} "
            f"booking={booking.get('booking_ref') or booking.get('ref') or customer.get('booking_ref', '-')} "
            f"| last message: {_last_user(state)[:200]}")
        args = {"reason": esc["reason"][:300], "team": esc.get("team", "contact_center"), "summary": summary,
                "booking_ref": booking.get("booking_ref") or customer.get("booking_ref")}
        res, evs = run_tool("escalate_to_human", tools["escalate_to_human"], args, cfg)
        team = esc.get("team_name") or {"care": "Care Team", "payments": "Payments Integrity team",
                                        "customer_relations": "Customer Relations team"}.get(esc.get("team"),
                                                                                              "support team")
        msg = HONEST.get(esc["kind"], HONEST["planner"]).format(team=team)
        if res["ok"]:
            tid = (res["data"] or {}).get("ticket_id") if isinstance(res["data"], dict) else None
            msg += f" Your reference is {tid}." if tid else ""
            esc["ticket"] = res["data"]
        else:
            msg += " I also couldn't open the hand-off ticket automatically, so please contact SkyDesk support " \
                   "directly and mention this conversation."
            esc["ticket_error"] = res["error"]
        upd = {"escalation": esc, "reply": msg, "outcome": "escalated",
               "events": evs + [_ev("escalation", content=esc["reason"], meta=scrub(esc))]}
        if esc.get("call_id"):
            upd["messages"] = [_tool_msg(esc["call_id"], {"ok": res["ok"], "data": res.get("data")})]
        return upd

    # ---------------------------------------------------------------- respond (+ ARL + memory)
    def respond(state: AgentState) -> dict:
        reply, outcome = state.get("reply") or "", state.get("outcome") or "answered"
        v = state.get("verify") or {}
        executed = (state.get("pending_action") or {}).get("status") == "executed"
        claimed = outcome == "executed" or (outcome == "answered" and v.get("action") == "pass"
                                            and not _unrecovered_failure(state.get("events", [])))
        evs = [_ev("final_answer", content=reply, claimed_success=claimed,
                   claimed_actions=[state["pending_action"]["tool"]] if executed else [],
                   meta={"outcome": outcome, "verify": v.get("action"), "removed": v.get("removed"),
                         "pending_action": (state.get("pending_action") or {}).get("status")}),
               _ev("run_end", status="ok", outcome={
                   "outcome": outcome, "resolved": claimed, "escalated": outcome == "escalated",
                   "steps": state.get("steps", 0), "total_cost_usd": round(state.get("cost", 0.0), 8),
                   "approvals": len(state.get("approvals", [])),
                   "thread_id": state.get("thread_id")})]
        if cfg.arl_path:
            write_arl(state.get("events", []) + evs, state.get("run_id") or "sda-unknown", cfg.arl_path)
        c = state.get("customer")
        if cfg.memory and c:
            pa = state.get("pending_action") or {}
            # what was done, not the raw customer text (keeps card digits etc. out of memory)
            cfg.memory.record(c["customer_id"], c.get("booking_ref"), pa.get("tool") or " > ".join(state.get("plan") or []),
                              outcome)
        return {"messages": [{"role": "assistant", "content": reply}], "events": evs}

    g = StateGraph(AgentState)
    for name, fn in [("guard", guard), ("planner", planner_node), ("tools", tools_node), ("propose", propose),
                     ("human_approval", approval), ("execute", execute), ("verify", verify_node),
                     ("escalate", escalate), ("respond", respond)]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", lambda s: "escalate" if s.get("escalation") else "planner",
                            ["escalate", "planner"])
    g.add_conditional_edges("planner", after_planner, ["escalate", "verify", "tools", "planner"])
    g.add_conditional_edges("tools", after_tools, ["escalate", "propose", "planner"])
    g.add_conditional_edges("propose", after_propose, ["escalate", "human_approval", "planner"])
    g.add_conditional_edges("human_approval", after_approval, ["execute", "propose", "planner", "escalate"])
    g.add_conditional_edges("execute", lambda s: "escalate" if s.get("escalation") else "planner",
                            ["escalate", "planner"])
    g.add_conditional_edges("verify", lambda s: "escalate" if s.get("escalation") else "respond",
                            ["escalate", "respond"])
    g.add_edge("escalate", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)


def _unrecovered_failure(events: list[dict]) -> bool:
    """A tool in this turn whose last result was not ok (e.g. NOT_ELIGIBLE, outage): the answer is not a success."""
    last = {}
    for e in events:
        if e.get("type") == "tool_result" and e.get("tool") not in ("human_approval", "verify_claims"):
            last[e["tool"]] = e.get("status") == "ok"
    return not all(last.values())


def write_arl(events: list[dict], run_id: str, path: Path | str = ARL_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for i, ev in enumerate(events):
            f.write(json.dumps({"run_id": run_id, "seq": i, **ev}, ensure_ascii=False, default=str) + "\n")


def sqlite_checkpointer(path: Path | str = CHECKPOINT_DB):
    from langgraph.checkpoint.sqlite import SqliteSaver
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def openai_planner(llm=None) -> Planner:
    from rag.llm import LLM
    llm = llm or LLM()
    return lambda messages, schemas: llm.chat_tools(messages, schemas, purpose="agent_planner")


def default_tools() -> dict[str, Callable]:
    from skydesk import tools as T
    names = P.READ_TOOLS + tuple(P.WRITE_TOOLS) + ("escalate_to_human", "request_approval", "decide_approval")
    return {n: getattr(T, n) for n in names}


class NotPending(RuntimeError):
    """resume() on a thread with no approval waiting (already decided, e.g. a double-clicked Approve)."""


class Agent:
    """Thin driver: one thread_id = one conversation; survives restarts via the SQLite checkpointer.
    One turn at a time per thread: two concurrent resumes of the same interrupt would both run the graph
    from the same checkpoint, and the loser's 'approval already decided' would overwrite the winner's success."""

    def __init__(self, tools=None, planner=None, checkpointer=None, cfg: AgentConfig | None = None):
        self.graph = build_graph(tools or default_tools(), planner or openai_planner(),
                                 checkpointer or sqlite_checkpointer(), cfg)
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, thread_id: str) -> threading.Lock:
        return self._locks.setdefault(thread_id, threading.Lock())  # setdefault is atomic in CPython

    def _result(self, thread_id: str, out: dict) -> dict:
        st = self.graph.get_state({"configurable": {"thread_id": thread_id}}).values
        intr = out.get("__interrupt__")
        return {"thread_id": thread_id, "reply": None if intr else st.get("reply"),
                "approval_request": intr[0].value if intr else None, "outcome": st.get("outcome"),
                "state": st}

    def send(self, thread_id: str, text: str) -> dict:
        cfg = {"configurable": {"thread_id": thread_id}}
        with self._lock(thread_id):
            if self.pending(thread_id):
                raise RuntimeError("an action is awaiting approval on this thread; resume it first")
            out = self.graph.invoke({"messages": [{"role": "user", "content": text}], "thread_id": thread_id}, cfg)
            return self._result(thread_id, out)

    def resume(self, thread_id: str, decision: dict) -> dict:
        from langgraph.types import Command
        with self._lock(thread_id):
            if not self.pending(thread_id):  # checked under the lock: the second click sees the first's result
                raise NotPending("nothing is awaiting approval on this thread")
            out = self.graph.invoke(Command(resume=decision), {"configurable": {"thread_id": thread_id}})
            return self._result(thread_id, out)

    def pending(self, thread_id: str) -> dict | None:
        snap = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        for t in snap.tasks or ():
            for i in t.interrupts or ():
                return i.value
        return None
