"""Fake tools (TOOL CONTRACT shapes) + a scripted planner for API-free agent tests."""
from __future__ import annotations

import copy
import inspect
import time

from skydesk.agent.graph import Agent, AgentConfig, build_graph  # noqa: F401

BOOKINGS = {
    "SKY123": {"booking_ref": "SKY123", "customer_id": "C1", "status": "confirmed", "fare_class": "SkyFlex",
               "flight": "SD100", "departure": "2026-11-20T09:00:00Z", "total_paid": 150.0},
    "OTH999": {"booking_ref": "OTH999", "customer_id": "C2", "status": "confirmed", "fare_class": "SkyLite"},
}


def ok(data):
    return {"ok": True, "data": data, "error": None, "retryable": False}


def err(msg, retryable):
    return {"ok": False, "data": None, "error": msg, "retryable": retryable}


class FakeTools:
    """`script[name]` = list of results/callables served first (then the default behaviour)."""

    def __init__(self, **script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, dict]] = []
        self.approvals: dict[str, dict] = {}
        self.eligibility = {"eligible": True, "requires_human": False, "outcome": "full_refund",
                            "conditions": [{"rule_id": "REF-001", "condition": "SkyFlex fares are fully refundable",
                                            "passed": True, "required": True}],
                            "reasons": [], "exceptions_applied": [], "rule_ids": ["REF-001"], "team_key": None}
        self.quotes = {"cancel": {"currency": "USD", "lines": [{"label": "Base fare", "amount": 120.0,
                                                               "rule_id": "REF-001"}],
                                  "refund_to_original_payment": 120.0, "travel_credit": 0.0,
                                  "collect_from_customer": 0.0, "rule_ids": ["REF-001"]}}

    def _scripted(self, name, kw):
        self.calls.append((name, kw))
        q = self.script.get(name)
        if q:
            r = q.pop(0)
            return True, (r(**kw) if callable(r) else copy.deepcopy(r))
        return False, None

    def n(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def as_dict(self):
        def mk(name, default):
            def f(**kw):
                hit, r = self._scripted(name, kw)
                return r if hit else default(**kw)
            f.__name__, f.__signature__ = name, inspect.signature(default)
            return f
        return {
            "verify_customer": mk("verify_customer", self._verify),
            "request_approval": mk("request_approval", self._request_approval),
            "decide_approval": mk("decide_approval", self._decide_approval),
            "lookup_booking": mk("lookup_booking", lambda booking_ref: ok(BOOKINGS[booking_ref])
                                 if booking_ref in BOOKINGS else err("booking not found", False)),
            "list_customer_bookings": mk("list_customer_bookings", lambda customer_id: ok(
                [b for b in BOOKINGS.values() if b["customer_id"] == customer_id])),
            "check_eligibility": mk("check_eligibility", lambda booking_ref, request_type, context=None:
                                    ok({**self.eligibility, "request_type": request_type})),
            "quote_amount": mk("quote_amount", self._quote),
            "search_help_center": mk("search_help_center", lambda query: ok(
                [{"doc_id": "REF-001", "text": "SkyFlex fares are fully refundable."}])),
            "rebook_flight": mk("rebook_flight", lambda booking_ref, new_flight_id, approval_id, idempotency_key:
                                ok({"booking_ref": booking_ref, "flight": new_flight_id, "status": "rebooked"})),
            "issue_refund": mk("issue_refund", lambda booking_ref, amount, approval_id, idempotency_key:
                               ok({"refund_id": "RF-1", "amount": amount, "status": "issued"})),
            "issue_voucher": mk("issue_voucher", lambda booking_ref, amount, kind, approval_id, idempotency_key:
                                ok({"voucher_id": "V-1", "amount": amount})),
            "waive_fee": mk("waive_fee", lambda booking_ref, approval_id, idempotency_key, fee_type=None:
                            ok({"waived": True})),
            "update_contact": mk("update_contact", lambda customer_id, approval_id, idempotency_key, email=None,
                                 phone=None: ok({"customer_id": customer_id, "email": email})),
            "cancel_booking": mk("cancel_booking", lambda booking_ref, approval_id, idempotency_key:
                                 ok({"booking_ref": booking_ref, "status": "cancelled"})),
            "escalate_to_human": mk("escalate_to_human", lambda reason, team, summary, booking_ref=None:
                                    ok({"ticket_id": "ESC-0001", "team": team})),
        }

    def _verify(self, booking_ref, last_name, card_last4=None):
        if (booking_ref, last_name.lower()) != ("SKY123", "lee"):
            return ok({"verified": False, "attempts_left": 2, "message": "Details do not match."})
        if not card_last4:
            return ok({"verified": False, "needs": "card_last4"})
        return ok({"verified": card_last4 == "4242", "customer_id": "C1", "first_name": "Ana",
                   "booking_ref": "SKY123"} if card_last4 == "4242" else {"verified": False, "attempts_left": 1})

    def _request_approval(self, action, target, params=None, summary=""):
        aid = f"APR-{len(self.approvals) + 1:04d}"
        self.approvals[aid] = {"action": action, "target": target, "params": params, "status": "pending"}
        return ok({"approval_id": aid, "status": "pending"})

    def _decide_approval(self, approval_id, approved, approver):
        a = self.approvals[approval_id]
        if a["status"] != "pending":
            return err(f"APPROVAL_ALREADY_DECIDED: {a['status']}", False)
        a["status"] = "approved" if approved else "rejected"
        return ok({"approval_id": approval_id, "status": a["status"]})

    def _quote(self, booking_ref, action, params=None):
        if action in ("change", "same_day_change"):
            fid = (params or {}).get("new_flight_id")
            diff = {"F1": 40.0, "F2": 15.0}.get(fid, 0.0)
            return ok({"currency": "USD", "lines": [{"label": "Fare difference", "amount": diff, "rule_id": "CHG-002"}],
                       "refund_to_original_payment": 0.0, "travel_credit": 0.0, "collect_from_customer": diff,
                       "rule_ids": ["CHG-002"]})
        return ok(copy.deepcopy(self.quotes.get(action, self.quotes["cancel"])))


class ScriptedPlanner:
    """Each step: ("call", [(name, args), ...]) or ("say", text). Records every prompt it saw."""

    def __init__(self, steps, loop_last=False):
        self.steps, self.i, self.loop_last, self.seen = list(steps), 0, loop_last, []

    def __call__(self, messages, schemas):
        self.seen.append(copy.deepcopy(messages))
        if self.i >= len(self.steps):
            if not self.loop_last:
                raise AssertionError(f"planner script exhausted at call {self.i}")
            step = self.steps[-1]
        else:
            step = self.steps[self.i]
        self.i += 1
        kind, body = step
        meta = {"model": "fake", "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0, "latency_ms": 1.0}
        if kind == "say":
            return {"content": body, "tool_calls": []}, meta
        return {"content": None, "tool_calls": [{"id": f"c{self.i}_{j}", "name": n, "arguments": a}
                                                for j, (n, a) in enumerate(body)]}, meta


VERIFY = ("call", [("verify_customer", {"booking_ref": "SKY123", "last_name": "Lee", "card_last4": "4242"})])


def make_agent(tmp_path, planner, tools: FakeTools, checkpointer=None, **cfg):
    from langgraph.checkpoint.memory import InMemorySaver
    sleeps: list[float] = []
    conf = AgentConfig(sleep=sleeps.append, arl_path=tmp_path / "arl.jsonl", **cfg)
    a = Agent(tools=tools.as_dict(), planner=planner, checkpointer=checkpointer or InMemorySaver(), cfg=conf)
    a.sleeps = sleeps
    return a


def slow(seconds, result):
    def f(**kw):
        time.sleep(seconds)
        return result
    return f
