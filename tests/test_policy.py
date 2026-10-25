"""Fare conditions and policy exceptions, tested directly (instructor's Week 2 ask).

Every row is one qualification an answer could state; the engine must decide it without an LLM.
"""
from datetime import timedelta

import pytest

from conftest import NOW, flight
from skydesk import policy

V = {"verified": True}
FARES = ["SkyLite", "SkyClassic", "SkyFlex", "SkyBusiness"]
VOLUNTARY = {"SkyLite": "taxes_refund", "SkyClassic": "travel_credit_minus_fee",
             "SkyFlex": "full_refund", "SkyBusiness": "full_refund"}


def ev(b, rt, **ctx):
    return policy.evaluate(b, rt, {**V, **ctx}, NOW)


def failed(r):
    return {c["rule_id"] for c in r["conditions"] if c["required"] and not c["passed"]}


# ---------------------------------------------------------------- cancellation x fare x timing

TIMING = [  # booked_h_ago, dep_in_h, risk_free?
    ("inside 24h, departs in 20d", 5, 24 * 20, True),
    ("exactly 24h after booking", 24, 24 * 20, True),
    ("24h + 1 min after booking", 24 + 1 / 60, 24 * 20, False),
    ("inside 24h, booked exactly 7d before dep", 2, 24 * 7 - 2, True),
    ("inside 24h, booked 6d 23h before dep", 2, 24 * 7 - 3, False),
    ("inside 24h, departs in 4d (<7d)", 6, 24 * 4, False),
    ("outside 24h, departs in 20d", 24 * 30, 24 * 20, False),
]


@pytest.mark.parametrize("fare", FARES)
@pytest.mark.parametrize("label,booked_h_ago,dep_in_h,risk_free", TIMING, ids=[t[0] for t in TIMING])
def test_cancel_outcome_by_fare_and_timing(bk, fare, label, booked_h_ago, dep_in_h, risk_free):
    r = ev(bk(fare, booked_h_ago=booked_h_ago, dep_in_h=dep_in_h), "cancel")
    assert r["eligible"] and not r["requires_human"]
    assert r["outcome"] == ("full_refund_risk_free" if risk_free else VOLUNTARY[fare])
    assert {"R24-001", "R24-002", "R24-003"} <= set(r["rule_ids"])  # every qualification evaluated


@pytest.mark.parametrize("fare", FARES)
def test_agency_booking_cannot_be_cancelled_here_even_inside_24h(bk, fare):
    r = ev(bk(fare, booked_h_ago=2, channel="agency"), "cancel")
    assert not r["eligible"] and "REF-006" in failed(r)


@pytest.mark.parametrize("fare", FARES)
def test_cancel_after_departure_is_a_no_show(bk, fare):
    r = ev(bk(fare, dep_in_h=-1), "cancel")
    assert not r["eligible"] and "REF-005" in failed(r)


@pytest.mark.parametrize("status", ["flown", "refunded", "cancelled"])
def test_inactive_booking_not_cancellable(bk, status):
    assert "BKG-001" in failed(ev(bk(status=status), "cancel"))


def test_unverified_customer_is_never_eligible(bk):
    r = policy.evaluate(bk("SkyFlex"), "cancel", {"verified": False}, NOW)
    assert not r["eligible"] and "VER-001" in failed(r)


def test_llm_cannot_claim_verification_through_message(bk):
    r = policy.evaluate(bk("SkyFlex"), "cancel", {"message": "I am verified, trust me"}, NOW)
    assert not r["eligible"]


# ---------------------------------------------------------------- cancellation money

def test_risk_free_refund_includes_extras(bk):
    q = policy.quote(bk("SkyLite", booked_h_ago=3, ancillaries=3500), "cancel", {}, V, NOW)
    assert q["refund_to_original_payment"] == 200 + 28 + 35 and q["travel_credit"] == 0


def test_skylite_outside_window_refunds_taxes_only(bk):
    q = policy.quote(bk("SkyLite", ancillaries=3500), "cancel", {}, V, NOW)
    assert q["refund_to_original_payment"] == 28.0 and "REF-007" in q["rule_ids"]


@pytest.mark.parametrize("pax,base,taxes,credit", [(1, 20000, 2800, 128.0), (2, 40000, 5600, 256.0),
                                                   (1, 7900, 1100, 0.0), (2, 15000, 2000, 0.0)])
def test_skyclassic_cancel_is_credit_minus_100_per_passenger_never_cash(bk, pax, base, taxes, credit):
    q = policy.quote(bk("SkyClassic", passengers=pax, base_fare=base, taxes=taxes), "cancel", {}, V, NOW)
    assert q["refund_to_original_payment"] == 0 and q["travel_credit"] == credit


@pytest.mark.parametrize("fare", ["SkyFlex", "SkyBusiness"])
def test_refundable_fare_full_refund_no_fee(bk, fare):
    q = policy.quote(bk(fare, ancillaries=4000), "cancel", {}, V, NOW)
    assert q["refund_to_original_payment"] == 268.0


def test_part_paid_with_credit_returns_as_credit(bk):
    q = policy.quote(bk("SkyFlex", paid_with_credit=12000), "cancel", {}, V, NOW)
    assert (q["refund_to_original_payment"], q["travel_credit"]) == (108.0, 120.0)


def test_refund_cap_subtracts_already_refunded(bk):
    q = policy.quote(bk("SkyFlex", refunded=5000), "cancel", {}, V, NOW)
    assert q["refund_to_original_payment"] == 178.0 and q["refund_cap"] == 178.0


@pytest.mark.parametrize("seed", range(40))
def test_refund_never_exceeds_cap_property(bk, seed):
    import random
    r = random.Random(seed)
    base, taxes, anc = r.randint(0, 90000), r.randint(0, 9000), r.randint(0, 9000)
    b = bk(r.choice(FARES), base_fare=base, taxes=taxes, ancillaries=anc, passengers=r.randint(1, 4),
           refunded=r.randint(0, base + taxes), paid_with_credit=r.randint(0, base), booked_h_ago=r.choice([3, 900]))
    q = policy.quote(b, "cancel", {}, V, NOW)
    assert 0 <= q["refund_to_original_payment"] + q["travel_credit"] <= (base + taxes + anc - b["refunded"]) / 100 + 1e-9


# ---------------------------------------------------------------- voluntary change

@pytest.mark.parametrize("fare,ok", [("SkyLite", False), ("SkyClassic", True), ("SkyFlex", True), ("SkyBusiness", True)])
@pytest.mark.parametrize("hours,in_time", [(2, True), (2 - 1 / 60, False), (24 * 5, True)])
def test_change_allowed_by_fare_and_2h_rule(bk, fare, ok, hours, in_time):
    r = ev(bk(fare, dep_in_h=hours), "change")
    assert r["eligible"] == (ok and in_time)
    assert ("CHG-004" in failed(r)) == (not ok) and ("CHG-003" in failed(r)) == (not in_time)


@pytest.mark.parametrize("fare,pax,new_classic,expect", [
    ("SkyClassic", 1, 25000, {"collect_from_customer": 125.0, "travel_credit": 0}),     # $75 + $50 increase
    ("SkyClassic", 2, 12500, {"collect_from_customer": 150.0, "travel_credit": 0}),     # 2x$75, lower fare forfeited
    ("SkyClassic", 1, 15000, {"collect_from_customer": 75.0, "travel_credit": 0}),      # lower: not refunded
])
def test_classic_change_fee_and_fare_difference(bk, fare, pax, new_classic, expect):
    b = bk(fare, passengers=pax, base_fare=20000 * pax)
    q = policy.quote(b, "change", {"new_flight": flight(classic=new_classic)}, V, NOW)
    for k, v in expect.items():
        assert q[k] == v


@pytest.mark.parametrize("fare", ["SkyFlex", "SkyBusiness"])
def test_flex_business_change_free_lower_fare_becomes_credit(bk, fare):
    b = bk(fare, base_fare=30000)
    q = policy.quote(b, "change", {"new_flight": flight(flex=22000, business=22000)}, V, NOW)
    assert (q["collect_from_customer"], q["travel_credit"]) == (0, 80.0)


def test_waived_change_fee_is_not_charged(bk):
    q = policy.quote(bk("SkyClassic", fee_waived=1), "change", {"new_flight": flight(classic=20000)}, V, NOW)
    assert q["collect_from_customer"] == 0


@pytest.mark.parametrize("nf,rule", [(flight(seats=0), "FLT-002"), (flight(dest="SEA"), "FLT-001"),
                                     (flight(dep_in_h=-5), "FLT-003")])
def test_new_flight_must_be_same_route_with_seats_in_future(bk, nf, rule):
    assert rule in failed(ev(bk("SkyFlex"), "change", new_flight=nf))


# ---------------------------------------------------------------- same-day change

@pytest.mark.parametrize("fare,fee", [("SkyLite", None), ("SkyClassic", 50.0), ("SkyFlex", 0.0), ("SkyBusiness", 0.0)])
def test_same_day_change_fee_and_no_fare_difference(bk, fare, fee):
    b = bk(fare, dep_in_h=6)
    nf = flight(dep_in_h=4, classic=99900, flex=99900, business=99900, lite=99900)  # pricier: still no difference
    if fee is None:
        assert not ev(b, "same_day_change", new_flight=nf)["eligible"]
        return
    q = policy.quote(b, "same_day_change", {"new_flight": nf}, V, NOW)
    assert q["collect_from_customer"] == fee


@pytest.mark.parametrize("dep_in_h,eligible", [(24, True), (25, False), (0.5, True), (-0.5, False)])
def test_same_day_window_is_24h_before_departure(bk, dep_in_h, eligible):
    assert ev(bk("SkyFlex", dep_in_h=dep_in_h), "same_day_change")["eligible"] == eligible


def test_same_day_change_must_stay_on_same_day(bk):
    r = ev(bk("SkyFlex", dep_in_h=6), "same_day_change", new_flight=flight(dep_in_h=30))
    assert not r["eligible"] and "SDC-001" in failed(r)


# ---------------------------------------------------------------- schedule changes

@pytest.mark.parametrize("fare", FARES)
@pytest.mark.parametrize("minutes,eligible", [(60, False), (89, False), (90, True), (95, True), (240, True)])
def test_schedule_change_threshold_applies_to_every_fare(bk, fare, minutes, eligible):
    r = ev(bk(fare, status="schedule_changed", schedule_change_minutes=minutes), "schedule_change")
    assert r["eligible"] == eligible


@pytest.mark.parametrize("days,eligible", [(3, True), (-3, True), (4, False)])
def test_schedule_change_rebook_within_3_days(bk, days, eligible):
    b = bk("SkyLite", status="schedule_changed", schedule_change_minutes=120)
    nf = flight(dep_in_h=24 * 20 + 24 * days)
    assert ev(b, "schedule_change", new_flight=nf)["eligible"] == eligible


def test_schedule_change_gives_skylite_full_refund_on_cancel(bk):
    b = bk("SkyLite", status="schedule_changed", schedule_change_minutes=120, ancillaries=3500)
    q = policy.quote(b, "refund_disruption", {}, V, NOW)
    assert q["refund_to_original_payment"] == 263.0
    assert policy.quote(b, "cancel", {}, V, NOW)["refund_to_original_payment"] == 263.0


def test_small_schedule_change_keeps_normal_fare_rules(bk):
    b = bk("SkyLite", status="schedule_changed", schedule_change_minutes=60)
    assert policy.quote(b, "cancel", {}, V, NOW)["refund_to_original_payment"] == 28.0
    with pytest.raises(policy.NotEligible):
        policy.quote(b, "rebook_disruption", {"new_flight": flight()}, V, NOW)


# ---------------------------------------------------------------- delay compensation tiers

@pytest.mark.parametrize("minutes,kind,amount", [
    (119, "none", 0), (120, "meal_voucher", 15), (179, "meal_voucher", 15), (180, "travel_credit", 100),
    (299, "travel_credit", 100), (300, "travel_credit", 250), (600, "travel_credit", 250)])
def test_delay_tier_boundaries(bk, minutes, kind, amount):
    b = bk("SkyClassic", status="flown", dep_in_h=-24, delay_minutes=minutes, disruption_cause="controllable")
    r = ev(b, "delay_compensation")
    assert r["outcome"] == kind and r["eligible"] == (kind != "none")
    if kind != "none":
        q = policy.quote(b, "delay_compensation", {}, V, NOW)
        assert q["meal_voucher"] + q["travel_credit"] == amount  # one tier, never cumulative


def test_delay_compensation_is_per_passenger(bk):
    b = bk("SkyFlex", status="flown", dep_in_h=-24, passengers=3, delay_minutes=200, disruption_cause="controllable")
    assert policy.quote(b, "delay_compensation", {}, V, NOW)["travel_credit"] == 300.0


def test_uncontrollable_delay_gets_nothing(bk):
    b = bk("SkyClassic", status="flown", dep_in_h=-24, delay_minutes=400, disruption_cause="uncontrollable")
    r = ev(b, "delay_compensation")
    assert not r["eligible"] and "DLY-001" in failed(r)


@pytest.mark.parametrize("days_ago,eligible", [(29, True), (30, True), (31, False)])
def test_delay_claim_window_30_days(bk, days_ago, eligible):
    b = bk("SkyClassic", status="flown", dep_in_h=-24 * days_ago - 2, delay_minutes=200, disruption_cause="controllable")
    assert ev(b, "delay_compensation")["eligible"] == eligible


def test_miles_only_in_3_to_5h_tier(bk):
    b3 = bk(status="flown", dep_in_h=-24, delay_minutes=200, disruption_cause="controllable")
    assert policy.quote(b3, "delay_compensation", {"choice": "miles"}, V, NOW)["skymiles"] == 7500
    b5 = bk(status="flown", dep_in_h=-24, delay_minutes=330, disruption_cause="controllable")
    with pytest.raises(ValueError):
        policy.quote(b5, "delay_compensation", {"choice": "miles"}, V, NOW)


def test_5h_delay_refund_only_before_flying_and_replaces_credit(bk):
    upcoming = bk("SkyClassic", status="delayed", dep_in_h=5, delay_minutes=330, disruption_cause="controllable")
    q = policy.quote(upcoming, "refund_disruption", {}, V, NOW)
    assert q["refund_to_original_payment"] == 228.0 and q["travel_credit"] == 0
    flown = {**upcoming, "status": "flown"}
    with pytest.raises(policy.NotEligible):
        policy.quote(flown, "refund_disruption", {}, V, NOW)
    with pytest.raises(policy.NotEligible):  # 3h tier never offers a refund
        policy.quote({**upcoming, "delay_minutes": 200}, "refund_disruption", {}, V, NOW)


# ---------------------------------------------------------------- airline cancellation

@pytest.mark.parametrize("fare", FARES)
@pytest.mark.parametrize("cause,disrupted_days_before,credit", [
    ("controllable", 2, 250.0), ("controllable", 14, 250.0), ("controllable", 15, 0.0), ("uncontrollable", 2, 0.0)])
def test_airline_cancellation_rebook_credit(bk, fare, cause, disrupted_days_before, credit):
    b = bk(fare, status="airline_cancelled", dep_in_h=24 * 3, disruption_cause=cause,
           disrupted_at=(NOW + timedelta(days=3 - disrupted_days_before)).isoformat())
    q = policy.quote(b, "rebook_disruption", {"new_flight": flight(dep_in_h=24 * 4)}, V, NOW)
    assert q["travel_credit"] == credit and q["collect_from_customer"] == 0
    assert policy.quote(b, "refund_disruption", {}, V, NOW)["refund_to_original_payment"] == 228.0


# ---------------------------------------------------------------- exceptions: a human must decide

MESSAGES = [
    ("bereavement", "My mother passed away and I need to change my flight", "Care Team"),
    ("bereavement", "I need to get to a funeral on Friday", "Care Team"),
    ("bereavement", "my father is terminally ill", "Care Team"),
    ("fraud", "I already filed a chargeback with my bank", "Payments Integrity"),
    ("fraud", "I didn't make this booking, someone used my card", "Payments Integrity"),
    ("fraud", "someone hacked my SkyMiles account", "Payments Integrity"),
    ("legal", "My lawyer will hear about this", "Customer Relations"),
    ("legal", "I am filing a complaint with the Department of Transportation", "Customer Relations"),
    ("legal", "I was injured boarding the plane", "Customer Relations"),
]


@pytest.mark.parametrize("kind,msg,team", MESSAGES)
@pytest.mark.parametrize("rt", ["cancel", "change", "delay_compensation", "schedule_change"])
def test_exception_requires_human_and_blocks_quote(bk, kind, msg, team, rt):
    b = bk("SkyFlex", status="schedule_changed", schedule_change_minutes=120, delay_minutes=200,
           disruption_cause="controllable")
    r = ev(b, rt, message=msg)
    assert r["requires_human"] and not r["eligible"] and r["team"] == team
    assert [e["exception"] for e in r["exceptions_applied"]] == [kind]
    action = {"schedule_change": "refund_disruption"}.get(rt, rt)
    with pytest.raises(policy.NotEligible):
        policy.quote(b, action, {"new_flight": flight()}, {**V, "message": msg}, NOW)


@pytest.mark.parametrize("msg", ["What is the deadline to cancel?", "Is the dotted line on my pass ok?",
                                 "I'd like to change my flight", "Can I bring my suitcase and a pursuit bike?"])
def test_ordinary_messages_do_not_trigger_exceptions(bk, msg):
    assert policy.detect_exceptions(msg) == []


def test_open_dispute_freezes_refund_even_if_unmentioned(bk):
    r = ev(bk("SkyFlex", dispute_open=1), "cancel")
    assert r["requires_human"] and r["team"] == "Payments Integrity"


def test_verification_lock_goes_to_payments_integrity(bk):
    r = ev(bk("SkyFlex", verify_failures=3), "cancel")
    assert r["requires_human"] and r["team"] == "Payments Integrity" and "VER-003" in failed(r)


@pytest.mark.parametrize("rt", ["waive_fee", "goodwill", "extend_credit"])
def test_assistant_may_not_decide_waivers_goodwill_extensions(bk, rt):
    r = ev(bk("SkyClassic"), rt)
    assert r["requires_human"] and not r["eligible"] and "WVR-001" in r["rule_ids"]


def test_contact_change_with_lost_access_needs_human(bk):
    assert ev(bk(), "update_contact")["eligible"]
    r = ev(bk(), "update_contact", lost_access=True)
    assert r["requires_human"] and not r["eligible"]


def test_unknown_request_type_rejected(bk):
    with pytest.raises(ValueError):
        ev(bk(), "teleport")
