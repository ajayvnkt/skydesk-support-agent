"""Write each support turn as an Agent Run Log (ARL v1, the Week 1 "Agent Report Card" format):
one JSON event per line, so the Week 1 grader can score this bot."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .llm import CHAT_MODEL, PROJECT

AGENT = "skydesk-support-rag"
LOG_PATH = PROJECT / "logs" / "conversations.arl.jsonl"


def run_events(query: str, state: dict, run_id: str | None = None,
               task_type: str = "support_qa", started: datetime | None = None) -> list[dict]:
    run_id = run_id or f"sdr-{uuid.uuid4().hex[:12]}"
    t = started or datetime.now(timezone.utc)
    events: list[dict] = []

    def add(type_: str, **fields):
        nonlocal t
        t += timedelta(milliseconds=fields.get("latency_ms") or 1)
        ev = {"run_id": run_id, "seq": len(events), "ts": t.isoformat(timespec="milliseconds"),
              "type": type_, "agent": AGENT}
        ev.update({k: v for k, v in fields.items() if v is not None})
        events.append(ev)

    add("run_start", model=CHAT_MODEL, task_type=task_type)
    add("user_input", content=query)
    for i, tr in enumerate(state.get("trace", [])):
        if tr["kind"] == "tool":
            cid = f"{tr['tool']}-{i}"
            add("tool_call", tool=tr["tool"], call_id=cid, args=tr.get("args"))
            add("tool_result", tool=tr["tool"], call_id=cid, result=tr.get("result"),
                status=tr.get("status", "ok"), error=tr.get("error"), latency_ms=tr.get("latency_ms"))
        elif tr["kind"] == "llm":
            add("llm_response", model=tr["model"], content=tr.get("content") or None,
                tokens_in=tr["tokens_in"], tokens_out=tr["tokens_out"], cost_usd=tr["cost_usd"],
                latency_ms=tr["latency_ms"], meta={"purpose": tr["purpose"], **(
                    {"repair_reason": tr["repair_reason"]} if tr.get("repair_reason") else {})})
        elif tr["kind"] == "error":
            add("error", content=tr.get("content"), status="error")

    route = state.get("route")
    if route == "escalate":
        ticket = state.get("ticket") or {}
        add("tool_call", tool="escalate_to_human", call_id="escalate-0", args=ticket)
        add("tool_result", tool="escalate_to_human", call_id="escalate-0",
            result={"queued": True}, status="ok")
        add("escalation", content=state.get("reason"), meta=ticket)
    add("final_answer", content=state.get("answer", ""), claimed_success=route == "answer",
        meta={"route": route, "citations": state.get("citations", []),
              "confidence": state.get("confidence")})
    # cost_usd is a per-step field in ARL (graders sum it), so the run total goes in outcome only.
    add("run_end", status="ok",
        outcome={"route": route, "reason": state.get("reason"),
                 "total_cost_usd": round(state.get("cost", 0.0), 8),
                 "resolved": route == "answer", "escalated": route == "escalate"})
    return events


def append(events: list[dict], path: Path | str = LOG_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
