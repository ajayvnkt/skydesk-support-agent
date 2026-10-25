"""Claim verification before any reply is sent (instructor feedback, Week 2).

Every money amount, percentage, date, duration/deadline, eligibility statement and fare-condition
sentence in the planner's draft must be backed by a tool result from THIS run (quote_amount,
check_eligibility, lookup_booking). A sentence with an unbacked claim is removed -- never kept -- and
the customer is told an agent will confirm it. When the removed number is the core of the answer (a
price question, or nothing useful left), the caller escalates instead.
"""
from __future__ import annotations

import re
from datetime import date

SOURCE_TOOLS = ("quote_amount", "check_eligibility", "lookup_booking", "list_customer_bookings",
                # a successful write's own result (amount moved, voucher code, new flight) is a backend fact too
                "rebook_flight", "issue_refund", "issue_voucher", "waive_fee", "update_contact", "cancel_booking")

MONEY_RE = re.compile(r"(?:[$€£]\s?(\d[\d,]*(?:\.\d+)?))|(?:\b(\d[\d,]*(?:\.\d+)?)\s?(?:USD|EUR|GBP|dollars)\b)", re.I)
PCT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
MONTHS = {m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())}
MDY_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                    r"(?:,?\s+(\d{4}))?\b", re.I)
DMY_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
                    r"(?:,?\s+(\d{4}))?\b", re.I)
DUR_RE = re.compile(r"\b(\d+)\s?(?:-\s?)?(hours?|hrs?|days?|weeks?|months?)\b", re.I)
NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
COND_RE = re.compile(r"\b(only if|as long as|provided( that)?|subject to|must|required|requires|"
                     r"non-?refundable|refundable|change fee|cancell?ation fee|no fee|free of charge|"
                     r"eligible|qualif\w+|exception|waiv\w+|deadline|before departure)\b", re.I)
ELIG_YES_RE = re.compile(r"\b(you('re| are)|booking is|ticket is)\s+(\w+\s+)?eligible\b", re.I)
ELIG_NO_RE = re.compile(r"\b(not|isn't|aren't|ineligible)\b[^.]{0,20}\beligible\b|\bineligible\b", re.I)
DONE_RE = re.compile(r"\b(has|have) been (refunded|rebooked|cancell?ed|issued|waived|updated|processed|changed)\b|"
                     r"\bI('ve| have) (refunded|rebooked|cancell?ed|issued|waived|updated|processed|changed)\b", re.I)
PRICE_Q_RE = re.compile(r"\b(how much|cost|price|fee|refund|charge|fare|pay|amount|credit|voucher|get back|"
                        r"compensat\w*|owed?)\b", re.I)
WRITE_TOOLS = ("rebook_flight", "issue_refund", "issue_voucher", "waive_fee", "update_contact", "cancel_booking")
STOP = set("this that with your have will from they there their about been were would could should into "
           "also just than then them what when where which while only must able does made make".split())


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def collect_facts(tool_results: list[dict]) -> dict:
    """Numbers, dates and words from successful source-tool results of this run."""
    nums: set[float] = set()
    dates: set[tuple] = set()
    words: set[str] = set()
    conditions: list[str] = []
    eligible: list[bool] = []

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            nums.add(round(float(v), 2))
        elif isinstance(v, str):
            for m in ISO_RE.finditer(v):
                dates.add((int(m[1]), int(m[2]), int(m[3])))
            for m in NUM_RE.finditer(v):
                n = _num(m[0])
                if n is not None:
                    nums.add(round(abs(n), 2))
            words.update(w for w in re.findall(r"[a-z]{4,}", v.lower()))
        elif isinstance(v, dict):
            for k, x in v.items():
                words.update(re.findall(r"[a-z]{4,}", str(k).lower()))
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    writes_ok = False
    for r in tool_results:
        # policy decisions in order: a check, or a quote (quote_amount refuses with NOT_ELIGIBLE when policy says no)
        if r.get("tool") == "quote_amount":
            if r.get("ok"):
                eligible.append(True)
            elif "NOT_ELIGIBLE" in str(r.get("error")):
                eligible.append(False)
        if r.get("tool") not in SOURCE_TOOLS or not r.get("ok"):
            continue
        writes_ok |= r["tool"] in WRITE_TOOLS
        walk(r.get("data"))
        if r["tool"] == "check_eligibility" and isinstance(r.get("data"), dict):
            d = r["data"]
            if isinstance(d.get("eligible"), bool):
                eligible.append(d["eligible"])
            for k in ("conditions", "reasons", "exceptions_applied"):
                conditions += [str(c) for c in d.get(k) or []]
    nums |= {round(n * 100, 2) for n in list(nums) if 0 < n < 1}  # 0.25 backs "25%"
    return {"numbers": nums, "dates": dates, "words": words, "conditions": conditions, "eligible": eligible,
            "writes_ok": writes_ok}


def _dates_in(s: str) -> list[tuple]:
    out = [(int(m[1]), int(m[2]), int(m[3])) for m in ISO_RE.finditer(s)]
    for m in MDY_RE.finditer(s):
        out.append((int(m[3]) if m[3] else None, MONTHS[m[1][:3].lower()], int(m[2])))
    for m in DMY_RE.finditer(s):
        out.append((int(m[3]) if m[3] else None, MONTHS[m[2][:3].lower()], int(m[1])))
    return out


def _date_ok(d: tuple, facts: set[tuple]) -> bool:
    y, mo, da = d
    return any(fm == mo and fd == da and (y is None or fy == y) for fy, fm, fd in facts)


def _sentence_claims(s: str, facts: dict, executed: bool, asked: set[float] = frozenset()) -> list[str]:
    """Unverified claims in one sentence (empty list = sentence is fine). `asked`: amounts the customer stated."""
    bad: list[str] = []
    nums = facts["numbers"]
    has = lambda v: v is not None and any(abs(v - n) < 0.006 for n in nums)  # noqa: E731
    elig = facts["eligible"][-1:]  # the latest policy decision decides (state changes, e.g. after a cancellation)
    # "you're not eligible for the $100 you asked for": echoing the customer's figure in a refusal that policy backs
    refusal = bool(ELIG_NO_RE.search(s)) and elig == [False]
    for m in MONEY_RE.finditer(s):
        v = _num(m[1] or m[2])
        if not has(v) and not (refusal and v in asked):
            bad.append(f"amount {m[0].strip()}")
    for m in PCT_RE.finditer(s):
        if not has(_num(m[1])):
            bad.append(f"percentage {m[0]}")
    for d in _dates_in(s):
        if not _date_ok(d, facts["dates"]):
            bad.append(f"date {d}")
    for m in DUR_RE.finditer(s):
        if not has(_num(m[1])):
            bad.append(f"deadline {m[0]}")
    if ELIG_NO_RE.search(s):
        if not elig or elig[0]:
            bad.append("eligibility (says not eligible)")
    elif ELIG_YES_RE.search(s) and (not elig or not elig[0]):
        bad.append("eligibility (says eligible)")
    # shortcut: any successful write in this conversation backs a "done" sentence; match verb->tool if it misfires
    if DONE_RE.search(s) and not (executed or facts.get("writes_ok")):
        bad.append("action claimed but not executed")
    if not bad and COND_RE.search(s):
        kws = {w for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in STOP}
        cond_words = set(re.findall(r"[a-z]{4,}", " ".join(facts["conditions"]).lower())) | facts["words"]
        if kws and len(kws & cond_words) / len(kws) < 0.5:
            bad.append("condition not backed by a policy check")
    return bad


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.strip()]


WITHHELD_NOTE = ("I couldn't confirm some of the details from our systems, so I've left them out; "
                 "a SkyDesk agent will confirm them with you.")


def verify_reply(draft: str, tool_results: list[dict], question: str = "",
                 executed: bool = False, exclude: set[float] = frozenset()) -> dict:
    """Returns {action: pass|withheld|escalate, reply, removed[], checked, core}.
    `reply` never contains an unverified claim. `exclude`: figures that are no longer true (a quote the approver
    edited down) even though a tool result still contains them."""
    facts = collect_facts(tool_results)
    facts["numbers"] = {n for n in facts["numbers"] if not any(abs(n - x) < 0.006 for x in exclude)}
    asked = {n for m in MONEY_RE.finditer(question or "") if (n := _num(m[1] or m[2])) is not None}
    kept, removed, checked = [], [], 0
    for s in split_sentences(draft):
        checked += bool(MONEY_RE.search(s) or PCT_RE.search(s) or _dates_in(s) or DUR_RE.search(s)
                        or COND_RE.search(s) or DONE_RE.search(s))
        bad = _sentence_claims(s, facts, executed, asked)
        (removed.append({"sentence": s, "claims": bad}) if bad else kept.append(s))
    money_removed = any(c.startswith(("amount", "percentage")) for r in removed for c in r["claims"])
    core = bool(removed) and (not kept or (money_removed and bool(PRICE_Q_RE.search(question)))
                              or any("eligibility" in c or "action claimed" in c
                                     for r in removed for c in r["claims"]))
    if not removed:
        return {"action": "pass", "reply": draft.strip(), "removed": [], "checked": checked, "core": False}
    if core:
        return {"action": "escalate", "reply": None, "removed": removed, "checked": checked, "core": True}
    return {"action": "withheld", "reply": " ".join(kept + [WITHHELD_NOTE]), "removed": removed,
            "checked": checked, "core": False}


if __name__ == "__main__":  # tiny self-check
    tr = [{"tool": "quote_amount", "ok": True, "data": {"total": 120.0, "breakdown": [{"fare": 150}, {"fee": -30}]}}]
    assert verify_reply("Your refund is $120.00.", tr, "how much refund?")["action"] == "pass"
    assert verify_reply("Your refund is $135.", tr, "how much refund?")["action"] == "escalate"
    r = verify_reply("Thanks for waiting. The bag fee is $35.", tr, "hello")
    assert r["action"] == "withheld" and "$35" not in r["reply"]
    print("ok", date.today())
