"""The numbers in policy_rules.yaml must match the help-center docs the customer can read.

Parses the corpus tables/sentences directly, so editing either side without the other fails CI.
"""
import re
from pathlib import Path

import pytest

from skydesk.policy import FARES, RULES

CORPUS = Path(__file__).resolve().parent.parent / "corpus"


def doc(name: str) -> str:
    return (CORPUS / f"{name}.md").read_text()


def table(text: str, header_start: str) -> dict[str, list[str]]:
    """Markdown table whose header row starts with `| header_start` -> {first cell: other cells}."""
    lines = text.splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith(f"| {header_start}"))
    out = {}
    for l in lines[i + 2:]:
        if not l.startswith("|"):
            break
        cells = [c.strip() for c in l.strip("|").split("|")]
        out[cells[0]] = cells[1:]
    return out


def dollars(cell: str) -> int | None:
    m = re.search(r"\$(\d+)", cell)
    return int(m.group(1)) if m else (0 if cell.lower().startswith("free") else None)


def test_change_fees_match():
    t = table(doc("policy-change-fees"), "Fare class | Change fee")
    for fc in FARES:
        assert dollars(t[fc][0]) == FARES[fc]["change_fee"], fc
    assert "not permitted" in t["SkyLite"][0]
    assert "not refunded" in t["SkyClassic"][1] and FARES["SkyClassic"]["lower_fare_difference"] == "forfeit"
    for fc in ("SkyFlex", "SkyBusiness"):
        assert "travel credit" in t[fc][1] and FARES[fc]["lower_fare_difference"] == "travel_credit"
    assert f"at least **{RULES['change']['min_hours_before_departure']} hours before" in doc("policy-change-fees")


def test_same_day_fees_match():
    t = table(doc("policy-change-fees"), "Fare class | Same-day change")
    for fc in FARES:
        assert dollars(t[fc][0]) == FARES[fc]["same_day_fee"], fc
    assert f"From {RULES['same_day']['window_hours_before_departure']} hours before departure" in doc("policy-change-fees")


def test_refund_rules_match():
    text = doc("policy-refunds-cancellations")
    t = table(text, "Fare class | Cancel before departure")
    assert "Non-refundable" in t["SkyLite"][0] and FARES["SkyLite"]["voluntary_refund"] == "taxes_only"
    assert dollars(t["SkyClassic"][0]) == FARES["SkyClassic"]["cancel_fee"] == 100
    for fc in ("SkyFlex", "SkyBusiness"):
        assert "Full refund" in t[fc][0] and FARES[fc]["voluntary_refund"] == "full"
    assert f"minus a ${RULES['no_show_fee']} no-show fee" in text
    assert f"within {RULES['taxes_refund_months_after_travel']} months of the original travel date" in text


def test_risk_free_window_matches():
    text = doc("policy-24-hour-cancellation")
    assert f"within {RULES['risk_free']['window_hours']} hours of making it" in text
    assert f"at least {RULES['risk_free']['min_days_before_departure']} days before" in text


def test_schedule_change_threshold_matches():
    text = doc("policy-schedule-changes")
    assert f"| {RULES['schedule_change']['threshold_minutes']} minutes or more" in text
    assert f"Less than {RULES['schedule_change']['threshold_minutes']} minutes" in text
    assert f"within {RULES['schedule_change']['rebook_window_days']} days before or after" in text


def test_delay_tiers_match_current_policy_not_superseded():
    text = doc("policy-delay-compensation")
    rows = table(text, "Arrival delay")
    parsed = []
    for k, (v,) in rows.items():
        lo = 0 if k.startswith("Less") else int(re.match(r"(\d+) hours", k).group(1)) * 60
        parsed.append((lo, dollars(v) or 0, int(re.sub(r"\D", "", m.group(1))) if (m := re.search(r"([\d,]+) SkyMiles", v)) else None,
                       "full refund" in v))
    yaml_tiers = [(t["min_minutes"], t["amount"], t.get("alt_miles"), bool(t.get("alt_refund")))
                  for t in RULES["delay"]["tiers"]]
    assert parsed == yaml_tiers
    assert "under 3 hours" in text and "under 5 hours" in text  # upper bounds are exclusive
    assert f"within {RULES['delay']['claim_days']} days of the flight" in text
    old = doc("policy-delay-compensation-2024")
    assert "$150 travel credit" in old and 150 not in [t["amount"] for t in RULES["delay"]["tiers"]]


def test_airline_cancellation_matches():
    text = doc("policy-delay-compensation")
    c = RULES["airline_cancellation"]
    assert f"within {c['window_days']} days of departure" in text
    assert f"**plus** a ${c['controllable_credit']} travel credit" in text


def test_vouchers_goodwill_verification_match():
    v = doc("policy-vouchers-credits")
    assert f"valid for **{RULES['vouchers']['validity_months']} months" in v
    assert f"up to **{RULES['vouchers']['max_combined']} travel credits**" in v
    assert f"by **{RULES['vouchers']['extension_months']} months**" in v
    assert f"up to **${RULES['goodwill_max_frontline']}**" in doc("policy-agent-conduct")
    iv = doc("policy-identity-verification")
    assert f"After {RULES['verification']['max_failures']} failed verification attempts" in iv
    assert f"locked for {RULES['verification']['lock_hours']} hours" in iv


@pytest.mark.parametrize("team,doc_name,phrase", [
    ("payments", "policy-fraud-chargebacks", "**2 business days**"),
    ("customer_relations", "policy-legal-complaints", "**1 business day**"),
    ("care", "policy-bereavement", "24 hours a day"),
])
def test_escalation_teams_and_slas_match(team, doc_name, phrase):
    t = RULES["teams"][team]
    text = doc(doc_name)
    assert t["name"] in text and phrase in text
    assert phrase.strip("*").split(" business")[0] in t["sla"] or "24 hours a day" in t["sla"]
    assert t["code"] in doc("policy-agent-conduct")


def test_every_rule_cites_an_existing_doc():
    for rid, r in RULES["rules"].items():
        assert (CORPUS / f"{r['source']}.md").exists(), rid
    for kind, e in RULES["exceptions"].items():
        assert e["rule_id"] in RULES["rules"] and e["team"] in RULES["teams"]
