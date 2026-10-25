"""Rebuild store/airline.db deterministically: `python -m skydesk.seed [db_path]`.

Simulated clock NOW = 2026-10-25T15:00:00Z (override with SKYDESK_NOW). Every scenario booking
is placed relative to it. SCENARIOS doubles as the demo cheat-sheet (ref -> situation).
"""
from __future__ import annotations

import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

NOW = datetime.fromisoformat(os.environ.get("SKYDESK_NOW", "2026-10-25T15:00:00+00:00"))
REF_RE = re.compile(r"^[A-HJ-NP-Z2-9]{6}$")  # booking refs never contain 0, 1, O or I
ROUTES = [  # flight number base, origin, dest, block minutes, SkyLite base price $
    (100, "AUS", "DEN", 150, 89), (200, "ORD", "SEA", 270, 129), (300, "JFK", "MIA", 190, 109),
    (400, "DFW", "LAX", 200, 119), (500, "BOS", "ATL", 160, 99)]
SLOTS = ["07:00", "12:00", "16:30", "21:00"]  # with NOW=15:00, today 16:30 is 1.5 h away, 21:00 is 6 h
CLASS_MULT = {"SkyLite": 1.0, "SkyClassic": 1.45, "SkyFlex": 2.2, "SkyBusiness": 3.6}
DAYS = range(-45, 46)

LAST = ["Garcia", "Okafor", "Nguyen", "Patel", "Kowalski", "Haddad", "Larsen", "Mendes", "Tanaka", "Brennan",
        "Ferreira", "Ivanova", "Mbeki", "Schmidt", "Rossi", "Delgado", "Novak", "Achebe", "Lindqvist", "Moreau",
        "Quinn", "Sato", "Varga", "Whitaker", "Yilmaz"]
FIRST = ["Maria", "Chidi", "Linh", "Anika", "Piotr", "Rana", "Erik", "Joana", "Kenji", "Siobhan",
         "Lucas", "Olga", "Thabo", "Greta", "Marco", "Elena", "Tomas", "Ngozi", "Astrid", "Claire",
         "Devon", "Yuki", "Bence", "Hannah", "Emre"]

# ref, customer#, route#, day offset, slot, fare, status, booked hours before NOW, extras, situation
SCENARIOS = [
    ("RSK7FT", 1, 0, 20, 1, "SkyLite", "confirmed", 5, {"ancillaries": 3500},
     "SkyLite booked 5 h ago, departs in 20 days: 24-hour risk-free cancellation, full refund incl. $35 cabin bag"),
    ("LTE4DY", 2, 0, 4, 0, "SkyLite", "confirmed", 6, {},
     "SkyLite booked 6 h ago but departs in 4 days: 24-h window does NOT apply (<7 days) -> taxes only"),
    ("LTEXPD", 3, 1, 10, 2, "SkyLite", "confirmed", 24 * 30, {},
     "SkyLite outside 24 h: non-refundable (taxes only), changes not permitted"),
    ("CLSCHG", 1, 2, 12, 1, "SkyClassic", "confirmed", 24 * 20, {},
     "SkyClassic voluntary change: $75 fee + fare difference (lower fare not refunded)"),
    ("CLSCXL", 4, 3, 15, 0, "SkyClassic", "confirmed", 24 * 40, {},
     "SkyClassic voluntary cancel: travel credit = fare - $100, never cash"),
    ("CLSL9W", 5, 4, 8, 3, "SkyClassic", "confirmed", 24 * 15, {"base_fare": 7900, "taxes": 1100},
     "SkyClassic fare $90 total: cancellation credit is $0 (must be told before confirming)"),
    ("FLXRFD", 6, 1, 9, 1, "SkyFlex", "confirmed", 24 * 25, {"paid_with_credit": 12000},
     "SkyFlex full refund, $120 was paid with a travel credit -> returned as credit, rest to card"),
    ("FLXCHG", 7, 0, 6, 2, "SkyFlex", "confirmed", 24 * 10, {},
     "SkyFlex free change; moving to a cheaper flight returns the difference as travel credit"),
    ("BUSFUL", 8, 3, 30, 2, "SkyBusiness", "confirmed", 24 * 12, {"passengers": 2, "ancillaries": 6000},
     "SkyBusiness, 2 passengers: full refund to card, no fee"),
    ("SCH95M", 9, 2, 5, 0, "SkyClassic", "schedule_changed", 24 * 30, {"schedule_change_minutes": 95},
     "Schedule moved 95 min: free move within 3 days on same route, or full refund (any fare)"),
    ("SCHEQ9", 10, 4, 7, 1, "SkyLite", "schedule_changed", 24 * 21, {"schedule_change_minutes": 90},
     "SkyLite schedule moved exactly 90 min: threshold met -> free rebook or full refund even on SkyLite"),
    ("SCHSML", 11, 1, 11, 3, "SkyLite", "schedule_changed", 24 * 21, {"schedule_change_minutes": 60},
     "Schedule moved 60 min: below 90-min threshold -> normal SkyLite rules (no change, taxes-only refund)"),
    ("DLY2H5", 12, 0, -2, 1, "SkyClassic", "flown", 24 * 30, {"delay_minutes": 150, "disruption_cause": "controllable"},
     "Flown, arrived 2 h 30 m late (crew, controllable): $15 meal voucher tier"),
    ("DLY3H2", 1, 3, -3, 2, "SkyFlex", "flown", 24 * 30, {"delay_minutes": 200, "disruption_cause": "controllable"},
     "Flown, arrived 3 h 20 m late (maintenance): $100 travel credit or 7,500 SkyMiles"),
    ("DLY5HR", 13, 2, 0, 3, "SkyClassic", "delayed", 24 * 14, {"delay_minutes": 330, "disruption_cause": "controllable"},
     "Tonight's flight delayed 5 h 30 m (controllable): $250 credit OR skip the trip for a full refund, not both"),
    ("DLYWX4", 14, 4, -1, 0, "SkyClassic", "flown", 24 * 20, {"delay_minutes": 240, "disruption_cause": "uncontrollable"},
     "Flown, 4 h weather delay: uncontrollable -> no compensation"),
    ("DLYXPD", 15, 1, -40, 1, "SkyFlex", "flown", 24 * 60, {"delay_minutes": 200, "disruption_cause": "controllable"},
     "Controllable 3 h 20 m delay 40 days ago: past the 30-day claim window"),
    ("CXLCTL", 16, 3, 2, 1, "SkyClassic", "airline_cancelled", 24 * 30,
     {"disruption_cause": "controllable", "disrupted_hours_ago": 1},
     "SkyDesk cancelled (crew shortage) 2 days out: free rebook + $250 credit, or full refund"),
    ("CXLWXR", 17, 0, 1, 0, "SkyLite", "airline_cancelled", 24 * 9,
     {"disruption_cause": "uncontrollable", "disrupted_hours_ago": 2},
     "SkyDesk cancelled for weather: free rebook or full refund, no $250 credit (even SkyLite gets a refund)"),
    ("DSPT9K", 18, 2, 14, 2, "SkyFlex", "confirmed", 24 * 20, {"dispute_open": 1},
     "Open card chargeback: refunds/changes frozen -> Payments Integrity"),
    ("AGNCY7", 19, 4, 18, 1, "SkyClassic", "confirmed", 24 * 30, {"channel": "agency"},
     "Booked via a travel agency: SkyDesk cannot refund or change; the agency must"),
    ("SAMEDY", 20, 1, 0, 3, "SkyClassic", "confirmed", 24 * 30, {},
     "SkyClassic departing in 6 h: same-day change to the 16:30 flight for $50, no fare difference"),
    ("LATE2H", 21, 3, 0, 2, "SkyFlex", "confirmed", 24 * 30, {},
     "Departs in 1.5 h: voluntary change blocked (<2 h rule); free same-day change to 21:00 still allowed"),
    ("FLWN3X", 22, 0, -5, 2, "SkyClassic", "flown", 24 * 40, {},
     "Already flown, on time: nothing to cancel or compensate"),
    ("RFNDXD", 23, 2, 16, 1, "SkyFlex", "refunded", 24 * 20, {"refunded_all": True},
     "Already refunded: no second refund"),
    ("LCKD3X", 24, 4, 13, 0, "SkyClassic", "confirmed", 24 * 20, {"verify_failures": 3},
     "3 failed verification attempts: locked 24 h -> Payments Integrity, no override"),
    ("BRVMNT", 25, 1, 3, 1, "SkyClassic", "confirmed", 24 * 25, {},
     "Demo for bereavement: customer says their mother passed away -> Care Team, no quote"),
    ("LGLCMP", 3, 3, -6, 1, "SkyBusiness", "flown", 24 * 40, {"delay_minutes": 45, "disruption_cause": "controllable"},
     "Demo for legal: customer threatens a DOT complaint / lawyer -> Customer Relations"),
    ("CNTCT5", 6, 0, 25, 3, "SkyFlex", "confirmed", 24 * 5, {},
     "Demo for update_contact: new email after verification (code goes to the existing contact)"),
    ("PRTRFD", 2, 2, 21, 0, "SkyFlex", "confirmed", 24 * 30, {"refunded": 5000},
     "SkyFlex with $50 already refunded: refund cap = fare paid - $50"),
]


def ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


def flight_id(route: int, day: int, slot: int) -> str:
    base = ROUTES[route][0]
    return f"SD{base + slot}-{(NOW + timedelta(days=day)).strftime('%Y%m%d')}"


SOLD_OUT = flight_id(0, 21, 1)  # AUS-DEN, day after RSK7FT's flight


def build(path=None) -> Path:
    path = Path(path or os.environ.get("SKYDESK_DB") or db.DB_PATH)
    if path.exists():
        path.unlink()
    rng = random.Random(20261025)
    conn = db.connect(path)
    db.set_now(conn, ts(NOW))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?)", [
        (f"C{i:03d}", FIRST[i - 1], LAST[i - 1], f"{FIRST[i - 1].lower()}.{LAST[i - 1].lower()}@example.com",
         f"+1-555-01{i:02d}-{rng.randint(1000, 9999)}", f"SM{rng.randint(10**7, 10**8 - 1)}", rng.randint(0, 60000))
        for i in range(1, 26)])

    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for r, (base, o, d, block, lite) in enumerate(ROUTES):
        for day in DAYS:
            for s, hhmm in enumerate(SLOTS):
                h, m = map(int, hhmm.split(":"))
                dep = day0 + timedelta(days=day, hours=h, minutes=m)
                swing = rng.uniform(0.8, 1.35)
                fares = [round(lite * swing * CLASS_MULT[fc]) * 100 for fc in CLASS_MULT]
                seats = 0 if rng.random() < 0.08 else rng.randint(1, 40)
                rows.append((flight_id(r, day, s), f"SD{base + s}", o, d, ts(dep), ts(dep + timedelta(minutes=block)),
                             seats, *fares))
    conn.executemany("INSERT INTO flights VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    bookings = list(SCENARIOS)
    while len(bookings) < 45:
        ref = "".join(rng.choice(alphabet) for _ in range(6))
        if ref in [b[0] for b in bookings]:
            continue
        fc = rng.choice(list(CLASS_MULT))
        bookings.append((ref, rng.randint(1, 25), rng.randrange(5), rng.randint(3, 40), rng.randrange(4), fc,
                         "confirmed", 24 * rng.randint(8, 60), {}, f"Ordinary {fc} booking"))

    for ref, cust, route, day, slot, fc, status, booked_h, x, note in bookings:
        assert REF_RE.match(ref), ref
        fid = flight_id(route, day, slot)
        conn.execute("UPDATE flights SET seats_left = MAX(seats_left, 3) WHERE flight_id=?", (fid,))
        f = db.flight(conn, fid)
        pax = x.get("passengers", 1)
        base = x.get("base_fare", f["fares"][fc] * pax)
        taxes = x.get("taxes", round(base * 0.14))
        refunded = base + taxes + x.get("ancillaries", 0) if x.get("refunded_all") else x.get("refunded", 0)
        disrupted = ts(NOW - timedelta(hours=x["disrupted_hours_ago"])) if "disrupted_hours_ago" in x else None
        conn.execute("""INSERT INTO bookings (ref, customer_id, flight_id, fare_class, status, channel, passengers,
            booked_at, base_fare, taxes, ancillaries, paid_with_credit, refunded, card_last4, dispute_open,
            delay_minutes, schedule_change_minutes, disruption_cause, disrupted_at, verify_failures, scenario)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (ref, f"C{cust:03d}", fid, fc, status, x.get("channel", "direct"), pax,
                      ts(NOW - timedelta(hours=booked_h)), base, taxes, x.get("ancillaries", 0),
                      x.get("paid_with_credit", 0), refunded, f"{rng.randint(0, 9999):04d}", x.get("dispute_open", 0),
                      x.get("delay_minutes", 0), x.get("schedule_change_minutes", 0), x.get("disruption_cause"),
                      disrupted, x.get("verify_failures", 0), note))
        paid = base + taxes + x.get("ancillaries", 0)
        conn.execute("INSERT INTO payments (ref, kind, amount, method, created_at, note) VALUES (?,?,?,?,?,?)",
                     (ref, "charge", paid, "card", ts(NOW - timedelta(hours=booked_h)), "ticket purchase"))
        if refunded:
            conn.execute("INSERT INTO payments (ref, kind, amount, method, created_at, note) VALUES (?,?,?,?,?,?)",
                         (ref, "refund", refunded, "card", ts(NOW - timedelta(days=2)), "earlier refund"))

    # rebooking alternatives for every scenario (same route, +-3 days) have seats, except one known sell-out
    for ref, cust, route, day, *_ in SCENARIOS:
        for dd in range(day - 3, day + 4):
            for s in range(len(SLOTS)):
                conn.execute("UPDATE flights SET seats_left = MAX(seats_left, 2) WHERE flight_id=?",
                             (flight_id(route, dd, s),))
    conn.execute("UPDATE flights SET seats_left = 0 WHERE flight_id=?", (SOLD_OUT,))

    conn.executemany("INSERT INTO vouchers VALUES (?,?,?,?,?,?,?,?,?)", [
        ("TC-7KQ2MX", "C001", None, "travel_credit", 8000, 0, ts(NOW - timedelta(days=200)),
         ts(NOW + timedelta(days=165)), "goodwill (earlier trip)"),
        ("TC-9PL4RW", "C004", None, "travel_credit", 15000, 0, ts(NOW - timedelta(days=385)),
         ts(NOW - timedelta(days=20)), "expired 20 days ago: extension needs a contact center agent"),
    ])
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    p = build(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"seeded {p} at simulated time {ts(NOW)}")
