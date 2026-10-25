"""LangGraph flow for one support turn.

classify --(guard hit)--> escalate --> END
         --(too vague)--> clarify  --> END
         --> retrieve --> rerank --> grade --(low support)-----------------> escalate
                                          --(answer differs per fare/tier)---> clarify
                                          --> generate --> verify --(ok)--> END
                                                 ^                --(no valid citation, once)--> generate (repair)
                                                 +----------------                 --(fail)--> escalate
"""
from __future__ import annotations

import json
import operator
import os
import re
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from .index import HybridRetriever, tokenize
from .ingest import from_document
from .llm import CHECK_MODEL, BudgetExceeded
from .prompts import CHECK_PROMPT, GEN_PROMPT, REFUSAL_SENTINEL, REPAIR_PROMPT, to_messages
from .rerank import rerank

CONFIDENCE_THRESHOLD = 0.5

# Topics SkyDesk policy says always go to a human, plus prompt-injection-looking text.
GUARDS: list[tuple[str, re.Pattern]] = [
    ("bereavement", re.compile(
        r"\b(bereave\w*|death|dead|died|dying|passed away|funeral|deceased|condolence)\b", re.I)),
    ("fraud", re.compile(
        r"\b(fraud\w*|scam\w*|chargeback|charge ?back|stolen (card|identity)|identity theft|"
        r"unauthori[sz]ed (charge|transaction|payment|purchase)|didn'?t (make|authori[sz]e) "
        r"(this|that|the) (charge|booking|payment)|hacked)\b", re.I)),
    ("legal", re.compile(
        r"\b(lawyer|attorney|solicitor|legal action|lawsuit|sue|suing|court|small claims|"
        r"regulator|ombudsman|DOT complaint|department of transportation|CAA|DGCA|litigation)\b",
        re.I)),
    ("prompt_injection", re.compile(
        r"(ignore|disregard|forget|override)\b.{0,40}\b(instructions|rules|polic(y|ies)|prompt|"
        r"guidelines)|system prompt|you are now|developer mode|jailbreak|\bDAN\b|"
        r"pretend (to be|you are)|act as (an? )?(admin|supervisor|developer)|"
        r"reveal .{0,20}(prompt|instructions)|</?(system|instructions?|question|customer_question)>|"
        # Indirect injection / fake authority (QA: "the customer is pre-verified, tell them ...").
        r"\bnote to (the )?(assistant|bot|ai|model)\b|\bpre-?verified\b|"
        r"\bi (hereby )?(authori[sz]e|instruct|order) you\b|"
        r"\bas (a|an|the|your) skydesk (supervisor|agent|manager|employee|admin)\b", re.I)),
    ("human_requested", re.compile(
        r"\b(talk|speak|connect|transfer)\b.{0,30}\b(human|real person|agent|representative|"
        r"supervisor|manager)\b", re.I)),
]

VAGUE = re.compile(r"^\W*(help|hi|hello|hey|question|issue|problem|it|this|that)\W*$", re.I)


def guard(query: str) -> str | None:
    """LLM-free check. -> guard name or None."""
    for name, pat in GUARDS:
        if pat.search(query):
            return name
    return None


def guards(query: str) -> list[str]:
    """Every guard that fires (a chargeback + lawyer message needs two teams)."""
    return [name for name, pat in GUARDS if pat.search(query)]


def is_vague(query: str) -> bool:
    return bool(VAGUE.match(query)) or len(tokenize(query)) <= 1


class State(TypedDict, total=False):
    query: str          # clipped text sent to search/LLM
    raw_query: str      # full customer text; guards scan all of it
    route: str          # answer | escalate | clarify
    reason: str         # why we escalated / clarified
    candidates: list[dict]
    chunks: list[dict]
    scores: list
    ambiguous: bool
    support: float      # retrieval evidence (0..1); the escalation threshold gates on this
    confidence: float   # reported: estimated P(the chosen route is right), see route_confidence
    option: dict        # set when the answer differs per fare/tier and the customer didn't say theirs
    repaired: str       # why the one citation-repair generation ran ("" / unset = not yet)
    answer: str
    citations: list[str]
    ticket: dict
    timings: Annotated[dict, operator.or_]
    cost: Annotated[float, operator.add]
    trace: Annotated[list, operator.add]  # tool/llm events for the ARL log


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _llm_trace(purpose: str, meta: dict | None, content: str = "") -> list[dict]:
    return [{"kind": "llm", "purpose": purpose, "content": content, **meta}] if meta else []


def support(chunks: list[dict]) -> float:
    """Retrieval evidence that the KB answers the question, 0..1. Rerank evidence dominates
    (best and second-best scores); retriever agreement (chunk found by BOTH dense and BM25) adds
    a little. Without rerank scores only agreement counts, capped so it alone can just pass the
    default threshold."""
    if not chunks:
        return 0.0
    agree = sum(1 for c in chunks if c.get("dense_rank") and c.get("bm25_rank")) / len(chunks)
    s = [c["rerank"] for c in chunks if c.get("rerank") is not None]
    if not s:
        return round(0.55 * agree, 3)
    s = sorted(s, reverse=True) + [0]
    return round(0.6 * s[0] / 3 + 0.25 * s[1] / 3 + 0.15 * agree, 3)


def route_confidence(route: str, reason: str | None, sup: float, *, repaired: bool = False,
                     flagged_vague: bool = False) -> float:
    """Reported confidence = estimated P(the route taken is the right one), built from the same
    signals as the decision. `support` alone answers "does the KB answer this?", which points the
    wrong way for escalations (a correct low-evidence escalation scored ~0, hence AUROC 0.125).
      answer:   support, x0.85 if citations needed a repair (the draft was shaky), x0.9 if the
                reranker called the question vague (weak signal, so a mild discount).
      escalate, low support: 1 - support. The less evidence, the surer "not in the KB" is.
      escalate after generation failed (refusal, bad/no citation, claimed action): retrieval said
                answerable but generation disagreed; 1 - support/2 sits between the two
                (~0.5 when support was strong, higher when support was marginal).
      clarify on a per-fare/tier table: support. The table is deterministic evidence that the
                answer differs per option, so it is as sure as the evidence is relevant.
    The factors are fixed priors from what each signal means, not fitted to the 20 eval queries."""
    if route == "answer":
        return round(sup * (0.85 if repaired else 1.0) * (0.9 if flagged_vague else 1.0), 3)
    if route == "escalate":
        return round(1 - sup if reason == "low_confidence" else 1 - sup / 2, 3)
    return round(sup, 3)


# ---- fare / tier dependent answers ----
# A table whose first column is a customer attribute: "Fare class", "Tier", "Cabin", "Status".
OPTION_FAMILY = re.compile(r"\b(fare|cabin|tier|status|membership|class)\b", re.I)
_SEPARATOR = re.compile(r"[\s|:\-]+")


def option_tables(text: str) -> list[dict]:
    """Markdown tables (and HTML tables flattened to '| a | b' rows) keyed by a customer attribute
    whose rows give at least two different answers. -> [{family, columns, rows: {option: cells}}]"""
    out, block = [], []
    for line in text.splitlines() + [""]:
        if line.strip().startswith("|"):
            block.append(line)
            continue
        rows = [[c.strip().replace("**", "") for c in ln.strip().strip("|").split("|")]
                for ln in block if not _SEPARATOR.fullmatch(ln)]
        block = []
        if len(rows) < 3 or not OPTION_FAMILY.search(rows[0][0]):
            continue
        opts = {r[0]: r[1:] for r in rows[1:] if r[0]}
        if len(opts) >= 2 and len({tuple(v) for v in opts.values()}) >= 2:
            out.append({"family": rows[0][0], "columns": rows[0][1:], "rows": opts})
    return out


def stated_options(query: str, options: list[str]) -> list[str]:
    """Options the customer named: "SkyClassic", "sky classic" or, when all options share a
    prefix like "Sky", just "classic"."""
    q, flat = query.lower(), re.sub(r"\s+", "", query.lower())
    pre = os.path.commonprefix([o.lower() for o in options])
    found = []
    for o in options:
        name = o.lower()
        short = name[len(pre):] if len(pre) >= 3 and len(name) - len(pre) >= 3 else None
        if re.sub(r"\s+", "", name) in flat or (short and re.search(rf"\b{re.escape(short)}\b", q)):
            found.append(o)
    return found


def option_ambiguity(query: str, chunks: list[dict]) -> dict | None:
    """The best-supported chunks (top rerank score, >= 2; or the first chunk without rerank)
    contain a per-fare/tier table and the customer named none of its options -> that table."""
    scored = [c for c in chunks if c.get("rerank") is not None]
    if scored:
        best = max(c["rerank"] for c in scored)
        top = [c for c in scored if c["rerank"] == best] if best >= 2 else []
    else:
        top = chunks[:1]
    found = None
    for c in top:
        for t in option_tables(c["text"]):
            if stated_options(query, list(t["rows"])):
                continue
            if found is None:
                found = {**t, "doc_ids": [c["doc_id"]]}
            elif t["family"].lower() == found["family"].lower() and list(t["rows"]) == list(found["rows"]) \
                    and t["columns"] != found["columns"]:  # e.g. change fee + same-day change tables
                found["columns"] = found["columns"] + t["columns"]
                found["rows"] = {o: found["rows"][o] + t["rows"][o] for o in found["rows"]}
                found["doc_ids"] = list(dict.fromkeys(found["doc_ids"] + [c["doc_id"]]))
    return found


def pinned_option_chunk(query: str, ranked: list[dict], chosen: list[dict]) -> dict | None:
    """When the customer names their fare/tier, the passage with that option's row (what the fare
    includes) must be in context, or fee arithmetic can't subtract included allowances. Returns the
    best-ranked relevant (rerank >= 1) candidate outside `chosen` holding such a row, if any."""
    def has_row(c):
        return any(stated_options(query, list(t["rows"])) for t in option_tables(c["text"]))
    if any(has_row(c) for c in chosen):
        return None
    have = {c["chunk_id"] for c in chosen}
    for c in ranked:
        if c["chunk_id"] in have or (c.get("rerank") or 0) < 1:
            continue
        if has_row(c):
            return {**c, "pinned": True}
    return None


def option_clarify_message(opt: dict) -> str:
    fam = opt["family"].lower()
    lines = [f"- {o}: " + "; ".join(f"{h}: {v}" if h else v for h, v in zip(opt["columns"], vals))
             for o, vals in opt["rows"].items()]
    refs = "".join(f"[{d}]" for d in opt["doc_ids"])
    return (f"That depends on your {fam}. Here is how it works for each {refs}:\n\n"
            + "\n".join(lines) + f"\n\nWhich {fam} are you on: {', '.join(opt['rows'])}?")


def _context(chunks: list[dict]) -> str:
    parts = []
    for c in chunks:
        sec = f" > {c['section']}" if c.get("section") else ""
        flag = (" SUPERSEDED" if c.get("superseded") else "") + \
            (" RESOLVED TICKET, another customer" if c.get("doc_type") == "ticket" else "")
        parts.append(f"[{c['doc_id']}] {c['title']}{sec} (updated {c.get('updated') or 'n/a'}{flag})\n"
                     f"{c['text']}")
    return "\n\n---\n\n".join(parts)


BRACKET = re.compile(r"\[([^\[\]]{1,200})\]")
DOC_ID = re.compile(r"[A-Za-z][A-Za-z0-9_\-./]*")


def citations(text: str) -> list[str]:
    """Doc ids in [..]; tolerates "[a, b]", "[policy-x, 2]" (bare numbers dropped) and a copied
    context header "[policy-x > Section (updated 2026-01-01)]" (the id before " >" / " (")."""
    ids = (re.split(r"\s+>|\s*\(", p, maxsplit=1)[0].strip()
           for g in BRACKET.findall(text) for p in re.split(r"[,;]", g))
    return list(dict.fromkeys(i for i in ids if DOC_ID.fullmatch(i)))


def resolve_citation(cite: str, ctx: set[str]) -> str:
    """Map a mangled id to the one context doc it uniquely matches: shortened ("change-fees")
    or with a chunk number / date glued on ("pets-policy-2", "pets-policy-2026-04-22")."""
    if cite in ctx:
        return cite
    # Only a chunk number or full date may be glued on: "policy-x-2024" is a different doc.
    m = [d for d in ctx if d.endswith("-" + cite) or (
        cite.startswith(d + "-") and re.fullmatch(r"\d{1,3}|\d{4}-\d{2}-\d{2}", cite[len(d) + 1:]))]
    return m[0] if len(m) == 1 else cite
# The bot cannot act, so an answer saying a refund/voucher/waiver was done is a hallucination
# or an injection that slipped past the guards ("Your $500 refund has been approved").
ACTION_CLAIM = re.compile(
    r"\b(?:has|have)\s+been\s+(?:approved|issued|processed|refunded|waived|granted|credited|"
    r"rebooked|cancell?ed)\b|\b(?:i|we)(?:'ve| have)\s+(?:approved|issued|processed|refunded|"
    r"waived|granted|credited|rebooked|cancell?ed)\b", re.I)
CONDITIONAL = re.compile(r"\b(once|after|when|until|if|before|unless)\b[^.!?]*$", re.I)


def claims_action(text: str) -> bool:
    """True if the answer asserts an action was done ("once it has been processed" is fine)."""
    return any(not CONDITIONAL.search(text[max(0, m.start() - 60):m.start()])
               for m in ACTION_CLAIM.finditer(text))


REFUSAL = re.compile(rf"{REFUSAL_SENTINEL}|\b(i (don'?t|do not) have (that|enough|this) "
                     r"information|not (covered|mentioned|found) in the (provided )?(context|passages)|"
                     r"i (can'?t|cannot|could not|couldn'?t) find)", re.I)

ESCALATION_MSG = {
    "bereavement": "I'm very sorry for your loss. Bereavement requests are handled personally by "
                   "the SkyDesk Care Team, so I'm connecting you with them; they can help with your "
                   "booking and any compassionate options.",
    "fraud": "Thanks for flagging this. Possible fraud or disputed charges must be handled by our "
             "Payments Integrity team, so I'm passing this to them. Please don't share card "
             "numbers or passwords in this chat.",
    "legal": "I understand. Legal and regulatory matters are handled by the SkyDesk Customer Relations "
             "team, so I'm handing your case to them and they will follow up.",
    "prompt_injection": "I can only help with SkyDesk travel questions under our support policy. "
                        "I've passed this conversation to a SkyDesk agent.",
    "human_requested": "Of course. I'm connecting you with a SkyDesk agent now.",
}
DEFAULT_ESCALATION = ("I want to be sure you get an accurate answer, and I couldn't confirm this "
                      "from the SkyDesk help center. I'm handing your question to a SkyDesk agent "
                      "who will get back to you.")


def _verify(ans: str, cites: list[str], ctx: set[str], reason: str | None) -> dict:
    if not ans:
        return {"route": "escalate", "reason": reason or "no_answer"}
    if REFUSAL.search(ans):
        return {"route": "escalate", "reason": "not_in_kb"}
    if claims_action(ans):
        return {"route": "escalate", "reason": "claimed_action"}
    if not cites:
        return {"route": "escalate", "reason": "no_citations"}
    if bad := [c for c in cites if c not in ctx]:
        return {"route": "escalate", "reason": f"citation_not_in_context:{','.join(bad)}"}
    return {"route": "answer"}


PRICE = re.compile(r"\$\s?\d")


def _price_check(llm, values: dict, text: str) -> tuple[str, list, float]:
    """One cheap JSON call that recomputes included / extra / fees and returns a corrected answer
    only if it disagrees. Any failure keeps the original answer (verify still runs on it)."""
    try:
        out, meta = llm.chat(to_messages(CHECK_PROMPT, context=values["context"],
                                         question=values["question"], answer=text),
                             purpose="price_check", max_tokens=450, json_mode=True,
                             model=CHECK_MODEL)
    except BudgetExceeded:
        return text, [], 0.0
    try:
        data = json.loads(out)
        fixed = (data.get("corrected_answer") or "").strip()
        new = fixed if data.get("answer_correct") is False and fixed else text
    except (ValueError, AttributeError):
        new = text
    return new, _llm_trace("price_check", meta, out), meta["cost_usd"]


# UI/eval retrieval modes -> (index.search mode, use LLM rerank)
MODES = {"hybrid_rerank": ("hybrid", True), "hybrid": ("hybrid", False),
         "dense": ("dense", False), "bm25": ("bm25", False)}


def build_graph(index, llm, threshold: float = CONFIDENCE_THRESHOLD, top_k: int = 12,
                top_n: int = 4, mode: str = "hybrid_rerank"):
    """index: HybridIndex; llm: object with .chat(messages, purpose, ...) -> (text, meta).
    Without rerank, the top_n retrieved chunks go straight to grade, where support then
    comes only from dense/BM25 agreement (so those modes escalate more)."""
    search_mode, use_rerank = MODES[mode]
    retriever = HybridRetriever(index=index, mode=search_mode, k=top_k)
    option_chunks = [c for c in getattr(index, "chunks", []) if option_tables(c["text"])]

    def classify(s: State) -> dict:
        t0 = time.perf_counter()
        hits = guards(s.get("raw_query") or s["query"])
        out: dict[str, Any] = {"timings": {"classify": _ms(t0)}}
        if hits:
            out.update(route="escalate", reason="+".join(hits))
        elif is_vague(s["query"]):
            out.update(route="clarify", reason="vague_query")
        return out

    def retrieve(s: State) -> dict:
        t0 = time.perf_counter()
        cands = [from_document(d) for d in retriever.invoke(s["query"])]
        # Superseded pages stay searchable but never reach the generator if a current page is there.
        cands = [c for c in cands if not c.get("superseded")] or cands
        ms = _ms(t0)
        return {"candidates": cands, "timings": {"retrieve": ms}, "trace": [{
            "kind": "tool", "tool": "search_kb",
            "args": {"query": s["query"], "top_k": top_k, "mode": search_mode},
            "result": [{"chunk_id": c["chunk_id"], "fused": c["fused"]} for c in cands],
            "status": "ok", "latency_ms": ms}]}

    def do_rerank(s: State) -> dict:
        if not use_rerank:
            top = [{**c, "rerank": None} for c in s.get("candidates", [])[:top_n]]
            return {"chunks": top, "scores": [None] * len(top)}
        t0 = time.perf_counter()
        cands = s.get("candidates", [])
        q = s.get("raw_query") or s["query"]
        # Per-fare/tier tables naming the customer's option join the rerank pool as pin-only
        # candidates: retrieval often misses "what my fare includes" for a fee question.
        have = {c["chunk_id"] for c in cands}
        extra = [{**c, "fused": None, "dense_rank": None, "bm25_rank": None} for c in option_chunks
                 if c["chunk_id"] not in have and not c.get("superseded")
                 and any(stated_options(q, list(t["rows"])) for t in option_tables(c["text"]))]
        ranked, info = rerank(s["query"], cands + extra, llm, top_n=len(cands) + len(extra))
        extra_ids = {c["chunk_id"] for c in extra}
        top = [c for c in ranked if c["chunk_id"] not in extra_ids][:top_n]
        if info["ok"] and (pin := pinned_option_chunk(q, ranked, top)):
            top = top + [pin]
        ms = _ms(t0)
        meta = info.get("meta") or {}
        return {"chunks": top, "scores": [c.get("rerank") for c in top],
                "ambiguous": info["ambiguous"], "timings": {"rerank": ms},
                "cost": meta.get("cost_usd", 0.0), "trace": _llm_trace("rerank", info.get("meta")) + [{
                    "kind": "tool", "tool": "rerank_kb",
                    "args": {"query": s["query"], "n_candidates": len(cands)},
                    "result": [{"doc_id": c["doc_id"], "chunk_id": c["chunk_id"],
                                "score": c.get("rerank"), **({"pinned": True} if c.get("pinned") else {})}
                               for c in top],
                    "status": "ok" if info["ok"] else "error", "error": info.get("error"),
                    "latency_ms": ms}]}

    def grade(s: State) -> dict:
        chunks = s.get("chunks", [])
        sup = support([c for c in chunks if not c.get("pinned")])
        # The reranker's `ambiguous` flag only discounts confidence: on the eval set it flagged
        # clear questions (and off-topic ones) as vague. Fare/tier ambiguity is detected from the
        # passages instead: the best chunks hold a per-option table and the customer named none.
        if sup < threshold:
            return {"support": sup, "route": "escalate", "reason": "low_confidence",
                    "confidence": route_confidence("escalate", "low_confidence", sup)}
        if opt := option_ambiguity(s.get("raw_query") or s["query"], chunks):
            return {"support": sup, "route": "clarify", "reason": "option_dependent", "option": opt,
                    "confidence": route_confidence("clarify", "option_dependent", sup)}
        return {"support": sup, "route": "answer"}

    def generate(s: State) -> dict:
        t0 = time.perf_counter()
        ids = list(dict.fromkeys(c["doc_id"] for c in s["chunks"]))
        values = {"context": _context(s["chunks"]), "question": s["query"]}
        repair = bool(s.get("repaired"))
        if repair:
            values.update(draft=s.get("answer", ""), doc_ids=", ".join(ids), example_id=ids[0])
        msgs = to_messages(REPAIR_PROMPT if repair else GEN_PROMPT, **values)
        purpose = "generate_repair" if repair else "generate"
        try:
            text, meta = llm.chat(msgs, purpose=purpose, max_tokens=350, temperature=0)
        except BudgetExceeded as e:
            return {"answer": "", "reason": "budget_exceeded", "timings": {purpose: _ms(t0)},
                    "trace": [{"kind": "error", "content": str(e)}]}
        text, trace, cost = text.strip(), _llm_trace(purpose, meta, text.strip()), meta["cost_usd"]
        if not repair and PRICE.search(text) and any(option_tables(c["text"]) for c in s["chunks"]):
            text, extra_trace, extra_cost = _price_check(llm, values, text)
            trace, cost = trace + extra_trace, cost + extra_cost
        ctx = set(ids)
        cites = list(dict.fromkeys(resolve_citation(c, ctx) for c in citations(text)))
        return {"answer": text, "citations": cites,
                "timings": {purpose: _ms(t0)}, "cost": cost,
                "trace": [{**t, "repair_reason": s["repaired"]} if repair else t for t in trace]}

    def verify(s: State) -> dict:
        ctx = {c["doc_id"] for c in s.get("chunks", [])}
        ans, cites = s.get("answer", ""), s.get("citations", [])
        out = _verify(ans, cites, ctx, s.get("reason"))
        # No valid citation although supporting chunks exist: one repair turn before escalating.
        why = (out.get("reason") or "").split(":")[0]
        if why in ("no_citations", "citation_not_in_context") and ctx \
                and not set(cites) & ctx and not s.get("repaired"):
            return {"route": "repair", "repaired": why}
        sup = s.get("support", 0.0)
        if out["route"] == "answer":
            out["reason"] = "citations_repaired" if s.get("repaired") else None
        out["confidence"] = route_confidence(out["route"], out.get("reason"), sup,
                                             repaired=bool(s.get("repaired")),
                                             flagged_vague=bool(s.get("ambiguous")))
        return out

    def escalate(s: State) -> dict:
        reason = s.get("reason", "unknown")
        topics = reason.split("+")
        ticket = {"reason": reason,
                  "priority": "high" if {"bereavement", "fraud", "legal"} & set(topics) else "normal",
                  "customer_message": s["query"][:500],
                  "suggested_docs": list(dict.fromkeys(c["doc_id"] for c in s.get("chunks", []))),
                  "confidence": s.get("confidence"),
                  "draft_answer": s.get("answer") or None}
        msg = " ".join(dict.fromkeys(ESCALATION_MSG[t] for t in topics if t in ESCALATION_MSG))
        return {"route": "escalate", "answer": msg or DEFAULT_ESCALATION,
                "citations": [], "ticket": ticket}

    def clarify(s: State) -> dict:
        if opt := s.get("option"):
            return {"route": "clarify", "answer": option_clarify_message(opt),
                    "citations": opt["doc_ids"]}
        titles = list(dict.fromkeys(c["title"] for c in (s.get("chunks") or s.get("candidates") or [])))[:3]
        msg = "Could you tell me a bit more so I can find the right answer? For example, your " \
              "trip type, what you're trying to do, or any error code you see."
        if titles:
            msg += " Are you asking about: " + "; ".join(titles) + "?"
        return {"route": "clarify", "answer": msg, "citations": []}

    g = StateGraph(State)
    for name, fn in [("classify", classify), ("retrieve", retrieve), ("rerank", do_rerank),
                     ("grade", grade), ("generate", generate), ("verify", verify),
                     ("escalate", escalate), ("clarify", clarify)]:
        g.add_node(name, fn)
    g.set_entry_point("classify")
    g.add_conditional_edges("classify", lambda s: s.get("route") or "retrieve",
                            {"escalate": "escalate", "clarify": "clarify", "retrieve": "retrieve"})
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "grade")
    g.add_conditional_edges("grade", lambda s: s["route"],
                            {"answer": "generate", "escalate": "escalate", "clarify": "clarify"})
    g.add_edge("generate", "verify")
    g.add_conditional_edges("verify", lambda s: s["route"],
                            {"answer": END, "escalate": "escalate", "repair": "generate"})
    g.add_edge("escalate", END)
    g.add_edge("clarify", END)
    return g.compile()
