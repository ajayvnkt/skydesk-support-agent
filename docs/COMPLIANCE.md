# Compliance check: Week 3 handout ("Build Your AI Agent")

Each requirement in the handout is checked against this repository below, with evidence. Status as of
2026-10-25.

**Summary:**

- **PASS: 26**, plus both instructor-feedback items
- **PARTIAL: 3**
  - LangChain agent/tool objects
  - LangSmith
  - Google Doc not yet uploaded
- **PENDING (student action): 2** (video, GitHub push)
- **FAIL: 0**
- **N/A: 1** (Nebius, optional)

## What the agent must be

| # | Requirement (handout) | Status | Evidence |
|---|---|---|---|
| 1 | "Not a one-shot LLM call. Not a RAG lookup." | PASS | LangGraph loop of planner, tools, propose, approval, execute, then back to planner. RAG is only one of 13 tools (`search_help_center`). `skydesk/agent/graph.py` |
| 2 | "decides what to do next" | PASS | gpt-4.1-mini tool calling picks the next tool or the final answer each step (`planner_node`). Conditional edges route on the result (`after_planner`, `after_tools`, `after_propose`, `after_approval`). |
| 3 | "calls tools" | PASS | 6 READ, 6 WRITE and 1 hand-off tool, each with a JSON schema. `skydesk/tools.py`, `skydesk/agent/prompts.py::TOOL_SCHEMAS` |
| 4 | "holds state across steps" | PASS | Typed `AgentState` (customer, booking, pending action, approvals, retries, events) is checkpointed per thread in SQLite. `tests/test_agent_graph.py::test_resume_after_restart_from_sqlite_checkpoint`, `tests/test_adversarial.py::test_restart_mid_approval_resumes_once_via_the_server` |
| 5 | "recovers from errors" | PASS | Timeout plus retry with backoff and jitter, result validation, the same idempotency key on write retries, and an honest hand-off on permanent failure. 8 retry/failure tests in `tests/test_agent_graph.py`; eval s16, s17, s18 (recovery 1.00). |
| 6 | "hands off to a human when it should" | PASS | Guard (must-escalate topics) and policy `requires_human` both lead to `escalate_to_human` with a team and ticket. Eval s09, s10, s11 (escalation precision and recall 1.00). |
| 7 | "running end-to-end on a real task" | PASS | It runs end to end with the **real gpt-4.1-mini planner** in the web app, the CLI and the eval, against the real backend tools: 24 scenarios × 2 passes, 23/24 completed in each pass, 0 unsafe writes (`eval/results/openai-final2_20261025-131630.json`, `docs/EVAL_REPORT.md`). |

## Part 1 and 2: use case, one-liner, framework

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 8 | Pick a use case, or bring your own and formalise it with the framework | PASS | Bring-your-own: an airline support agent, built on Week 2. README "Agent framework", `docs/PROJECT_DOC.md` §3 |
| 9 | One-liner in the template (user, multi-step task, surface, manual workflow replaced, N tools, hand-off condition, success measure with time and rate) | PASS | README "One-liner", with measured gpt-4.1-mini numbers: task completion 23/24 (96%) per pass, median agent time 3.5–3.7 s per task (p95 4.7–4.9 s per turn). The 2-minute end-to-end time with real users is stated as a target, not as measured. |
| 10 | Rule: task completion, not single-shot accuracy; measure end to end | PASS | The eval grades the **airline database end state** after multi-turn conversations with approvals (`eval/run_eval.py`, `eval/scenarios.yaml`). |
| 11 | Rule: decide what it remembers, for how long, where | PASS | Thread state lives in SQLite per `thread_id` (`store/checkpoints.db`). Cross-session customer memory (`store/memory.db`) is loaded only after verification and scrubbed of payment data (`tests/test_agent_graph.py::test_memory_scrubs_payment_data`). |
| 12 | Rule: "Write actions deserve a human… Reads can be autonomous" | PASS | All 6 writes pause at `interrupt()`, and READs run autonomously. The backend also refuses writes without an approved, single-use approval id. `test_every_write_pauses_for_approval_and_reject_executes_nothing` (5 writes), `tests/test_tools.py` guards; eval: 0 unsafe writes, approval coverage 1.00. |
| 13 | Framework: Agent goal | PASS | README table |
| 14 | Framework: Where people use it | PASS | Website chat widget on port 9009, `/supervisor`, CLI |
| 15 | Framework: Steps in order | PASS | README table and the architecture diagram |
| 16 | Framework: What it can do, read vs write marked | PASS | README table (READ, WRITE and HAND-OFF listed separately) |
| 17 | Framework: What it remembers | PASS | README table |
| 18 | Framework: What it should never do | PASS | README table. Enforced in code: `execute` asserts an approval, `propose` fills amounts from the quote, the ownership check, the guard and `verify.py`. |
| 19 | Framework: Human-in-the-loop (where, how) | PASS | Before every write: approve, reject or edit (an edit is re-quoted and re-approved). Approval card and `/supervisor`. `test_edit_path_requotes_and_asks_again`, `test_edit_can_only_lower_the_quoted_amount` |
| 20 | Framework: What happens when something breaks | PASS | README table. FaultInjector (`skydesk/faults.py`) covers timeout, 503, empty and malformed data, by probability or on the Nth call. `tests/test_adversarial.py::test_everything_down_is_honest_and_writes_nothing` |
| 21 | Framework: How you know it worked (one clear measure) | PASS | ≥ 20/24 scenarios with 0 unsafe writes. Measured with gpt-4.1-mini: 23/24 in each of 2 passes, 0 unsafe. |

## Part 3: Track 2 building blocks

| # | Building block | Status | Evidence |
|---|---|---|---|
| 22 | LangGraph state machines | PASS | `StateGraph` with 9 nodes and conditional edges (`graph.py::build_graph`) |
| 23 | Checkpointers for persistence | PASS | `SqliteSaver` (`graph.py::sqlite_checkpointer`). A pending approval survives a server restart (two tests). |
| 24 | Interrupts for human-in-the-loop | PASS | `interrupt()` in `approval`, `Command(resume=...)` in `Agent.resume` and `POST /api/approve` |
| 25 | LangChain agents and tools | **PARTIAL** | LangChain is used for the Week 2 `BaseRetriever` behind `search_help_center`. The agent loop is a custom LangGraph graph, and the tools are plain functions with OpenAI JSON schemas rather than LangChain `Tool` / `AgentExecutor`. This is deliberate: the approval gate has to sit in the graph, between the model's proposal and the write. |
| 26 | LangSmith for tracing and evals | **PARTIAL** | Not used. Tracing uses the ARL JSONL log graded by the Agent Report Card (`eval/agent_report_card_rules.md`), and evals are code (`eval/run_eval.py`). LangGraph runs can be traced in LangSmith with no code change by setting `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY`. |
| 27 | Nebius Token Factory for at least one model call ("may") | N/A | Optional in the handout. Not used. All model calls go through `rag/llm.py` (OpenAI, $1.00 cap). |

## "One last thing": failure handling

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 28 | "If your agent… falls over on the first tool failure, you have not finished" | PASS | Each READ is retried up to 3 times when the error is retryable. A failed critical read leads to an honest hand-off. A failed help center leads to a fallback. A failed write is reported as not done. If even the hand-off ticket fails, the customer is told to contact support. Demo presets `quote_flaky`, `lookup_flaky`, `quote_down`, `search_timeout`; screenshots `06_retry_badge.png`, `07_price_unverified.png`. |
| 29 | Error handling and human-in-the-loop hardened | PASS | Adversarial QA (`tests/test_adversarial.py`, 13 cases): double-clicked approve (bug found and fixed), "I'm the supervisor" bypass, injected approval text, approver edit to another customer's booking, restart mid-approval, everything down, malformed booking refs. |

## Deliverables

| # | Deliverable | Status | Evidence / action |
|---|---|---|---|
| 30 | Project documentation (Google Doc): overview, datasets, vibe-coding prompts, iterations, learnings | **PARTIAL** | The content is complete in `docs/PROJECT_DOC.md` and `submission/SkyDesk_Support_Agent_Week3.docx` (with screenshots and the architecture diagram). **Action:** upload the .docx to Google Docs, share it, and put the link in the form. |
| 31 | Video demo ≤ 5 min (walkthrough, how AI coding tools were used, live demo) | PENDING | **Action:** record it. The demo runs on port 9009 with `SKYDESK_PLANNER=openai` (gpt-4.1-mini), and the selectors are listed in the UI hand-off notes. Screenshots in `docs/images/` were re-taken with the real planner; reset demo data before recording. |
| 32 | Code base on GitHub, link in the form | PENDING | **Action:** commit and push. Before committing, check `git status`, and that `.env`, `.venv/`, `store/`, `logs/ui/` and `submission/` are ignored. No API key is in any trackable file (checked with a key-pattern scan and an exact-key scan, names only, 0 matches). Total OpenAI spend from the ledger: $0.397 of the $1.00 cap. |

## Instructor feedback on Week 2

| Feedback | Status | Evidence |
|---|---|---|
| "Fix the price-check failure path… if a price can't be verified, withhold it or escalate" | PASS | Amounts come only from `quote_amount`. A failed or garbage quote is withheld and escalated. `verify.py` never keeps an unbacked figure. `PRICE_CHANGED` refuses the write if the price moved after approval. Tests and scenario s18 are listed in the README. |
| "Test fare conditions and policy exceptions directly" | PASS | `tests/test_policy.py` (223 boundary cases), `tests/test_policy_corpus.py`, the fare-condition and exception claim tests in `tests/test_agent_verify.py`, and eval s02, s03, s22 and s23, graded on the money actually moved. |
