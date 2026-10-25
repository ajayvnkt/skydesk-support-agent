"""SkyDesk policy as code: eligibility and deterministic money math.

Pure functions over a booking dict (the shape db.booking() returns: money in integer cents,
times as ISO-8601 UTC strings). No LLM, no DB, no clock: `now` is always passed in, so every
fare-class x request-type x timing case is directly testable.

evaluate()  -> every condition evaluated, every exception applied, the outcome, rule ids.
quote()     -> line-item amounts for an eligible request; raises NotEligible otherwise.
               These numbers are the ONLY numbers the agent may state.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml

RULES = yaml.safe_load((Path(__file__).with_name("policy_rules.yaml")).read_text())
FARES = RULES["fare_classes"]
ACTIVE = ("confirmed", "delayed", "schedule_changed", "airline_cancelled")
REQUEST_TYPES = ("cancel", "change", "same_day_change", "schedule_change", "delay_compensation",
                 "airline_cancellation", "update_contact", "waive_fee", "goodwill", "extend_credit")
HUMAN_ONLY = ("waive_fee", "goodwill", "extend_credit")  # WVR-001: the assistant may not decide
QUOTE_ACTIONS = ("cancel", "change", "same_day_change", "rebook_disruption", "refund_disruption",
                 "delay_compensation")


class NotEligible(ValueError):
    def __init__(self, result: dict):
        self.result = result
        super().__init__("; ".join(result["reasons"]) or "not eligible")


def ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def usd(cents: int) -> float:
    return round(cents / 100, 2)


def detect_exceptions(text: str) -> list[str]:
    t = (text or "").lower()
    return [k for k, e in RULES["exceptions"].items()
            if any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in e["keywords"])]


def tier(minutes: int) -> dict:
    for t in RULES["delay"]["tiers"]:
        if minutes >= t["min_minutes"] and (t["max_minutes"] is None or minutes < t["max_minutes"]):
            return t
    raise ValueError(minutes)


def disruption(b: dict) -> str | None:
    """The airline-caused situation that overrides the fare rules, if any."""
    if b["status"] == "airline_cancelled":
        return "airline_cancellation"
    if (b.get("schedule_change_minutes") or 0) >= RULES["schedule_change"]["threshold_minutes"]:
        return "schedule_change"
    return None


def evaluate(b: dict, request_type: str, context: dict | None = None, now=None) -> dict:
    """context: verified (set by the tool layer from the DB, never by the LLM), message (customer
    text, scanned for exceptions), exceptions (explicit list), new_flight (dict), lost_access."""
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"unknown request_type {request_type!r}; one of {REQUEST_TYPES}")
    ctx, now = context or {}, ts(now)
    conds, exc, options = [], [], []
    outcome = None

    def cond(rule_id, text, passed, detail="", required=True):
        conds.append({"rule_id": rule_id, "condition": text, "passed": bool(passed),
                      "required": required, "detail": detail})
        return bool(passed)

    def add_exc(kind, detail):
        e = RULES["exceptions"][kind]
        if kind not in [x["exception"] for x in exc]:
            exc.append({"exception": kind, "rule_id": e["rule_id"], "team": e["team"], "detail": detail})

    # ---- exceptions first: they decide who decides ----
    for kind in detect_exceptions(ctx.get("message", "")):
        add_exc(kind, "mentioned by the customer")
    for kind in ctx.get("exceptions") or []:
        if kind in RULES["exceptions"]:
            add_exc(kind, "flagged in context")
    if b.get("dispute_open"):
        add_exc("fraud", "a card dispute/chargeback is open on this booking: refunds and changes are frozen")
    locked = (b.get("verify_failures") or 0) >= RULES["verification"]["max_failures"]
    cond("VER-003", "Booking is not locked by failed verification attempts", not locked,
         f"{b.get('verify_failures') or 0} failed attempts")
    cond("VER-001", "Customer verified (booking reference + last name + card last 4)", ctx.get("verified"))

    dep, fc = ts(b["departs_at"]), b["fare_class"]
    fare = FARES[fc]
    active = b["status"] in ACTIVE
    agency = b.get("channel") == "agency"
    dis = disruption(b)
    nf = ctx.get("new_flight")

    def new_flight_ok(same_day=False, window_days=None):
        if not nf:
            return
        cond("FLT-001", "New flight is on the same route", (nf["origin"], nf["dest"]) == (b["origin"], b["dest"]),
             f"{nf['origin']}-{nf['dest']}")
        cond("FLT-002", "New flight has seats available", nf["seats_left"] > 0, f"{nf['seats_left']} seats")
        cond("FLT-003", "New flight is a different, future flight",
             nf["flight_id"] != b["flight_id"] and ts(nf["departs_at"]) > now)
        if same_day:
            cond("SDC-001", "New flight departs the same day as the original",
                 ts(nf["departs_at"]).date() == dep.date())
        if window_days is not None:
            delta = abs((ts(nf["departs_at"]).date() - dep.date()).days)
            cond("SCH-001", f"New flight departs within {window_days} days of the original date",
                 delta <= window_days, f"{delta} days apart")

    if request_type in ("cancel", "change", "same_day_change", "schedule_change", "airline_cancellation"):
        cond("BKG-001", "Booking is active", active, b["status"])
    if request_type in ("cancel", "change", "same_day_change"):
        cond("REF-006", "Booked directly with SkyDesk (agency tickets are serviced by the agency)",
             not agency, b.get("channel", "direct"))

    if request_type == "cancel":
        cond("REF-005", "Cancelled before the scheduled departure (otherwise a no-show)", now < dep,
             f"departs {dep.isoformat()}")
        within = now - ts(b["booked_at"]) <= timedelta(hours=RULES["risk_free"]["window_hours"])
        lead = dep - ts(b["booked_at"]) >= timedelta(days=RULES["risk_free"]["min_days_before_departure"])
        rf = [cond("R24-001", "Cancelled within 24 hours of booking", within, b["booked_at"], required=False),
              cond("R24-002", "Booked at least 7 days before departure", lead, required=False),
              cond("R24-003", "Booked directly (not via an agency)", not agency, required=False)]
        if dis:
            outcome = "full_refund"
            cond("SCH-001" if dis == "schedule_change" else "CXL-002",
                 "Airline disruption entitles every fare class to a full refund", True, dis, required=False)
        elif all(rf):
            outcome = "full_refund_risk_free"
        else:
            outcome = {"taxes_only": "taxes_refund", "credit_minus_fee": "travel_credit_minus_fee",
                       "full": "full_refund"}[fare["voluntary_refund"]]
            cond({"taxes_refund": "REF-007", "travel_credit_minus_fee": "REF-002", "full_refund": "REF-001"}[outcome],
                 f"{fc} voluntary cancellation rule", True, outcome, required=False)

    elif request_type == "change":
        cond("CHG-004", "Fare class allows changes (SkyLite cannot be changed)", fare["change_fee"] is not None, fc)
        cond("CHG-003", "At least 2 hours before the scheduled departure",
             dep - now >= timedelta(hours=RULES["change"]["min_hours_before_departure"]),
             f"{(dep - now).total_seconds() / 3600:.1f} h to departure")
        new_flight_ok()
        outcome = "change_with_fee"

    elif request_type == "same_day_change":
        cond("SDC-001", "Fare class allows same-day changes (not SkyLite)", fare["same_day_fee"] is not None, fc)
        cond("SDC-001", "Within 24 hours before departure and before departure",
             timedelta(0) < dep - now <= timedelta(hours=RULES["same_day"]["window_hours_before_departure"]),
             f"{(dep - now).total_seconds() / 3600:.1f} h to departure")
        new_flight_ok(same_day=True)
        outcome = "same_day_change"

    elif request_type == "schedule_change":
        mins = b.get("schedule_change_minutes") or 0
        cond("SCH-001", "Schedule changed by 90 minutes or more",
             mins >= RULES["schedule_change"]["threshold_minutes"], f"{mins} minutes")
        cond("SCH-002", "Option chosen before the original departure time", now < dep)
        new_flight_ok(window_days=RULES["schedule_change"]["rebook_window_days"])
        outcome, options = "free_rebook_or_full_refund", ["accept_new_time", "rebook_free", "refund_full"]

    elif request_type == "airline_cancellation":
        cond("CXL-002", "Flight was cancelled by SkyDesk", b["status"] == "airline_cancelled", b["status"])
        controllable = b.get("disruption_cause") == "controllable"
        within = b.get("disrupted_at") and dep - ts(b["disrupted_at"]) <= timedelta(
            days=RULES["airline_cancellation"]["window_days"])
        cond("CXL-001", "Cause within SkyDesk's control", controllable, b.get("disruption_cause"), required=False)
        cond("CXL-001", "Cancelled within 14 days of departure", within, required=False)
        new_flight_ok()
        if controllable and within:
            outcome, options = "rebook_plus_credit_or_refund", ["rebook_free_plus_credit", "refund_full"]
        else:
            outcome, options = "rebook_or_refund", ["rebook_free", "refund_full"]

    elif request_type == "delay_compensation":
        mins = b.get("delay_minutes") or 0
        t = tier(mins)
        cond("DLY-001", "Delay cause within SkyDesk's control", b.get("disruption_cause") == "controllable",
             b.get("disruption_cause") or "none")
        cond("DLY-002", "Arrival delay of at least 2 hours", t["kind"] != "none", f"{mins} minutes")
        arr = ts(b["arrives_at"])
        cond("DLY-004", "Claimed within 30 days of the flight",
             now - arr <= timedelta(days=RULES["delay"]["claim_days"]), f"arrived {arr.date()}")
        cond("DLY-003", "Tiers are not cumulative: the single tier for the full delay applies", True,
             f"{t['min_minutes']}-{t['max_minutes'] or 'inf'} min tier", required=False)
        outcome = t["kind"]
        options = [t["kind"]] + (["miles"] if t.get("alt_miles") else []) + (
            ["refund_full"] if t.get("alt_refund") and b["status"] in ACTIVE else [])

    elif request_type == "update_contact":
        cond("VER-002", "One-time code goes to the EXISTING email/phone (customer still has access)",
             not ctx.get("lost_access"), "lost access" if ctx.get("lost_access") else "")
        outcome = "update_contact"

    requires_human = bool(exc) or request_type in HUMAN_ONLY or (request_type == "update_contact" and ctx.get("lost_access")) or locked
    team = (exc[0]["team"] if exc else "payments" if locked else "contact_center" if requires_human else None)
    if request_type in HUMAN_ONLY:
        cond("WVR-001", "Automated assistant may not decide waivers, goodwill or credit extensions", False)
    failed = [c for c in conds if c["required"] and not c["passed"]]
    reasons = [f"{c['rule_id']}: {c['condition']} -- not met" + (f" ({c['detail']})" if c["detail"] else "")
               for c in failed]
    reasons += [f"{e['rule_id']}: {e['exception']} -> {RULES['teams'][e['team']]['name']} must decide" for e in exc]
    rule_ids = sorted({c["rule_id"] for c in conds} | {e["rule_id"] for e in exc})
    return {"booking_ref": b["ref"], "request_type": request_type, "fare_class": fc,
            "eligible": not failed and not requires_human, "requires_human": requires_human,
            "team": RULES["teams"][team]["name"] if team else None, "team_key": team,
            "outcome": outcome, "options": options, "reasons": reasons, "conditions": conds,
            "exceptions_applied": exc, "rule_ids": rule_ids}


# ---------------------------------------------------------------- money (integer cents only)

def _refund_full(b: dict, include_extras: bool, rule: str) -> dict:
    """Full refund of the unused ticket, split cash vs credit, clamped to the cap (REF-003/004)."""
    lines = [("Base fare", b["base_fare"], rule), ("Taxes and airport fees", b["taxes"], rule)]
    if include_extras and b.get("ancillaries"):
        lines.append(("Seats and bags bought with the ticket", b["ancillaries"], "REF-008"))
    gross = sum(a for _, a, _ in lines)
    cap = max(0, b["base_fare"] + b["taxes"] + ((b.get("ancillaries") or 0) if include_extras else 0)
              - (b.get("refunded") or 0))
    if b.get("refunded"):
        lines.append(("Already refunded on this booking", -b["refunded"], "REF-003"))
    total = max(0, min(gross - (b.get("refunded") or 0), cap))
    credit_part = min(b.get("paid_with_credit") or 0, total)
    return {"lines": lines, "refund_c": total - credit_part, "credit_c": credit_part, "cap_c": cap,
            "rules": [rule, "REF-003"] + (["REF-004"] if credit_part else [])}


def quote(b: dict, action: str, params: dict | None = None, context: dict | None = None, now=None) -> dict:
    """Deterministic amounts with a line-item breakdown. action in QUOTE_ACTIONS.
    params: new_flight (dict with fares) for change/same_day_change/rebook_disruption;
    choice for delay_compensation ('credit' | 'miles' | 'refund')."""
    if action not in QUOTE_ACTIONS:
        raise ValueError(f"unknown action {action!r}; one of {QUOTE_ACTIONS}")
    p, ctx = params or {}, dict(context or {})
    if p.get("new_flight"):
        ctx["new_flight"] = p["new_flight"]
    req = {"rebook_disruption": disruption(b) or "schedule_change",
           "refund_disruption": disruption(b) or "delay_compensation"}.get(action, action)
    ev = evaluate(b, req, ctx, now)
    if not ev["eligible"]:
        raise NotEligible(ev)
    if action == "refund_disruption" and req == "delay_compensation" and "refund_full" not in ev["options"]:
        raise NotEligible({**ev, "eligible": False, "reasons": [
            "DLY-002: a full refund needs an arrival delay of 5 hours or more on a flight not yet taken"]})
    pax, fc = b["passengers"], b["fare_class"]
    fare = FARES[fc]
    out = {"lines": [], "refund_c": 0, "credit_c": 0, "collect_c": 0, "meal_c": 0, "miles": 0,
           "cap_c": None, "rules": []}

    def merge(r):
        out["lines"] += r["lines"]
        for k in ("refund_c", "credit_c", "cap_c"):
            out[k] = r[k] if k == "cap_c" else out[k] + r[k]
        out["rules"] += r["rules"]

    if action == "cancel":
        o = ev["outcome"]
        if o in ("full_refund", "full_refund_risk_free"):
            rule = "R24-001" if o == "full_refund_risk_free" else ("REF-001" if not disruption(b) else "CXL-002")
            merge(_refund_full(b, include_extras=True, rule=rule))
        elif o == "taxes_refund":
            r = _refund_full({**b, "base_fare": 0}, include_extras=False, rule="REF-007")
            r["lines"] = [l for l in r["lines"] if l[0] != "Base fare"]
            r["lines"].insert(0, ("Base fare (SkyLite, non-refundable)", 0, "REF-007"))
            merge(r)
        else:  # SkyClassic: credit = fare value - $100 per passenger, never below 0, never cash
            value = b["base_fare"] + b["taxes"] - (b.get("refunded") or 0)
            fee = min(fare["cancel_fee"] * 100 * pax, value)
            out["lines"] += [("Fare value (base fare + taxes)", value, "REF-002"),
                             (f"Cancellation fee ${fare['cancel_fee']} x {pax} passenger(s)", -fee, "REF-002")]
            out["credit_c"], out["cap_c"] = value - fee, value
            out["rules"] += ["REF-002", "REF-003", "VCH-001"]

    elif action in ("change", "same_day_change"):
        nf = p.get("new_flight") or {}
        if not nf:
            raise ValueError("new_flight is required to quote a change")
        if action == "same_day_change":
            fee = fare["same_day_fee"] * 100 * pax
            out["lines"].append((f"Same-day change fee ${fare['same_day_fee']} x {pax}", fee, "SDC-001"))
            out["collect_c"] = fee
            out["rules"] += ["SDC-001"]
        else:
            fee = 0 if b.get("fee_waived") else fare["change_fee"] * 100 * pax
            out["lines"].append((f"Change fee ${fare['change_fee']} x {pax} passenger(s)"
                                 + (" (waived with approval)" if b.get("fee_waived") else ""), fee, "CHG-001"))
            new_total = nf["fares"][fc] * pax
            diff = new_total - b["base_fare"]
            out["lines"].append((f"Fare difference (new {usd(new_total)} - paid {usd(b['base_fare'])})", diff, "CHG-002"))
            if diff >= 0:
                out["collect_c"] = fee + diff
            else:
                out["collect_c"] = fee
                if fare["lower_fare_difference"] == "travel_credit":
                    out["credit_c"] = -diff
                else:
                    out["lines"].append(("Lower fare difference not refunded on " + fc, -diff, "CHG-002"))
            out["rules"] += ["CHG-001", "CHG-002"]

    elif action == "rebook_disruption":
        out["lines"].append(("Change fee and fare difference waived (airline disruption)", 0,
                             "SCH-001" if disruption(b) == "schedule_change" else "CXL-002"))
        if ev["outcome"] == "rebook_plus_credit_or_refund":
            c = RULES["airline_cancellation"]["controllable_credit"] * 100 * pax
            out["lines"].append((f"Controllable cancellation travel credit ${c // 100 // pax} x {pax}", c, "CXL-001"))
            out["credit_c"] = c
        out["rules"] += [l[2] for l in out["lines"]]

    elif action == "refund_disruption":
        merge(_refund_full(b, include_extras=True,
                           rule={"delay_compensation": "DLY-003", "schedule_change": "SCH-001"}.get(req, "CXL-002")))

    elif action == "delay_compensation":
        t, choice = tier(b["delay_minutes"]), p.get("choice", "credit")
        if t["kind"] == "meal_voucher":
            out["meal_c"] = t["amount"] * 100 * pax
            out["lines"].append((f"Meal voucher ${t['amount']} x {pax}", out["meal_c"], "DLY-002"))
        elif choice == "miles":
            if not t.get("alt_miles"):
                raise ValueError("SkyMiles are only offered for the 3h-<5h tier")
            out["miles"] = t["alt_miles"] * pax
            out["lines"].append((f"{t['alt_miles']:,} SkyMiles x {pax}", 0, "DLY-002"))
        elif choice == "refund":
            raise ValueError("use action refund_disruption for the 5h+ refund option")
        else:
            out["credit_c"] = t["amount"] * 100 * pax
            out["lines"].append((f"Delay travel credit ${t['amount']} x {pax}", out["credit_c"], "DLY-002"))
        out["rules"] += ["DLY-001", "DLY-002", "DLY-003"]

    if out["cap_c"] is not None:
        assert out["refund_c"] + out["credit_c"] <= out["cap_c"], "refund cap breached"
    return {
        "booking_ref": b["ref"], "action": action, "currency": "USD", "fare_class": fc, "passengers": pax,
        "lines": [{"label": l, "amount": usd(a), "rule_id": r} for l, a, r in out["lines"]],
        "refund_to_original_payment": usd(out["refund_c"]),
        "travel_credit": usd(out["credit_c"]),
        "collect_from_customer": usd(out["collect_c"]),
        "meal_voucher": usd(out["meal_c"]),
        "skymiles": out["miles"],
        "refund_cap": usd(out["cap_c"]) if out["cap_c"] is not None else None,
        "outcome": "full_refund" if action == "refund_disruption" else ev["outcome"], "conditions": [c for c in ev["conditions"] if c["passed"]],
        "rule_ids": sorted(set(out["rules"]) | set(ev["rule_ids"])),
    }
