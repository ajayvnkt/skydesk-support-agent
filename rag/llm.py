"""The ONLY module that talks to OpenAI.

Every call goes through `LLM.chat` / `LLM.embed`, which:
  * refuse the call (raise BudgetExceeded) if ledger spend + estimated cost > budget ($1.00),
  * append the real usage to store/cost_ledger.json (ts, purpose, model, tokens, usd, latency),
  * cache embeddings on disk (store/embeddings.json) keyed by sha256(model + text),
    so re-indexing an unchanged corpus costs $0.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
STORE = PROJECT / "store"

CHAT_MODEL = "gpt-4.1-nano"
CHECK_MODEL = "gpt-4.1-mini"  # only for the price self-check: nano misreads "1st extra" fee rows
PLANNER_MODEL = CHECK_MODEL  # Week 3 agent planner (tool calling)
EMBED_MODEL = "text-embedding-3-small"
# USD per 1M tokens: (input, output)
PRICES = {CHAT_MODEL: (0.10, 0.40), CHECK_MODEL: (0.40, 1.60), EMBED_MODEL: (0.02, 0.0)}
CACHED_INPUT = {CHAT_MODEL: 0.025, CHECK_MODEL: 0.10}  # automatic prompt caching (prefix >= 1024 tokens)
BUDGET_USD = 1.00


class BudgetExceeded(RuntimeError):
    pass


def cost_usd(model: str, tokens_in: int, tokens_out: int = 0, cached_in: int = 0) -> float:
    pin, pout = PRICES[model]
    cached_in = min(cached_in, tokens_in)
    return ((tokens_in - cached_in) * pin + cached_in * CACHED_INPUT.get(model, pin) + tokens_out * pout) / 1_000_000


def _est_tokens(text: str) -> int:
    return len(text) // 4 + 1


class LLM:
    def __init__(self, store_dir: Path | str = STORE, budget: float = BUDGET_USD, client=None):
        self.store = Path(store_dir)
        self.store.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.store / "cost_ledger.json"
        self.cache_path = self.store / "embeddings.json"
        self.budget = budget
        self._client = client
        self._cache: dict | None = None

    # ---- client (lazy: tests never construct a real one) ----
    @property
    def client(self):
        if self._client is None:
            from dotenv import load_dotenv
            from openai import OpenAI
            load_dotenv(PROJECT / ".env")
            self._client = OpenAI(timeout=60, max_retries=2)
        return self._client

    # ---- ledger ----
    def ledger(self) -> list[dict]:
        try:
            return json.loads(self.ledger_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def spent(self) -> float:
        return sum(r.get("usd", 0.0) for r in self.ledger())

    def _guard(self, est_usd: float, purpose: str):
        spent = self.spent()
        if spent + est_usd > self.budget:
            raise BudgetExceeded(
                f"{purpose}: spent ${spent:.4f} + est ${est_usd:.4f} > budget ${self.budget:.2f}")

    def _record(self, purpose, model, tin, tout, latency_ms, cached_in: int = 0) -> dict:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "purpose": purpose,
               "model": model, "tokens_in": tin, "tokens_out": tout, "cached_in": cached_in,
               "usd": round(cost_usd(model, tin, tout, cached_in), 8), "latency_ms": round(latency_ms, 1)}
        rows = self.ledger()
        rows.append(row)
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1))
        os.replace(tmp, self.ledger_path)  # atomic: a crash never truncates the ledger
        return row

    # ---- chat ----
    def chat(self, messages: list[dict], purpose: str, max_tokens: int = 400,
             temperature: float = 0.0, json_mode: bool = False,
             model: str = CHAT_MODEL) -> tuple[str, dict]:
        """Returns (text, meta) where meta = {model, tokens_in, tokens_out, cost_usd, latency_ms}."""
        est_in = sum(_est_tokens(m["content"]) for m in messages) + 10
        self._guard(cost_usd(model, est_in, max_tokens), purpose)
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens, **kw)
        ms = (time.perf_counter() - t0) * 1000
        u = resp.usage
        row = self._record(purpose, model, u.prompt_tokens, u.completion_tokens, ms)
        return resp.choices[0].message.content or "", {
            "model": model, "tokens_in": u.prompt_tokens, "tokens_out": u.completion_tokens,
            "cost_usd": row["usd"], "latency_ms": row["latency_ms"]}

    # ---- tool calling (Week 3 agent planner) ----
    def chat_tools(self, messages: list[dict], tools: list[dict], purpose: str,
                   max_tokens: int = 500, model: str = PLANNER_MODEL) -> tuple[dict, dict]:
        """One planner step. Returns (msg, meta); msg = {content, tool_calls: [{id, name, arguments}]}
        where arguments is the raw JSON string from the model (callers parse + validate it)."""
        est_in = sum(_est_tokens(json.dumps(m, default=str)) for m in messages) \
            + _est_tokens(json.dumps(tools)) + 10
        self._guard(cost_usd(model, est_in, max_tokens), purpose)
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=model, messages=messages, tools=tools, temperature=0.0, max_tokens=max_tokens)
        ms = (time.perf_counter() - t0) * 1000
        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        row = self._record(purpose, model, u.prompt_tokens, u.completion_tokens, ms, cached)
        m = resp.choices[0].message
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                 for c in (m.tool_calls or [])]
        return {"content": m.content, "tool_calls": calls}, {
            "model": model, "tokens_in": u.prompt_tokens, "tokens_out": u.completion_tokens,
            "cost_usd": row["usd"], "latency_ms": row["latency_ms"]}

    # ---- embeddings (cached) ----
    def _load_cache(self) -> dict:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                self._cache = {}
        return self._cache

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(f"{EMBED_MODEL}\n{text}".encode()).hexdigest()

    def embed(self, texts: list[str], purpose: str = "embed") -> list[list[float]]:
        cache = self._load_cache()
        missing = list(dict.fromkeys(t for t in texts if self._key(t) not in cache))
        for i in range(0, len(missing), 256):  # batch to stay well under request limits
            batch = missing[i:i + 256]
            self._guard(cost_usd(EMBED_MODEL, sum(_est_tokens(t) for t in batch)), purpose)
            t0 = time.perf_counter()
            resp = self.client.embeddings.create(model=EMBED_MODEL, input=batch)
            self._record(purpose, EMBED_MODEL, resp.usage.prompt_tokens, 0,
                         (time.perf_counter() - t0) * 1000)
            for t, d in zip(batch, resp.data):
                cache[self._key(t)] = d.embedding
            self.cache_path.write_text(json.dumps(cache))
        return [cache[self._key(t)] for t in texts]


def summary(store_dir: Path | str = STORE) -> dict:
    rows = LLM(store_dir).ledger()
    by: dict[str, float] = {}
    for r in rows:
        by[r["purpose"]] = by.get(r["purpose"], 0.0) + r["usd"]
    total = sum(by.values())
    return {"calls": len(rows), "total_usd": round(total, 6), "budget_usd": BUDGET_USD,
            "remaining_usd": round(BUDGET_USD - total, 6),
            "by_purpose": {k: round(v, 6) for k, v in sorted(by.items())}}
