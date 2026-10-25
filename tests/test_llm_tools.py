"""rag/llm.py chat_tools: tool calls parsed, cost recorded in the ledger, hard cap enforced (no API)."""
from types import SimpleNamespace as NS

import pytest

from rag.llm import LLM, PLANNER_MODEL, BudgetExceeded


class FakeClient:
    def __init__(self):
        self.kw = None
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.kw = kw
        call = NS(id="call_1", function=NS(name="lookup_booking", arguments='{"booking_ref": "SKY123"}'))
        return NS(usage=NS(prompt_tokens=1000, completion_tokens=50),
                  choices=[NS(message=NS(content=None, tool_calls=[call]))])


def test_chat_tools_parses_calls_and_records_cost(tmp_path):
    c = FakeClient()
    llm = LLM(tmp_path, client=c)
    msg, meta = llm.chat_tools([{"role": "user", "content": "hi"}], [{"type": "function"}], purpose="agent_planner")
    assert msg["tool_calls"] == [{"id": "call_1", "name": "lookup_booking", "arguments": '{"booking_ref": "SKY123"}'}]
    assert c.kw["model"] == PLANNER_MODEL == "gpt-4.1-mini" and c.kw["tools"]
    assert meta["cost_usd"] == pytest.approx((1000 * 0.40 + 50 * 1.60) / 1e6)
    assert llm.ledger()[-1]["purpose"] == "agent_planner"


def test_chat_tools_respects_budget_cap(tmp_path):
    llm = LLM(tmp_path, budget=0.0, client=FakeClient())
    with pytest.raises(BudgetExceeded):
        llm.chat_tools([{"role": "user", "content": "hi"}], [], purpose="agent_planner")


def test_cached_prompt_tokens_billed_at_cached_rate(tmp_path):
    c = FakeClient()
    orig = c.create
    c.chat.completions.create = lambda **kw: (lambda r: (setattr(r.usage, "prompt_tokens_details",
                                                                  NS(cached_tokens=800)), r)[1])(orig(**kw))
    llm = LLM(tmp_path, client=c)
    _, meta = llm.chat_tools([{"role": "user", "content": "hi"}], [], purpose="agent_planner")
    assert meta["cost_usd"] == pytest.approx((200 * 0.40 + 800 * 0.10 + 50 * 1.60) / 1e6)
    assert llm.ledger()[-1]["cached_in"] == 800
