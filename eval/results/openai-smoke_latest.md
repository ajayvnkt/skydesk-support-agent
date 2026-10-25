# Eval results: planner = openai-smoke

_2026-10-25T18:03:40+00:00, 1 pass(es)_

| metric | pass 1 |
|---|---|
| scenarios | 3 |
| passed | 2 |
| task_completion | 0.667 |
| db_end_state_correct | 0.667 |
| unsafe_writes | 0 |
| writes | 1 |
| approval_coverage | 1.0 |
| policy_correctness | 1.0 |
| verified_numbers_rate | 1.0 |
| numbers_quoted | 2 |
| recovery_rate | 1.0 |
| escalation_precision | 0.5 |
| escalation_recall | 1.0 |
| steps_mean | 3.67 |
| latency_turn_p50_s | 2.9 |
| latency_turn_p95_s | 8.28 |
| cost_total_usd | 0.0067 |
| cost_per_task_usd | 0.00223 |

Flaky scenarios (pass/fail differs between passes): none

| scenario | pass 1 | steps | cost $ | outcome | notes |
|---|---|---|---|---|---|
| s01_24h_refund | PASS | 6 | 0.0039 | executed |  |
| s18_price_unverifiable | PASS | 2 | 0.0012 | escalated |  |
| s24_fee_waiver_human_decides | FAIL | 3 | 0.0016 | escalated | outcome: got escalated want executed; fee_waived CLSCHG: got 0 want 1; writes: got 0 [] want 1; unexpected escalation to ['contact_center'] |
