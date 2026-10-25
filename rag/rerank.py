"""Listwise LLM rerank: one gpt-4.1-nano call scores all candidates 0-3 (~1.5k tokens in,
~60 out, about $0.0002). On any parse/API failure the fused RRF order is kept."""
from __future__ import annotations

import json
import re

from .prompts import RERANK_PROMPT, to_messages

TOP_N = 4
PREVIEW_CHARS = 450



def _preview(c: dict) -> str:
    text = re.sub(r"\s+", " ", c["text"])[:PREVIEW_CHARS]
    sec = f" > {c['section']}" if c.get("section") else ""
    return f"[{c['doc_id']}{sec}] {text}"


def rerank(query: str, candidates: list[dict], llm, top_n: int = TOP_N) -> tuple[list[dict], dict]:
    """-> (top_n chunks each with 'rerank' score 0-3 or None, info dict).
    info = {ok, ambiguous, meta (llm usage or None), error?}."""
    if not candidates:
        return [], {"ok": False, "ambiguous": False, "meta": None, "error": "no candidates"}
    listing = "\n\n".join(f"({i}) {_preview(c)}" for i, c in enumerate(candidates))
    msgs = to_messages(RERANK_PROMPT, question=query, listing=listing)
    meta = None
    try:
        text, meta = llm.chat(msgs, purpose="rerank", max_tokens=150, json_mode=True)
        data = json.loads(text)
        raw = data["scores"]
        scores = {int(k): max(0, min(3, int(v))) for k, v in raw.items()}
        if not scores or not all(0 <= k < len(candidates) for k in scores):
            raise ValueError("score indices out of range")
    except Exception as e:  # BudgetExceeded too: degrade to fused order, don't crash the turn
        fallback = [{**c, "rerank": None} for c in candidates[:top_n]]
        return fallback, {"ok": False, "ambiguous": False, "meta": meta,
                          "error": f"{type(e).__name__}: {e}"}
    # Stable sort: ties keep fused order. Unscored candidates count as 0.
    order = sorted(range(len(candidates)), key=lambda i: -scores.get(i, 0))
    top = [{**candidates[i], "rerank": scores.get(i, 0)} for i in order[:top_n]]
    return top, {"ok": True, "ambiguous": bool(data.get("ambiguous", False)), "meta": meta}
