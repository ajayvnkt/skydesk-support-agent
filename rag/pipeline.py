"""Public entry points: build_index(corpus_dir) and ask(query)."""
from __future__ import annotations

import time
from pathlib import Path

from . import arl
from .graph import CONFIDENCE_THRESHOLD, build_graph
from .index import HybridIndex
from .ingest import chunk_documents, load_corpus
from .llm import LLM, PROJECT

CORPUS_DIR = PROJECT / "corpus"
_default: dict = {}


def build_index(corpus_dir: Path | str = CORPUS_DIR, llm: LLM | None = None,
                embed=None) -> HybridIndex:
    """Load + chunk + embed (cached on disk, so a rebuild of an unchanged corpus is free)."""
    llm = llm or LLM()
    docs = chunk_documents(load_corpus(corpus_dir))
    return HybridIndex(docs, embed or (lambda texts: llm.embed(texts, purpose="embed_index")))


def _defaults():
    if not _default:
        llm = LLM()
        _default["llm"] = llm
        _default["index"] = build_index(llm=llm)
    return _default["index"], _default["llm"]


def clip(query: str, limit: int = 2000) -> str:
    """Keep the head and the tail: in long messages the actual question is usually at the end."""
    h = limit // 2
    return query if len(query) <= limit else f"{query[:h]} ... {query[-h:]}"


def ask(query: str, index: HybridIndex | None = None, llm=None, *,
        threshold: float = CONFIDENCE_THRESHOLD, log_path: Path | str | None = arl.LOG_PATH,
        run_id: str | None = None, task_type: str = "support_qa",
        mode: str = "hybrid_rerank") -> dict:
    """-> {query, route, answer, citations, confidence, reason, chunks, scores, ticket,
    timings, cost_usd, run_id, mode, candidates, support}. confidence = estimated P(route is
    right) (rag.graph.route_confidence); support = retrieval evidence the threshold gates on. mode: see rag.graph.MODES. Writes the turn to the ARL log unless log_path is None."""
    query = (query or "").strip()
    if not query:
        raise ValueError("empty query")
    if index is None or llm is None:
        d_index, d_llm = _defaults()
        index, llm = index or d_index, llm or d_llm
    t0 = time.perf_counter()
    state = build_graph(index, llm, threshold=threshold, mode=mode).invoke(
        {"query": clip(query), "raw_query": query, "timings": {}, "cost": 0.0, "trace": []})
    state["timings"]["total"] = round((time.perf_counter() - t0) * 1000, 1)
    events = arl.run_events(query, state, run_id=run_id, task_type=task_type)
    if log_path:
        arl.append(events, log_path)
    return {
        "query": query, "route": state.get("route"), "answer": state.get("answer", ""),
        "citations": state.get("citations", []), "confidence": state.get("confidence"),
        "support": state.get("support"),
        "reason": state.get("reason"), "ticket": state.get("ticket"),
        "chunks": [{k: c.get(k) for k in ("chunk_id", "doc_id", "title", "section", "doc_type",
                                          "updated", "superseded", "text", "fused", "rerank",
                                          "dense_rank", "bm25_rank")}
                   for c in state.get("chunks", [])],
        "scores": state.get("scores", []), "timings": state["timings"],
        "cost_usd": round(state.get("cost", 0.0), 8), "run_id": events[0]["run_id"],
        "mode": mode,
        "candidates": [{k: c.get(k) for k in ("chunk_id", "doc_id", "title", "fused", "dense_rank",
                                              "bm25_rank")} for c in state.get("candidates", [])],
    }
