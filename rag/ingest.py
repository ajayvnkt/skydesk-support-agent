"""Load the SkyDesk help center (Markdown + raw HTML) and cut it into retrievable chunks.

Chunking choices (for text-embedding-3-small):
  * Split on Markdown headings first, so a chunk never straddles two topics and the heading
    becomes `section` (shown in citations, prepended to the embedded text for context).
  * Then a recursive splitter (paragraph > line > sentence > word) caps chunks at ~350 tokens
    with ~50 overlap. text-embedding-3-small accepts 8191 tokens, but one vector per long
    passage blurs several facts into one point; ~350 tokens is roughly one policy clause or
    one manual procedure, which keeps similarity sharp, keeps 4 reranked chunks cheap to send
    to gpt-4.1-nano (~1.5k tokens of context), and 50 tokens overlap (~15%) stops a rule
    being cut from its condition at a boundary.
  * Chunking already uses LangChain's RecursiveCharacterTextSplitter (langchain_text_splitters)
    with a tiktoken length function, so there is nothing to swap in.
  * FAQ Q&A pairs (one per heading) and tickets stay whole even if longer: half a Q&A or a
    ticket without its resolution is worse than a slightly blurrier vector.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path

import yaml
from langchain_core.documents import Document

CHUNK_TOKENS = 350
OVERLAP_TOKENS = 50
WHOLE_DOC_TYPES = {"ticket"}  # never split
WHOLE_SECTION_TYPES = {"faq"}  # one Q&A per heading section, never split

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")  # the tokenizer text-embedding-3-small uses

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))
except Exception:  # tiktoken missing or its BPE file unavailable offline
    def count_tokens(text: str) -> int:
        return len(text) // 4


# ---------------- loading ----------------
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
BOILERPLATE = re.compile(
    r"^(skip to (main )?content|cookie|we use cookies|accept( all)?( cookies)?|sign in|log in|"
    r"menu|search|home|back to top|share( this)?|print|was this (article|page) helpful\??|"
    r"yes\s*no|©.*|copyright.*|all rights reserved.*|privacy( policy)?|terms( of use)?)\s*$", re.I)


def _norm_date(v) -> str:
    return str(v) if v is not None else ""


def _superseded(meta: dict) -> bool:
    return bool(meta.get("superseded") or meta.get("superseded_by")
                or str(meta.get("status", "")).lower() == "superseded")


def load_markdown(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    m = FRONT_MATTER.match(raw)
    meta = (yaml.safe_load(m.group(1)) or {}) if m else {}
    body = raw[m.end():] if m else raw
    return {
        "doc_id": str(meta.get("id") or path.stem), "title": str(meta.get("title") or path.stem),
        "doc_type": str(meta.get("doc_type") or "manual"), "updated": _norm_date(meta.get("updated")),
        "tags": list(meta.get("tags") or []), "superseded": _superseded(meta),
        "superseded_by": meta.get("superseded_by"), "source": str(path), "text": body.strip(),
    }


class _HTMLText(HTMLParser):
    """Visible text only; drops script/style/nav/header/footer/aside/noscript/form subtrees.
    Headings become Markdown headings so the same header-aware chunker applies."""
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "form", "svg"}
    BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "main", "ul", "ol", "table",
             "dd", "dt", "blockquote"}
    # Site chrome marked by id/class rather than a semantic tag.
    JUNK = re.compile(r"cookie|consent|breadcrumb|banner|site-?(nav|header|footer)", re.I)
    VOID = {"br", "img", "hr", "meta", "link", "input"}

    def __init__(self):
        super().__init__(convert_charrefs=True)  # decodes &amp; &nbsp; &#8217; ...
        self.out: list[str] = []
        self.skip = 0  # depth inside the skipped subtree rooted at _skip_root
        self._skip_root = ""
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") and a.get("content") is not None:
            self.meta[a["name"].lower().replace("-", "_")] = a["content"]  # doc-id == doc_id
        if tag in self.VOID:
            if tag == "br" and not self.skip:
                self.out.append("\n")
            return
        if self.skip and tag == self._skip_root:
            self.skip += 1
        elif self.skip:
            pass
        elif tag in self.SKIP or self.JUNK.search(f"{a.get('id') or ''} {a.get('class') or ''}"):
            self._skip_root, self.skip = tag, 1
        elif tag == "title":
            self._in_title = True
        elif not self.skip and re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif not self.skip and tag in self.BLOCK:
            self.out.append("\n")
        elif not self.skip and tag in ("td", "th"):
            self.out.append(" | ")  # keep table cells apart: "Pet in cabin | $95"

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self._skip_root:
                self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif not self.skip and (tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag)):
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def clean_html(raw: str) -> tuple[str, str, dict]:
    """-> (title, clean text, <meta name=..> dict)."""
    p = _HTMLText()
    p.feed(raw)
    p.close()
    lines = []
    for line in "".join(p.out).splitlines():
        line = re.sub(r"[ \t ]+", " ", html.unescape(line)).strip()
        if line and not BOILERPLATE.match(line):
            lines.append(line)
        elif not line and lines and lines[-1] != "":
            lines.append("")
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    # One blank line before headings so the Markdown splitter sees them.
    text = re.sub(r"\n(#{1,6} )", r"\n\n\1", text)
    return re.sub(r"\s+", " ", html.unescape(p.title)).strip(), text, p.meta


def load_html(path: Path) -> dict:
    title, text, meta = clean_html(path.read_text(encoding="utf-8", errors="replace"))
    # Site-name suffixes like "Baggage | SkyDesk Help" are boilerplate too.
    title = re.split(r"\s+[|\-–—]\s+", title)[0] if title else path.stem
    return {
        "doc_id": meta.get("doc_id") or meta.get("id") or path.stem, "title": title,
        "doc_type": meta.get("doc_type") or "manual", "updated": meta.get("updated", ""),
        "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
        "superseded": _superseded(meta), "superseded_by": meta.get("superseded_by"),
        "source": str(path), "text": text,
    }


def load_corpus(corpus_dir: Path | str) -> list[dict]:
    root = Path(corpus_dir)
    docs = [load_markdown(p) for p in sorted(root.rglob("*.md"))
            if p.name.lower() != "readme.md"]
    docs += [load_html(p) for p in sorted(root.rglob("*.html")) + sorted(root.rglob("*.htm"))]
    seen: set[str] = set()
    for d in docs:
        if d["doc_id"] in seen:
            raise ValueError(f"duplicate doc id {d['doc_id']!r} ({d['source']})")
        seen.add(d["doc_id"])
    return [d for d in docs if d["text"]]


# ---------------- chunking ----------------
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)


def split_sections(text: str) -> list[tuple[str, str]]:
    """-> [(section heading path, body)]. Text before the first heading gets section ''."""
    out, path = [], {}
    marks = list(HEADING.finditer(text))
    pre = text[:marks[0].start()] if marks else text
    if pre.strip():
        out.append(("", pre.strip()))
    for i, m in enumerate(marks):
        level = len(m.group(1))
        path = {k: v for k, v in path.items() if k < level}
        path[level] = m.group(2).strip()
        body = text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
        if body:
            out.append((" > ".join(path[k] for k in sorted(path)), body))
    return out


_splitter = None


def _split(text: str) -> list[str]:
    global _splitter
    if _splitter is None:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        _splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_TOKENS, chunk_overlap=OVERLAP_TOKENS, length_function=count_tokens,
            separators=["\n\n", "\n", ". ", " ", ""])
    return _splitter.split_text(text)


def chunk_doc(doc: dict) -> list[dict]:
    if doc["doc_type"] in WHOLE_DOC_TYPES:
        pieces = [("", doc["text"])]
    else:
        pieces = []
        for section, body in split_sections(doc["text"]):
            if doc["doc_type"] in WHOLE_SECTION_TYPES or count_tokens(body) <= CHUNK_TOKENS:
                pieces.append((section, body))
            else:
                pieces += [(section, part) for part in _split(body)]
    keep = ("doc_id", "title", "doc_type", "updated", "superseded", "tags")
    return [{"chunk_id": f"{doc['doc_id']}#{i}", "section": section, "text": body,
             **{k: doc[k] for k in keep}}
            for i, (section, body) in enumerate(pieces)]


def chunk_corpus(docs: list[dict]) -> list[dict]:
    return [c for d in docs for c in chunk_doc(d)]


def embed_text(chunk: dict) -> str:
    """What gets embedded / BM25-indexed: title + section give the chunk its context."""
    head = chunk["title"] + (f" | {chunk['section']}" if chunk["section"] else "")
    return f"{head}\n{chunk['text']}"


def to_document(chunk: dict) -> Document:
    """Chunk dict -> LangChain Document (text in page_content, everything else in metadata)."""
    return Document(page_content=chunk["text"], id=chunk["chunk_id"],
                    metadata={k: v for k, v in chunk.items() if k != "text"})


def from_document(doc: Document) -> dict:
    """Document -> the flat dict the graph state, ARL log and UIs serialise."""
    return {**doc.metadata, "text": doc.page_content}


def chunk_documents(docs: list[dict]) -> list[Document]:
    return [to_document(c) for c in chunk_corpus(docs)]
