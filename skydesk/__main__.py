"""python -m skydesk chat [--thread ID] | python -m skydesk run --scenario ID | python -m skydesk scenarios"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / "scenarios.yaml"


def _show_approval(req: dict) -> None:
    print("\n=== APPROVAL REQUIRED ===")
    print(f"action   : {req['action']}  {json.dumps(req['args'])}")
    print(f"approval : {req.get('approval_id')}  customer: {(req.get('customer') or {}).get('customer_id')}")
    el = req.get("eligibility") or {}
    for c in el.get("conditions") or []:
        print(f"  cond   : [{'x' if c.get('passed', True) else ' '}] {c.get('rule_id', '')} {c.get('condition', c)}")
    for x in el.get("exceptions_applied") or []:
        print(f"  except : {x}")
    q = req.get("quote") or {}
    for line in q.get("lines") or []:
        print(f"  quote  : {line.get('amount'):>9} {q.get('currency', 'USD')}  {line.get('label')}  ({line.get('rule_id')})")
    for k in ("refund_to_original_payment", "travel_credit", "collect_from_customer", "meal_voucher", "skymiles"):
        if q.get(k):
            print(f"  total  : {k} = {q[k]}")
    for n in req.get("risk_notes") or []:
        print(f"  risk   : {n}")


def _ask_decision() -> dict:
    while True:
        a = input("approve / reject / edit? [a/r/e] ").strip().lower()[:1]
        if a in ("a", "r"):
            return {"decision": "approve" if a == "a" else "reject", "approver": os.environ.get("USER", "cli"),
                    "note": input("note (optional): ").strip() or None}
        if a == "e":
            try:
                edited = json.loads(input("edited args as JSON, e.g. {\"new_flight_id\": \"SD123-X\"}: "))
            except json.JSONDecodeError:
                print("not JSON")
                continue
            return {"decision": "edit", "edited_args": edited, "approver": os.environ.get("USER", "cli")}


def _ensure_db():
    from skydesk import db, seed
    if not Path(db.DB_PATH).exists():
        seed.build(db.DB_PATH)


def _agent():
    from skydesk.agent import Agent, AgentConfig
    from skydesk.agent.memory import CustomerMemory
    _ensure_db()
    return Agent(cfg=AgentConfig(memory=CustomerMemory()))


def chat(thread: str | None) -> None:
    from rag.llm import LLM
    a, thread = _agent(), thread or f"chat-{uuid.uuid4().hex[:8]}"
    print(f"SkyDesk support agent  (thread {thread}; resume later with --thread {thread}; blank line quits)")
    req = a.pending(thread)
    while True:
        while req:
            _show_approval(req)
            r = a.resume(thread, _ask_decision())
            req = r["approval_request"]
            if r["reply"]:
                print(f"\nagent> {r['reply']}   [{r['outcome']}]")
        try:
            text = input("\nyou> ").strip()
        except EOFError:
            break
        if not text:
            break
        r = a.send(thread, text)
        req = r["approval_request"]
        if r["reply"]:
            print(f"\nagent> {r['reply']}   [{r['outcome']}]")
    print(f"\nspend so far ${LLM().spent():.4f} of ${LLM().budget:.2f}")


def run(sid: str) -> int:
    spec = yaml.safe_load(SCENARIOS.read_text())
    if sid not in spec:
        print(f"unknown scenario {sid!r}; one of: {', '.join(spec)}")
        return 2
    sc = spec[sid]
    dbp = HERE.parent / "store" / "scenarios" / f"{sid}.db"
    dbp.parent.mkdir(parents=True, exist_ok=True)
    dbp.unlink(missing_ok=True)
    os.environ["SKYDESK_DB"] = str(dbp)  # before skydesk.db is imported: fresh, repeatable data
    from skydesk import faults, seed
    seed.build(dbp)
    faults.set_injector(sc.get("faults"))
    from rag.llm import LLM
    spent0 = LLM().spent()
    a, thread = _agent(), f"scn-{sid}-{uuid.uuid4().hex[:6]}"
    decisions = list(sc.get("approvals") or [])
    print(f"# {sid}: {sc['description']}\n# thread {thread}")
    r = {}
    for text in sc["turns"]:
        print(f"\nyou> {text}")
        r = a.send(thread, text)
        while r["approval_request"]:
            _show_approval(r["approval_request"])
            d = decisions.pop(0) if decisions else {"decision": "approve", "approver": "scenario"}
            print(f"  human  : {d}")
            r = a.resume(thread, d)
        print(f"\nagent> {r['reply']}\n  [outcome={r['outcome']} steps={r['state'].get('steps')} "
              f"verify={(r['state'].get('verify') or {}).get('action')}]")
    fired = faults.injector().fired
    if fired:
        print(f"\nfaults fired: {fired}")
    print(f"scenario cost ${LLM().spent() - spent0:.5f}; total spend ${LLM().spent():.4f} of ${LLM().budget:.2f}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m skydesk")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="interactive chat; approval prompts appear inline")
    c.add_argument("--thread", help="resume an existing conversation (survives restarts)")
    r = sub.add_parser("run", help="run a scripted scenario end to end")
    r.add_argument("--scenario", required=True)
    sub.add_parser("scenarios", help="list scenarios")
    args = p.parse_args(argv)
    if args.cmd == "chat":
        chat(args.thread)
        return 0
    if args.cmd == "run":
        return run(args.scenario)
    for k, v in yaml.safe_load(SCENARIOS.read_text()).items():
        print(f"{k:24} {v['description']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
