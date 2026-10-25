"""Keyword planner: a NO-MODEL baseline that drives the same graph, tools, approvals and faults as the
LLM planner. It exists to check the eval harness end to end at $0 and as a floor the LLM must beat.
It sees only the prompt messages (like the LLM) plus the customer's last name, which it can't parse.
shortcut: keyword intents, English only; it is a harness check, not a product planner."""
from __future__ import annotations

import json
import re
import uuid

REF_RE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d|[A-Z]{6}\b)[A-Z0-9]{6}\b")
CARD_RE = re.compile(r"(?<![\d@.])\b(\d{4})\b(?![\d@])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+\w")
FLIGHT_RE = re.compile(r"\b(SD\d{3})\b", re.I)
DATE_RE = re.compile(r"\b(Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)
DO_RE = re.compile(r"\b(yes|go ahead|do it|proceed|cancel it|cancel the booking|cancel my booking|cancel the "
                   r"cancellation|i'll take|please issue|issue it|put me on|refund me|change my email|submit|"
                   r"please change|please cancel|can you (waive|cancel|change|refund|move))\b", re.I)
QUESTION_RE = re.compile(r"\b(how much|what would|what do i|what compensation|just asking|would it cost|which)\b", re.I)
WRITES = ("rebook_flight", "issue_refund", "issue_voucher", "waive_fee", "update_contact", "cancel_booking")
MONTH = {"oct": 10, "nov": 11, "dec": 12}


def intent(t: str) -> str | None:
    t = t.lower()
    if "@" in t or "email" in t or "phone number" in t:
        return "contact"
    if "waive" in t:
        return "waive"
    if re.search(r"delay|late\b|compensation", t):
        return "delay"
    if FLIGHT_RE.search(t) or re.search(r"\b(move|change|switch)\b", t):
        return "change"
    if re.search(r"cancel|refund|money back|get back", t):
        return "cancel"
    return None


def _disrupted(b: dict) -> bool:
    return b.get("status") in ("airline_cancelled", "delayed") or (b.get("schedule_change_minutes") or 0) >= 90 \
        or (b.get("delay_minutes") or 0) >= 300


def _money(q: dict) -> str:
    parts = [f"{ln['label']}: ${ln['amount']:.2f}" for ln in q.get("lines") or []]
    for k, label in (("refund_to_original_payment", "back to your card"), ("travel_credit", "as travel credit"),
                     ("collect_from_customer", "to pay"), ("meal_voucher", "meal voucher")):
        if q.get(k):
            parts.append(f"${q[k]:.2f} {label}")
    if q.get("skymiles"):
        parts.append(f"{q['skymiles']:,} SkyMiles")
    return "; ".join(parts)


class RulesPlanner:
    def __init__(self, identity: dict):
        self.last_name = identity.get("last_name", "")
        self.cost = {"model": "rules", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_ms": 0.0}

    def __call__(self, messages: list[dict], schemas: list[dict]):
        return self.step(messages), dict(self.cost)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _history(messages):
        """[(name, args, result, in_this_turn)] in order."""
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        res = {m["tool_call_id"]: m for m in messages if m.get("role") == "tool"}
        out = []
        for i, m in enumerate(messages):
            for tc in m.get("tool_calls") or []:
                r = res.get(tc["id"])
                try:
                    r = json.loads(r["content"]) if r else None
                except (TypeError, ValueError):
                    r = None
                out.append((tc["function"]["name"], json.loads(tc["function"]["arguments"]), r, i > last_user))
        return out

    @staticmethod
    def call(name, **args):
        return {"content": None, "tool_calls": [{"id": f"rp_{uuid.uuid4().hex[:10]}", "name": name,
                                                 "arguments": json.dumps(args)}]}

    @staticmethod
    def say(text):
        return {"content": text, "tool_calls": []}

    # ------------------------------------------------------------------ policy
    def step(self, messages: list[dict]) -> dict:
        users = [m["content"] for m in messages if m.get("role") == "user"]
        text = users[-1]
        hist = self._history(messages)
        now = [h for h in hist if h[3]]
        it, do = intent(text), bool(DO_RE.search(text)) and not QUESTION_RE.search(text)
        if not it:
            it = next((intent(u) for u in reversed(users[:-1]) if intent(u)), None)
            do = do or bool(re.search(r"\b(ok|fine|sure|please)\b", text, re.I))
        verified = {h[1].get("booking_ref", "").upper() for h in hist
                    if h[0] == "verify_customer" and (h[2] or {}).get("ok") and (h[2]["data"] or {}).get("verified")}
        used_cards = {h[1].get("card_last4") for h in hist if h[0] == "verify_customer"}
        refs = [r for u in reversed(users) for r in REF_RE.findall(u) if r not in ("SYSTEM", "CANCEL", "REFUND")]
        ref = refs[0] if refs else None
        listed = next((h[2]["data"] for h in reversed(hist) if h[0] == "list_customer_bookings"
                       and (h[2] or {}).get("ok")), None)
        if it == "delay" and listed:
            ref = next((b["booking_ref"] for b in listed if (b.get("delay_minutes") or 0) >= 120), ref)
        if not ref:
            return self.say("Please give me your booking reference, last name and the last 4 digits of the card.")

        # 1. identity
        if ref not in verified:
            tried = [h for h in now if h[0] == "verify_customer" and h[1].get("booking_ref") == ref]
            if tried:
                return self.say("I couldn't verify those details. Please check the last name and the last 4 digits "
                                "of the card used to pay for this booking.")
            cards = [c for c in CARD_RE.findall(text) if c not in used_cards or not verified]
            if not cards:
                return self.say(f"Booking {ref} looks like the one. To open it, please tell me the last 4 digits of "
                                "the card used to pay for it.")
            return self.call("verify_customer", booking_ref=ref, last_name=self.last_name, card_last4=cards[0])

        # 2. booking
        booking = next((h[2]["data"] for h in reversed(hist) if h[0] == "lookup_booking"
                        and h[1].get("booking_ref") == ref and (h[2] or {}).get("ok")), None)
        if not booking:
            if any(h[0] == "lookup_booking" for h in now):
                return self.say("I couldn't load your booking right now. Please try again shortly.")
            return self.call("lookup_booking", booking_ref=ref)
        if it == "delay" and not listed and not (booking.get("delay_minutes") or 0):
            return self.call("list_customer_bookings", customer_id=booking["customer_id"])

        # 3. a write already ran / was refused this turn -> report it truthfully
        for name, args, r, _ in reversed(now):
            if name in WRITES and r is not None:
                if r.get("ok"):
                    d = r.get("data") or {}
                    nums = [f"{k.replace('_', ' ')}: " + (f"${v:.2f}" if isinstance(v, float) else str(v))
                            for k, v in d.items() if v and k in ("refunded_to_original_payment", "travel_credit",
                                                                 "amount", "charged", "new_flight_id",
                                                                 "travel_credit_code", "code", "waived")]
                    return self.say(f"Done: your request on {ref} has been processed. " + "; ".join(nums))
                why = "; ".join(r.get("reasons") or []) or r.get("note") or ""
                return self.say(f"That change was not made: {r.get('error')}. {why} Nothing was changed.")

        memory = next((m["content"] for m in messages if m.get("role") == "system"
                       and "Memory:" in (m.get("content") or "")), "")
        tool_for = {"cancel": "issue_refund" if _disrupted(booking) else "cancel_booking", "delay": "issue_voucher",
                    "change": "rebook_flight", "contact": "update_contact", "waive": "waive_fee"}
        action = {"cancel": "refund_disruption" if _disrupted(booking) else "cancel",
                  "delay": "delay_compensation",
                  "change": "rebook_disruption" if _disrupted(booking) else "change"}.get(it)
        params = {}
        if it == "change":
            fno = next((f.upper() for u in reversed(users) for f in FLIGHT_RE.findall(u)), None)
            day = next((f"2026{MONTH[m[0][:3].lower()]:02d}{int(m[1]):02d}" for u in reversed(users)
                        for m in DATE_RE.findall(u)), "")
            alts = [a for a in booking.get("alternatives") or [] if not fno or a["flight_no"] == fno]
            pick = next((a for a in alts if a["flight_id"].endswith(day)), alts[0] if alts else None)
            if not pick:
                return self.say("I couldn't find a matching flight with seats. Which flight would you like?")
            params = {"new_flight_id": pick["flight_id"]}
        if it == "delay":
            params = {"choice": "miles" if "miles" in text.lower() else "credit"}
        if it is None:
            done = [h for h in now if h[0] == "search_help_center"]
            if done and (done[-1][2] or {}).get("ok"):
                return self.say("From the help center: " + "; ".join(p["title"] for p in done[-1][2]["data"][:3]))
            return self.call("search_help_center", query=text)

        q = next((h[2] for h in reversed(hist) if h[0] == "quote_amount" and h[1].get("booking_ref") == ref
                  and h[1].get("action") == action and h[1].get("params", {}) == params), None)
        tool = tool_for[it]
        already = tool in memory and "executed" in memory and ref in memory
        if do and not already:
            args = {"booking_ref": ref}
            if it == "contact":
                m = EMAIL_RE.search(text)
                args = {"email": m[0]} if m else {}
            if it == "change":
                args["new_flight_id"] = params["new_flight_id"]
            if it == "delay":
                kind = "miles" if "miles" in text.lower() else "travel_credit"
                if q and (q.get("data") or {}).get("meal_voucher"):
                    kind = "meal_voucher"
                elif not q and action:
                    return self.call("quote_amount", booking_ref=ref, action=action, params=params)
                args["kind"] = kind
            if it == "waive":
                args["fee_type"] = "change_fee"
            return self.call(tool, **args)
        if not action:
            return self.say("I can do that once you confirm. Shall I go ahead?")
        if q is None or (not q.get("ok") and not any(h[0] == "quote_amount" for h in now)):
            return self.call("quote_amount", booking_ref=ref, action=action, params=params)
        if not q.get("ok"):
            return self.say(f"Under our policy that isn't available for {ref}: {q.get('error')}")
        prefix = f"Our records show {tool} on {ref} was already completed in an earlier conversation. " if already else ""
        return self.say(prefix + f"For {ref}: {_money(q['data'])}. Would you like me to go ahead?")
