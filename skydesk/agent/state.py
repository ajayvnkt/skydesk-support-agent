"""Agent state. Everything is plain JSON-able dicts/lists so the SqliteSaver checkpointer can persist it."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def add_or_reset(a: list | None, b: list | None) -> list:
    """Per-turn ARL buffer: appends, but a batch starting with run_start begins a new turn."""
    if b and b[0].get("type") == "run_start":
        return list(b)
    return (a or []) + (b or [])


class AgentState(TypedDict, total=False):
    # conversation (OpenAI chat format dicts: role/content/tool_calls/tool_call_id)
    messages: Annotated[list[dict], operator.add]
    # verified identity {customer_id, name, booking_ref, ...} -- None until verify_customer passes
    customer: dict | None
    booking: dict | None            # active booking (last successful lookup_booking)
    memory: dict | None             # persistent customer memory loaded after verification
    plan: list[str]                 # planner's stated next steps (free text, for the trace)
    tool_results: Annotated[list[dict], operator.add]   # {step, tool, args, ok, data, error}
    pending_action: dict | None     # proposed write: {action, args, eligibility, quote, risk, ...}
    approvals: Annotated[list[dict], operator.add]      # human decisions
    retries: dict[str, int]         # tool name -> retries used this turn
    errors: Annotated[list[dict], operator.add]         # {tool, error, retryable, final}
    escalation: dict | None         # {reason, team, summary, ticket}
    verify_failures: int            # failed identity checks this conversation
    steps: int                      # planner steps this turn
    cost: float                     # USD spent by the planner this conversation
    run_id: str                     # one ARL run per customer turn
    thread_id: str
    guard: dict | None              # {escalate, topic, injection}
    draft: str | None               # planner's final text before verify
    reply: str | None               # what the customer is shown
    verify: dict | None             # verify.py report for this turn
    outcome: str | None             # answered|executed|rejected|escalated|not_eligible
    events: Annotated[list[dict], add_or_reset]          # ARL events of the current turn


# per-turn fields the guard node resets at the start of every customer message
TURN_RESET: dict[str, Any] = {
    "steps": 0, "retries": {}, "pending_action": None, "draft": None, "reply": None,
    "verify": None, "outcome": None, "guard": None, "plan": [], "escalation": None,
}
