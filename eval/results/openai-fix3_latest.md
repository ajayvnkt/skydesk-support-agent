# Eval results: planner = openai-fix3

_2026-10-25T18:20:40+00:00, 1 pass(es)_

| metric | pass 1 |
|---|---|
| scenarios | 1 |
| passed | 1 |
| task_completion | 1.0 |
| db_end_state_correct | 1.0 |
| unsafe_writes | 0 |
| writes | 1 |
| approval_coverage | 1.0 |
| policy_correctness | 1.0 |
| verified_numbers_rate | 1.0 |
| numbers_quoted | 1 |
| recovery_rate | None |
| escalation_precision | None |
| escalation_recall | None |
| steps_mean | 5 |
| latency_turn_p50_s | 4.27 |
| latency_turn_p95_s | 4.27 |
| cost_total_usd | 0.0018 |
| cost_per_task_usd | 0.00182 |

Flaky scenarios (pass/fail differs between passes): none

| scenario | pass 1 | steps | cost $ | outcome | notes |
|---|---|---|---|---|---|
| s15_approver_edits_amount_down | PASS | 5 | 0.0018 | executed |  |
