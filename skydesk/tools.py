"""SkyDesk tools: plain functions returning {ok, data, error, retryable}.

READ (autonomous): verify_customer, lookup_booking, list_customer_bookings, check_eligibility,
    quote_amount, search_help_center
WRITE (never without an approved, single-use approval_id + an idempotency_key):
    rebook_flight, issue_refund, issue_voucher, waive_fee, update_contact, cancel_booking
HANDOFF: escalate_to_human

Approval lifecycle (called by the agent graph / human UI, not by the planner):
    request_approval(action, target, params, summary) -> APR-id (pending)
    decide_approval(approval_id, approved, approver)    -> approved | rejected
A write checks: approval exists, approved, same action + target, params match, not yet used;
the customer is verified (in the DB, not by the LLM's say-so); policy still allows it; and the
amounts it is about to move equal the amounts the human approved (`expected`), else PRICE_CHANGED.
Every write lands in the `actions` ledger; replaying an idempotency_key is a no-op.

TOOLS[name] = {fn, kind, schema}; schema is an OpenAI function-calling spec.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from . import db, faults, policy

TOOLS: dict[str, dict] = {}


class ToolError(Exception):
    def __init__(self, msg: str, retryable: bool = False, data=None):
        super().__init__(msg)
        self.retryable, self.data = retryable, data


def ok(data) -> dict:
    return {"ok": True, "data": data, "error": None, "retryable": False}


def err(msg: str, retryable: bool = False, data=None) -> dict:
    return {"ok": False, "data": data, "error": msg, "retryable": retryable}


def tool(kind: str, description: str, properties: dict, required: list[str]):
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            fault = faults.injector().check(fn.__name__)
            if fault:
                return fault
            try:
                sig.bind(*a, **kw)
            except TypeError as e:
                return err(f"BAD_ARGS: {e}")
            try:
                return fn(*a, **kw)
            except ToolError as e:
                return err(str(e), e.retryable, e.data)
            except sqlite3.OperationalError as e:  # locked / busy DB: transient
                return err(f"BACKEND_UNAVAILABLE: {e}", retryable=True)
            except Exception as e:  # noqa: BLE001 - a tool must never crash the agent
                return err(f"INTERNAL: {type(e).__name__}: {e}")

        TOOLS[fn.__name__] = {"fn": wrapper, "kind": kind, "schema": {"type": "function", "function": {
            "name": fn.__name__, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}}
        return wrapper
    return deco


def call_tool(name: str, args: dict | None) -> dict:
    if name not in TOOLS:
        return err(f"UNKNOWN_TOOL: {name}")
    if not isinstance(args, dict):
        return err("BAD_ARGS: arguments must be a JSON object")
    return TOOLS[name]["fn"](**args)


def openai_tools(kinds=("read", "write", "handoff")) -> list[dict]:
    return [t["schema"] for t in TOOLS.values() if t["kind"] in kinds]


# ---------------------------------------------------------------- helpers

@contextlib.contextmanager
def _conn():
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _booking(conn, ref) -> dict:
    b = db.booking(conn, ref)
    if not b:
        raise ToolError(f"NOT_FOUND: no booking with reference {str(ref)[:12]!r}")  # never echo long input back
    return b


def _verified(conn, ref: str) -> bool:
    return conn.execute("SELECT 1 FROM verifications WHERE ref=?", (ref.upper(),)).fetchone() is not None


def _customer_verified(conn, cid: str) -> bool:
    return conn.execute("SELECT 1 FROM verifications WHERE customer_id=?", (cid,)).fetchone() is not None


def _require_verified(conn, ref: str):
    if not _verified(conn, ref):
        raise ToolError("NOT_VERIFIED: verify the customer (booking reference + last name + card last 4) first")


def _flight_or_fail(conn, fid) -> dict:
    f = db.flight(conn, fid or "")
    if not f:
        raise ToolError(f"NOT_FOUND: no flight {str(fid)[:12]!r}")
    return f


def _ctx(conn, b: dict, context: dict | None = None) -> dict:
    ctx = {k: v for k, v in (context or {}).items() if k in ("message", "exceptions", "lost_access")}
    ctx["verified"] = _verified(conn, b["ref"])  # from the DB, never from the model
    nf = (context or {}).get("new_flight_id")
    if nf:
        ctx["new_flight"] = _flight_or_fail(conn, nf)
    return ctx


def _money(c: int) -> float:
    return policy.usd(c or 0)


def _summary(conn, b: dict, with_alternatives: bool = False) -> dict:
    out = {"booking_ref": b["ref"], "customer_id": b["customer_id"], "status": b["status"],
           "fare_class": b["fare_class"], "passengers": b["passengers"], "channel": b["channel"],
           "flight_id": b["flight_id"], "flight_no": b["flight_no"], "route": f"{b['origin']}-{b['dest']}",
           "departs_at": b["departs_at"], "arrives_at": b["arrives_at"], "booked_at": b["booked_at"],
           "paid": {"base_fare": _money(b["base_fare"]), "taxes": _money(b["taxes"]),
                    "seats_and_bags": _money(b["ancillaries"]), "paid_with_travel_credit": _money(b["paid_with_credit"]),
                    "already_refunded": _money(b["refunded"]), "currency": "USD"},
           "delay_minutes": b["delay_minutes"], "schedule_change_minutes": b["schedule_change_minutes"],
           "disruption_cause": b["disruption_cause"], "card_dispute_open": bool(b["dispute_open"]),
           "change_fee_waived": bool(b["fee_waived"])}
    if with_alternatives and b["status"] in policy.ACTIVE:
        dep = policy.ts(b["departs_at"])
        rows = conn.execute(
            """SELECT flight_id FROM flights WHERE origin=? AND dest=? AND flight_id<>? AND seats_left>0
               AND departs_at > ? AND departs_at BETWEEN ? AND ? ORDER BY departs_at LIMIT 10""",
            (b["origin"], b["dest"], b["flight_id"], db.now(conn),
             (dep - timedelta(days=3)).isoformat(), (dep + timedelta(days=3, hours=23)).isoformat())).fetchall()
        out["alternatives"] = [{"flight_id": f["flight_id"], "flight_no": f["flight_no"], "departs_at": f["departs_at"],
                                "arrives_at": f["arrives_at"], "seats_left": f["seats_left"],
                                f"fare_{b['fare_class']}_per_passenger": _money(f["fares"][b["fare_class"]])}
                               for f in (db.flight(conn, r["flight_id"]) for r in rows)]
    return out


# ---------------------------------------------------------------- READ tools

@tool("read", "Verify the caller owns the booking. Needs booking reference, last name AND the last 4 digits of the "
      "payment card. Reference + last name alone are NOT enough. 3 failures lock the booking for 24 h.",
      {"booking_ref": {"type": "string"}, "last_name": {"type": "string"},
       "card_last4": {"type": "string", "description": "last 4 digits of the card used to pay"}},
      ["booking_ref", "last_name"])
def verify_customer(booking_ref: str, last_name: str, card_last4: str | None = None) -> dict:
    with _conn() as conn:
        b = _booking(conn, booking_ref)
        max_f = policy.RULES["verification"]["max_failures"]
        if b["verify_failures"] >= max_f:
            raise ToolError("LOCKED: 3 failed verification attempts; self-service is locked for 24 hours and "
                            "must not be overridden -> escalate to Payments Integrity", data={"locked": True})
        cust = conn.execute("SELECT * FROM customers WHERE customer_id=?", (b["customer_id"],)).fetchone()
        name_ok = str(last_name or "").strip().casefold() == cust["last_name"].casefold()
        if name_ok and not card_last4:
            return ok({"verified": False, "needs": "card_last4",
                       "message": "Last name matches; ask for the last 4 digits of the card used to pay."})
        if name_ok and str(card_last4).strip() == b["card_last4"]:
            conn.execute("INSERT INTO verifications VALUES (?,?,?)", (b["ref"], b["customer_id"], db.now(conn)))
            return ok({"verified": True, "customer_id": b["customer_id"], "first_name": cust["first_name"],
                       "booking_ref": b["ref"]})
        n = b["verify_failures"] + 1
        conn.execute("UPDATE bookings SET verify_failures=? WHERE ref=?", (n, b["ref"]))
        return ok({"verified": False, "attempts_left": max(0, max_f - n), "locked": n >= max_f,
                   "message": "Details do not match." + (" Booking is now locked." if n >= max_f else "")})


@tool("read", "Booking details (status, fare class, flight, amounts paid, disruption info) plus alternative flights "
      "on the same route within 3 days with seats. Requires a verified customer. `now` is the backend clock.",
      {"booking_ref": {"type": "string"}}, ["booking_ref"])
def lookup_booking(booking_ref: str) -> dict:
    with _conn() as conn:
        b = _booking(conn, booking_ref)
        _require_verified(conn, b["ref"])
        return ok({**_summary(conn, b, with_alternatives=True), "now": db.now(conn)})


@tool("read", "All bookings of a verified customer.", {"customer_id": {"type": "string"}}, ["customer_id"])
def list_customer_bookings(customer_id: str) -> dict:
    with _conn() as conn:
        if not _customer_verified(conn, customer_id):
            raise ToolError("NOT_VERIFIED: verify the customer on one of their bookings first")
        refs = [r["ref"] for r in conn.execute("SELECT ref FROM bookings WHERE customer_id=? ORDER BY ref", (customer_id,))]
        return ok([_summary(conn, db.booking(conn, r)) for r in refs])


@tool("read", "Policy-as-code check. Returns eligible, requires_human, team, outcome, options, every condition "
      "evaluated (state these qualifications to the customer), exceptions applied and rule ids. "
      "Pass the customer's latest message in context.message so bereavement / fraud / legal exceptions are caught.",
      {"booking_ref": {"type": "string"},
       "request_type": {"type": "string", "enum": list(policy.REQUEST_TYPES)},
       "context": {"type": "object", "properties": {
           "message": {"type": "string"}, "new_flight_id": {"type": "string"},
           "lost_access": {"type": "boolean", "description": "customer lost access to the old email/phone"},
           "exceptions": {"type": "array", "items": {"type": "string", "enum": ["bereavement", "fraud", "legal"]}}}}},
      ["booking_ref", "request_type"])
def check_eligibility(booking_ref: str, request_type: str, context: dict | None = None) -> dict:
    with _conn() as conn:
        b = _booking(conn, booking_ref)
        if request_type not in policy.REQUEST_TYPES:
            raise ToolError(f"BAD_ARGS: request_type must be one of {policy.REQUEST_TYPES}")
        return ok(policy.evaluate(b, request_type, _ctx(conn, b, context), db.now(conn)))


@tool("read", "Exact amounts (deterministic, line items with rule ids). These are the ONLY numbers you may tell "
      "the customer. Fails with NOT_ELIGIBLE when policy does not allow the action. Actions: cancel (voluntary), "
      "change / same_day_change (params.new_flight_id), rebook_disruption (params.new_flight_id), refund_disruption "
      "(schedule change >= 90 min, airline cancellation, delay >= 5 h), delay_compensation (params.choice credit|miles).",
      {"booking_ref": {"type": "string"}, "action": {"type": "string", "enum": list(policy.QUOTE_ACTIONS)},
       "params": {"type": "object", "properties": {"new_flight_id": {"type": "string"},
                                                   "choice": {"type": "string", "enum": ["credit", "miles"]},
                                                   "message": {"type": "string"}}}},
      ["booking_ref", "action"])
def quote_amount(booking_ref: str, action: str, params: dict | None = None) -> dict:
    with _conn() as conn:
        b = _booking(conn, booking_ref)
        return ok(_quote(conn, b, action, params))


def _quote(conn, b: dict, action: str, params: dict | None) -> dict:
    p = dict(params or {})
    if action not in policy.QUOTE_ACTIONS:
        raise ToolError(f"BAD_ARGS: action must be one of {policy.QUOTE_ACTIONS}")
    ctx = _ctx(conn, b, {"message": p.get("message", ""), "new_flight_id": p.get("new_flight_id")})
    if "new_flight" in ctx:
        p["new_flight"] = ctx["new_flight"]
    try:
        return policy.quote(b, action, p, ctx, db.now(conn))
    except policy.NotEligible as e:
        raise ToolError(f"NOT_ELIGIBLE: {e}", data=e.result) from None
    except ValueError as e:
        raise ToolError(f"BAD_ARGS: {e}") from None


_search_backend = None


def set_search_backend(fn):
    """fn(query) -> list[passage dict]; tests install a fake, None restores the RAG index."""
    global _search_backend
    _search_backend = fn


def _rag_search(query: str) -> list[dict]:
    from rag.pipeline import _defaults  # lazy: embeds the corpus once (disk-cached, ~$0.0003)
    index = _defaults()[0]
    try:
        rows = index.search(query, top_k=8)
    except Exception:  # noqa: BLE001 - query embedding unavailable (API down / budget): BM25 alone still works
        rows = [index.by_id[c] for c in index.keyword(query, 8)]
    rows = [r for r in rows if not r.get("superseded")] or rows
    return [{"doc_id": r["doc_id"], "title": r["title"], "section": r.get("section"), "updated": r.get("updated"),
             "text": r["text"][:900]} for r in rows[:5]]


@tool("read", "Search the SkyDesk help center (policies, manuals). Returns passages with doc ids to cite. "
      "Use for general questions; use check_eligibility / quote_amount for a specific booking.",
      {"query": {"type": "string"}}, ["query"])
def search_help_center(query: str) -> dict:
    if not (query or "").strip():
        raise ToolError("BAD_ARGS: empty query")
    return ok((_search_backend or _rag_search)(query.strip()[:1000]))


# ---------------------------------------------------------------- approvals (graph / human UI, not planner tools)

WRITE_ACTIONS = ("rebook_flight", "issue_refund", "issue_voucher", "waive_fee", "update_contact", "cancel_booking")


def request_approval(action: str, target: str, params: dict | None = None, summary: str = "") -> dict:
    if action not in WRITE_ACTIONS:
        return err(f"BAD_ARGS: action must be one of {WRITE_ACTIONS}")
    aid = "APR-" + uuid.uuid4().hex[:10].upper()
    with _conn() as conn:
        conn.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, action, target.upper(), db.dumps(params or {}), summary, "pending", db.now(conn), None, None))
    return ok({"approval_id": aid, "status": "pending"})


def decide_approval(approval_id: str, approved: bool, approver: str) -> dict:
    with _conn() as conn:
        r = conn.execute("SELECT status FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not r:
            return err("APPROVAL_NOT_FOUND")
        if r["status"] != "pending":
            return err(f"APPROVAL_ALREADY_DECIDED: {r['status']}")
        status = "approved" if approved else "rejected"
        conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE approval_id=?",
                     (status, approver, db.now(conn), approval_id))
    return ok({"approval_id": approval_id, "status": status})


def _write(name: str, target: str, approval_id, idempotency_key, params: dict, body) -> dict:
    """Shared write guard. body(conn) -> result dict, or raises ToolError (rolled back)."""
    if not approval_id:
        raise ToolError("APPROVAL_REQUIRED: write actions run only with a human approval_id")
    if not idempotency_key:
        raise ToolError("IDEMPOTENCY_KEY_REQUIRED")
    target = target.upper()
    with _conn() as conn:
        prev = conn.execute("SELECT * FROM actions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if prev:
            if (prev["action"], prev["target"]) != (name, target):
                raise ToolError("IDEMPOTENCY_KEY_REUSED: key already used for a different action")
            return ok({**json.loads(prev["result"]), "duplicate": True})
        apr = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not apr:
            raise ToolError("APPROVAL_NOT_FOUND")
        if apr["status"] != "approved":
            raise ToolError(f"APPROVAL_NOT_GRANTED: approval is {apr['status']}")
        if (apr["action"], apr["target"]) != (name, target):
            raise ToolError(f"APPROVAL_MISMATCH: approval is for {apr['action']} on {apr['target']}")
        approved = json.loads(apr["params"])
        for k, v in params.items():
            if k in approved and approved[k] != v:
                raise ToolError(f"APPROVAL_MISMATCH: {k}={v!r} but the human approved {approved[k]!r}")
        result = body(conn, approved.get("expected") or {})
        conn.execute("UPDATE approvals SET status='used' WHERE approval_id=?", (approval_id,))
        conn.execute("INSERT INTO actions (idempotency_key, action, target, approval_id, params, result, created_at)"
                     " VALUES (?,?,?,?,?,?,?)", (idempotency_key, name, target, approval_id, db.dumps(params),
                                                 db.dumps(result), db.now(conn)))
        return ok(result)


def _expect(expected: dict, q: dict):
    """The human approved specific numbers; if the backend now computes different ones, stop."""
    for k, v in expected.items():
        if k in q and round(float(q[k]), 2) != round(float(v), 2):
            raise ToolError(f"PRICE_CHANGED: {k} is now {q[k]} but {v} was approved; re-quote and re-approve",
                            data={"quote": q})


def _voucher(conn, b, cents: int, reason: str, kind="travel_credit", miles=0) -> str:
    code = ("TC-" if kind == "travel_credit" else "MV-" if kind == "meal_voucher" else "SM-") + uuid.uuid4().hex[:6].upper()
    now = policy.ts(db.now(conn))
    months = policy.RULES["vouchers"]["validity_months"] if kind == "travel_credit" else 0
    expires = (now.replace(year=now.year + months // 12) if months else now.replace(hour=23, minute=59)).isoformat()
    conn.execute("INSERT INTO vouchers VALUES (?,?,?,?,?,?,?,?,?)",
                 (code, b["customer_id"], b["ref"], kind, cents, miles, now.isoformat(), expires, reason))
    return code


def _cents(x) -> int:
    return int(round(float(x) * 100))


def _apply_refund(conn, b: dict, q: dict, status: str) -> dict:
    cash, credit = _cents(q["refund_to_original_payment"]), _cents(q["travel_credit"])
    if cash:
        conn.execute("INSERT INTO payments (ref, kind, amount, method, created_at, note) VALUES (?,?,?,?,?,?)",
                     (b["ref"], "refund", cash, "card", db.now(conn), q["action"]))
    code = _voucher(conn, b, credit, f"{q['action']} ({q['outcome']})") if credit else None
    conn.execute("UPDATE bookings SET status=?, refunded=refunded+? WHERE ref=?",
                 (status, cash + credit, b["ref"]))
    conn.execute("UPDATE flights SET seats_left=seats_left+? WHERE flight_id=?", (b["passengers"], b["flight_id"]))
    return {"booking_ref": b["ref"], "status": status, "refunded_to_original_payment": q["refund_to_original_payment"],
            "travel_credit": q["travel_credit"], "travel_credit_code": code, "lines": q["lines"],
            "rule_ids": q["rule_ids"], "processing": "card refunds post within 7 business days"}


# ---------------------------------------------------------------- WRITE tools

_W = {"approval_id": {"type": "string", "description": "id of the HUMAN-approved request"},
      "idempotency_key": {"type": "string", "description": "unique per intended action; reuse it when retrying"}}


@tool("write", "Move the booking to another flight (voluntary change, same-day change or free disruption rebook; "
      "the tool picks the policy). Requires human approval.",
      {"booking_ref": {"type": "string"}, "new_flight_id": {"type": "string"}, **_W},
      ["booking_ref", "new_flight_id", "approval_id", "idempotency_key"])
def rebook_flight(booking_ref: str, new_flight_id: str, approval_id: str, idempotency_key: str) -> dict:
    def body(conn, expected):
        b = _booking(conn, booking_ref)
        _require_verified(conn, b["ref"])
        nf = _flight_or_fail(conn, new_flight_id)
        now, dep = policy.ts(db.now(conn)), policy.ts(b["departs_at"])
        if policy.disruption(b):
            action = "rebook_disruption"
        elif timedelta(0) < dep - now <= timedelta(hours=24) and policy.ts(nf["departs_at"]).date() == dep.date():
            action = "same_day_change"
        else:
            action = "change"
        q = _quote(conn, b, action, {"new_flight_id": new_flight_id})
        _expect(expected, q)
        if conn.execute("UPDATE flights SET seats_left=seats_left-? WHERE flight_id=? AND seats_left>=?",
                        (b["passengers"], nf["flight_id"], b["passengers"])).rowcount != 1:
            raise ToolError("SOLD_OUT: no seats left on that flight", retryable=False)
        conn.execute("UPDATE flights SET seats_left=seats_left+? WHERE flight_id=?", (b["passengers"], b["flight_id"]))
        collect = _cents(q["collect_from_customer"])
        if collect:
            conn.execute("INSERT INTO payments (ref, kind, amount, method, created_at, note) VALUES (?,?,?,?,?,?)",
                         (b["ref"], "charge", collect, "card", db.now(conn), action))
        code = _voucher(conn, b, _cents(q["travel_credit"]), action) if q["travel_credit"] else None
        new_base = nf["fares"][b["fare_class"]] * b["passengers"] if action == "change" else b["base_fare"]
        conn.execute("""UPDATE bookings SET flight_id=?, status='confirmed', schedule_change_minutes=0,
                        base_fare=? WHERE ref=?""", (nf["flight_id"], max(new_base, 0), b["ref"]))
        return {"booking_ref": b["ref"], "policy": action, "old_flight_id": b["flight_id"],
                "new_flight_id": nf["flight_id"], "departs_at": nf["departs_at"],
                "charged": q["collect_from_customer"], "travel_credit": q["travel_credit"],
                "travel_credit_code": code, "lines": q["lines"], "rule_ids": q["rule_ids"]}
    return _write("rebook_flight", booking_ref, approval_id, idempotency_key, {"new_flight_id": new_flight_id}, body)


@tool("write", "Refund to the original payment and cancel the booking. amount is the quoted "
      "refund_to_original_payment, or less if the approver lowered it (never more: refund cap). Uses refund_disruption when SkyDesk caused it, "
      "otherwise the voluntary cancel rules. Requires human approval.",
      {"booking_ref": {"type": "string"}, "amount": {"type": "number"}, **_W},
      ["booking_ref", "amount", "approval_id", "idempotency_key"])
def issue_refund(booking_ref: str, amount: float, approval_id: str, idempotency_key: str) -> dict:
    def body(conn, expected):
        b = _booking(conn, booking_ref)
        _require_verified(conn, b["ref"])
        delayed5 = policy.tier(b["delay_minutes"] or 0).get("alt_refund")
        action = "refund_disruption" if policy.disruption(b) or delayed5 else "cancel"
        q = _quote(conn, b, action, {})
        _expect(expected, q)
        if q["refund_to_original_payment"] <= 0:
            raise ToolError("NO_CASH_REFUND: this fare returns travel credit only; use cancel_booking", data={"quote": q})
        amt = round(float(amount), 2)
        if not 0 < amt <= q["refund_to_original_payment"]:  # a human may approve less (edit), never more
            raise ToolError(f"REFUND_AMOUNT_MISMATCH: policy allows at most {q['refund_to_original_payment']} "
                            f"(cap {q['refund_cap']}), got {amount}", data={"quote": q})
        return _apply_refund(conn, b, {**q, "refund_to_original_payment": amt}, "refunded")
    return _write("issue_refund", booking_ref, approval_id, idempotency_key, {"amount": amount}, body)


@tool("write", "Cancel the booking under the voluntary fare rules (refund, taxes-only refund or travel credit, as "
      "quote_amount action=cancel says). Requires human approval.",
      {"booking_ref": {"type": "string"}, **_W}, ["booking_ref", "approval_id", "idempotency_key"])
def cancel_booking(booking_ref: str, approval_id: str, idempotency_key: str) -> dict:
    def body(conn, expected):
        b = _booking(conn, booking_ref)
        _require_verified(conn, b["ref"])
        q = _quote(conn, b, "cancel", {})
        _expect(expected, q)
        return _apply_refund(conn, b, q, "cancelled")
    return _write("cancel_booking", booking_ref, approval_id, idempotency_key, {}, body)


@tool("write", "Issue delay compensation (travel credit, SkyMiles or meal voucher) exactly as quoted by "
      "quote_amount action=delay_compensation. Goodwill credits are NOT allowed here (human only). Requires approval.",
      {"booking_ref": {"type": "string"}, "amount": {"type": "number", "description": "USD; 0 for miles"},
       "kind": {"type": "string", "enum": ["travel_credit", "meal_voucher", "miles"]},
       "reason": {"type": "string", "enum": ["delay_compensation"]}, **_W},
      ["booking_ref", "amount", "kind", "approval_id", "idempotency_key"])
def issue_voucher(booking_ref: str, amount: float, kind: str, approval_id: str, idempotency_key: str,
                  reason: str = "delay_compensation") -> dict:
    def body(conn, expected):
        if reason != "delay_compensation":
            raise ToolError("NOT_ALLOWED: only delay compensation can be issued by the assistant; goodwill and "
                            "other credits need a human agent (WVR-001)")
        b = _booking(conn, booking_ref)
        _require_verified(conn, b["ref"])
        if conn.execute("SELECT 1 FROM vouchers WHERE ref=? AND reason='delay_compensation'", (b["ref"],)).fetchone():
            raise ToolError("ALREADY_COMPENSATED: delay compensation was already issued for this booking (DLY-003)")
        q = _quote(conn, b, "delay_compensation", {"choice": "miles" if kind == "miles" else "credit"})
        _expect(expected, q)
        due = {"travel_credit": q["travel_credit"], "meal_voucher": q["meal_voucher"], "miles": 0}[kind]
        if (kind == "miles" and not q["skymiles"]) or (kind != "miles" and (not due or round(float(amount), 2) != due)):
            raise ToolError(f"AMOUNT_MISMATCH: policy allows {kind}={due if kind != 'miles' else q['skymiles']}",
                            data={"quote": q})
        if kind == "miles":
            conn.execute("UPDATE customers SET skymiles_balance=skymiles_balance+? WHERE customer_id=?",
                         (q["skymiles"], b["customer_id"]))
        code = _voucher(conn, b, _cents(due), "delay_compensation", kind, q["skymiles"] if kind == "miles" else 0)
        return {"booking_ref": b["ref"], "kind": kind, "amount": due, "skymiles": q["skymiles"] if kind == "miles" else 0,
                "code": code, "lines": q["lines"], "rule_ids": q["rule_ids"]}
    return _write("issue_voucher", booking_ref, approval_id, idempotency_key, {"amount": amount, "kind": kind}, body)


@tool("write", "Waive the change fee on a booking (once per booking). Only a human may decide this (WVR-001): "
      "the approval IS the supervisor's decision.",
      {"booking_ref": {"type": "string"}, "fee_type": {"type": "string", "enum": ["change_fee"]}, **_W},
      ["booking_ref", "fee_type", "approval_id", "idempotency_key"])
def waive_fee(booking_ref: str, fee_type: str, approval_id: str, idempotency_key: str) -> dict:
    def body(conn, expected):
        b = _booking(conn, booking_ref)
        _require_verified(conn, b["ref"])
        if fee_type != "change_fee":
            raise ToolError("BAD_ARGS: only change_fee can be waived")
        if b["fee_waived"]:
            raise ToolError("ALREADY_WAIVED: a change fee was already waived on this booking")
        if b["dispute_open"]:
            raise ToolError("FROZEN: card dispute open -> Payments Integrity")
        conn.execute("UPDATE bookings SET fee_waived=1 WHERE ref=?", (b["ref"],))
        return {"booking_ref": b["ref"], "waived": fee_type, "rule_ids": ["WVR-001", "CHG-001"]}
    return _write("waive_fee", booking_ref, approval_id, idempotency_key, {"fee_type": fee_type}, body)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\- ]{6,18}[0-9]$")


@tool("write", "Change the email and/or phone on a verified customer's profile. The confirmation code goes to the "
      "EXISTING contact (VER-002). Requires human approval.",
      {"customer_id": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}, **_W},
      ["customer_id", "approval_id", "idempotency_key"])
def update_contact(customer_id: str, approval_id: str, idempotency_key: str, email: str | None = None,
                   phone: str | None = None) -> dict:
    def body(conn, expected):
        if not _customer_verified(conn, customer_id):
            raise ToolError("NOT_VERIFIED: verify the customer first")
        if not email and not phone:
            raise ToolError("BAD_ARGS: give a new email or phone")
        if email and not EMAIL_RE.match(email):
            raise ToolError("BAD_ARGS: invalid email")
        if phone and not PHONE_RE.match(phone):
            raise ToolError("BAD_ARGS: invalid phone")
        old = conn.execute("SELECT email, phone FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
        conn.execute("UPDATE customers SET email=COALESCE(?, email), phone=COALESCE(?, phone) WHERE customer_id=?",
                     (email, phone, customer_id))
        mask = lambda s: (s[:2] + "***" + s[-4:]) if s else None  # noqa: E731
        return {"customer_id": customer_id, "email_updated": bool(email), "phone_updated": bool(phone),
                "confirmation_sent_to": mask(old["email"]), "rule_ids": ["VER-001", "VER-002"]}
    return _write("update_contact", customer_id, approval_id, idempotency_key,
                  {k: v for k, v in (("email", email), ("phone", phone)) if v}, body)


# ---------------------------------------------------------------- HANDOFF

@tool("handoff", "Hand the case to a human team and stop. Teams: care (bereavement), payments (fraud, chargeback, "
      "verification lock), customer_relations (legal, regulator, media, injury, discrimination), contact_center "
      "(anything the assistant may not do or cannot do safely, e.g. waivers, goodwill, repeated tool failures).",
      {"reason": {"type": "string"}, "team": {"type": "string", "enum": list(policy.RULES["teams"])},
       "summary": {"type": "string", "description": "one neutral paragraph for the receiving team"},
       "booking_ref": {"type": "string"}},
      ["reason", "team", "summary"])
def escalate_to_human(reason: str, team: str, summary: str, booking_ref: str | None = None) -> dict:
    t = policy.RULES["teams"].get(team)
    if not t:
        raise ToolError(f"BAD_ARGS: team must be one of {list(policy.RULES['teams'])}")
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] + 1
        tid = f"ESC-{n:05d}"
        conn.execute("INSERT INTO escalations VALUES (?,?,?,?,?,?)",
                     (tid, team, reason, summary, (booking_ref or "").upper() or None, db.now(conn)))
    return ok({"ticket_id": tid, "team": t["name"], "expected_response": t["sla"], "resolution_code": t["code"]})


READ_TOOLS = [n for n, t in TOOLS.items() if t["kind"] == "read"]
WRITE_TOOLS = [n for n, t in TOOLS.items() if t["kind"] == "write"]
assert set(WRITE_TOOLS) == set(WRITE_ACTIONS)
