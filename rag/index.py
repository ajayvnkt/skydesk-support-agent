"""Hybrid retrieval: dense (numpy cosine over cached text-embedding-3-small vectors) + BM25,
fused with Reciprocal Rank Fusion. BM25 catches exact codes (SD-ERR-112, fare class names);
dense catches paraphrases ("my bag never showed up" -> delayed baggage)."""
from __future__ import annotations

import re
from typing import Any, Callable

import numpy as np
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from rank_bm25 import BM25Okapi

from .ingest import embed_text, from_document, to_document

RRF_K = 60
K_DENSE = 20
K_BM25 = 20
TOP_K = 12
SUPERSEDED_WEIGHT = 0.5  # fused score multiplier for superseded docs (kept, but ranked lower)

# Codes like SD-ERR-112, PNR-AB12CD, 23kg, 7-day stay one token; plain words split as usual.
TOKEN = re.compile(r"[a-z0-9]+(?:[-_/.][a-z0-9]+)*")
STOP = frozenset("""a an the and or of to in on for is are was were be been it its this that
with as at by from my me i you your we our can do does did how what when where which who why
will would should could have has had not no if so than then there their them they""".split())


def tokenize(text: str) -> list[str]:
    toks = []
    for t in TOKEN.findall(text.lower()):
        if t in STOP:
            continue
        toks.append(t)
        if re.search(r"[-_/.]", t):  # also index the parts so "err 112" still hits SD-ERR-112
            toks += [p for p in re.split(r"[-_/.]", t) if p and p not in STOP]
    return toks


def rrf(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """score(d) = sum over rankings of 1 / (k + rank), rank starting at 1."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


class HybridIndex:
    def __init__(self, chunks: list[Document | dict], embed: Callable[[list[str]], list[list[float]]]):
        """chunks: LangChain Documents (or the equivalent flat dicts); kept as flat rows inside
        for the numpy / BM25 lookups. `embed` maps texts -> vectors (LLM.embed in prod, a fake in tests)."""
        if not chunks:
            raise ValueError("no chunks to index")
        chunks = [from_document(c) if isinstance(c, Document) else c for c in chunks]
        self.chunks = chunks
        self.by_id = {c["chunk_id"]: c for c in chunks}
        self.embed = embed
        texts = [embed_text(c) for c in chunks]
        m = np.asarray(embed(texts), dtype=np.float32)
        self.matrix = m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)
        self.bm25 = BM25Okapi([tokenize(t) for t in texts])

    def dense(self, query: str, k: int = K_DENSE) -> list[str]:
        q = np.asarray(self.embed([query])[0], dtype=np.float32)
        sims = self.matrix @ (q / max(np.linalg.norm(q), 1e-12))
        return [self.chunks[i]["chunk_id"] for i in np.argsort(-sims)[:k]]

    def keyword(self, query: str, k: int = K_BM25) -> list[str]:
        toks = tokenize(query)
        if not toks:
            return []
        scores = self.bm25.get_scores(toks)
        return [self.chunks[i]["chunk_id"] for i in np.argsort(-scores)[:k] if scores[i] > 0]

    def search(self, query: str, k_dense: int = K_DENSE, k_bm25: int = K_BM25,
               top_k: int = TOP_K, superseded_weight: float = SUPERSEDED_WEIGHT,
               mode: str = "hybrid") -> list[dict]:
        """mode: hybrid (RRF order) | dense | bm25 (that retriever's order). Both ranks are
        always annotated so the UI and the agreement signal can show them."""
        d, b = self.dense(query, k_dense), self.keyword(query, k_bm25)
        fused = rrf([d, b])
        for cid in fused:
            if self.by_id[cid]["superseded"]:
                fused[cid] *= superseded_weight
        dr = {c: i + 1 for i, c in enumerate(d)}
        br = {c: i + 1 for i, c in enumerate(b)}
        if mode not in ("hybrid", "dense", "bm25"):
            raise ValueError(f"unknown retrieval mode {mode!r}")
        ranked = {"dense": d, "bm25": b}.get(mode, None)
        ranked = (sorted(fused, key=lambda c: -fused[c]) if ranked is None else ranked)[:top_k]
        return [{**self.by_id[c], "fused": round(fused[c], 6), "dense_rank": dr.get(c),
                 "bm25_rank": br.get(c)} for c in ranked]


class HybridRetriever(BaseRetriever):
    """LangChain retriever over a HybridIndex. mode: dense | bm25 | hybrid (RRF). Returns
    Documents in the index's order, with fused / dense_rank / bm25_rank in metadata."""
    index: Any
    mode: str = "hybrid"
    k: int = TOP_K

    def _get_relevant_documents(self, query: str, *,
                                run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        return [to_document(r) for r in self.index.search(query, top_k=self.k, mode=self.mode)]
