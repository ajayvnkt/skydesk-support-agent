# Agent Report Card

**File format:** ARL JSONL (canonical) (confidence 0.98) -- 64 runs, 1234 events.
**Policy:** default (generic) (built-in generic default).  **Success score:** trained model.

## Headline

- Runs: **64**; agent said "done" in **70%**.
- Mean predicted success: **53%** (no ground-truth outcomes in this file).
- Runs with a critical finding: **2**; said done although something failed / never happened: **0**.
- Cost: **$0.1570** total, $0.0025 per run, $0.0046 per expected success. (estimated)

## Fix these first

### 1. [P0] issue_voucher runs without verify check first

- **Why:** 2 of 64 runs (3%) called issue_voucher before verify check had succeeded in the same run.
- **Fix:** Add a guard in the tool layer: issue_voucher requires verify check=ok earlier in the same run; otherwise refuse with a clear message. Do not rely on the prompt to enforce the order.
- **Impact:** Affected runs average 0.0% predicted success vs 55% for the rest. They cost $0.0023 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-c8d22d1afd92, sda-f488c5313bb9

### 2. [P3] Keep testing prompt-injection cases (agent resisted)

- **Why:** 4 of 64 runs (6%) contained instruction-like text; the agent did not act on it.
- **Fix:** Good behaviour -- keep these runs as regression tests in your eval set so a prompt or model change cannot silently break it.
- **Impact:** Affected runs average 59% predicted success vs 53% for the rest. They cost $0.0149 in total. Fixing this addresses up to 4 runs.
- **Evidence runs:** sda-9423d2c33d62, sda-e199ca7a568a, sda-4947e0da5284, sda-de647cbaca50

### 3. [P3] check_eligibility keeps failing with the same error

- **Why:** 2 check_eligibility calls in 2 of 64 runs (3%) failed with: "BAD_ARGS: request_type must be one of ('cancel', 'change', 'same_day_change', 'schedule_change', 'delay_compensation', '".
- **Fix:** Fix the tool-argument schema / prompt for check_eligibility: the model keeps sending inputs the tool rejects ("BAD_ARGS: request_type must be one of ('cancel', 'change', 'same_day_change', 'schedule_change', 'delay_compensation', '"). Add the allowed values to the tool description and validate before calling.
- **Impact:** Affected runs average 28% predicted success vs 54% for the rest. They cost $0.0109 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-7c78c518d2bb, sda-01c3df06106e

### 4. [P3] lookup_booking keeps failing with the same error

- **Why:** 2 lookup_booking calls in 2 of 64 runs (3%) failed with: "TIMEOUT: lookup_booking did not respond in time".
- **Fix:** Fix the tool-argument schema / prompt for lookup_booking: the model keeps sending inputs the tool rejects ("TIMEOUT: lookup_booking did not respond in time"). Add the allowed values to the tool description and validate before calling.
- **Impact:** Affected runs average 92% predicted success vs 52% for the rest. They cost $0.0068 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-da9ed37c06d3, sda-b8c755a9482c

### 5. [P3] lookup_booking keeps failing with the same error

- **Why:** 6 lookup_booking calls in 2 of 64 runs (3%) failed with: "HTTP 503 Service Unavailable from lookup_booking backend".
- **Fix:** Fix the tool-argument schema / prompt for lookup_booking: the model keeps sending inputs the tool rejects ("HTTP 503 Service Unavailable from lookup_booking backend"). Add the allowed values to the tool description and validate before calling.
- **Impact:** Affected runs average 0.0% predicted success vs 55% for the rest. They cost $0.0021 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-d58b541aa974, sda-77ef708a239c

### 6. [P3] quote_amount keeps failing with the same error

- **Why:** 2 quote_amount calls in 2 of 64 runs (3%) failed with: "HTTP 503 Service Unavailable from quote_amount backend".
- **Fix:** Fix the tool-argument schema / prompt for quote_amount: the model keeps sending inputs the tool rejects ("HTTP 503 Service Unavailable from quote_amount backend"). Add the allowed values to the tool description and validate before calling.
- **Impact:** Affected runs average 92% predicted success vs 52% for the rest. They cost $0.0068 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-da9ed37c06d3, sda-b8c755a9482c

### 7. [P3] quote_amount keeps failing with the same error

- **Why:** 6 quote_amount calls in 2 of 64 runs (3%) failed with: "malformed data from quote_amount".
- **Fix:** Fix the tool-argument schema / prompt for quote_amount: the model keeps sending inputs the tool rejects ("malformed data from quote_amount"). Add the allowed values to the tool description and validate before calling.
- **Impact:** Affected runs average 0.0% predicted success vs 55% for the rest. They cost $0.0047 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-5cb9df0a446c, sda-7a8f608812b4

### 8. [P3] verify_claims keeps failing with the same error

- **Why:** 2 verify_claims calls in 2 of 64 runs (3%) failed with: "{'action': 'withheld', 'removed': [{'sentence': 'The details you provided do not match our records.', 'claims': ['condit".
- **Fix:** Fix the tool-argument schema / prompt for verify_claims: the model keeps sending inputs the tool rejects ("{'action': 'withheld', 'removed': [{'sentence': 'The details you provided do not match our records.', 'claims': ['condit"). Add the allowed values to the tool description and validate before calling.
- **Impact:** Affected runs average 2% predicted success vs 55% for the rest. They cost $0.0020 in total. Fixing this addresses up to 2 runs.
- **Evidence runs:** sda-43909a64fde8, sda-7a32cfb6fa68

## Autonomy Ladder

| Task | Runs | Success | Rung | Why |
|---|---|---|---|---|
| support_agent | 64 | 53% (predicted) | Suggest only | success 53% (predicted) is below 60% |

## Runs most likely to have failed

| Run | Task | P(success) | Outcome | Findings |
|---|---|---|---|---|
| sda-c8d22d1afd92 | support_agent | 0% | - | PREREQ_MISSING |
| sda-f488c5313bb9 | support_agent | 0% | - | PREREQ_MISSING |
| sda-8231e07d0d52 | support_agent | 0% | - | - |
| sda-5cb9df0a446c | support_agent | 0% | - | - |
| sda-7a8f608812b4 | support_agent | 0% | - | - |
| sda-d58b541aa974 | support_agent | 0% | - | - |
| sda-77ef708a239c | support_agent | 0% | - | - |
| sda-68bd606698e3 | support_agent | 1% | - | - |
| sda-4182a90cf700 | support_agent | 1% | - | - |
| sda-4902951847d3 | support_agent | 1% | - | - |
| sda-53908c7ded36 | support_agent | 1% | - | - |
| sda-4b4b3cda97c0 | support_agent | 1% | - | - |
| sda-a3637ee74bc4 | support_agent | 1% | - | - |
| sda-541cf86f0298 | support_agent | 1% | - | - |
| sda-7bcb3c77df71 | support_agent | 1% | - | - |
