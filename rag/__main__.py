"""python -m rag ask "..." | python -m rag index | python -m rag cost"""
import argparse
import json
import sys

from . import llm as llm_mod
from .pipeline import CORPUS_DIR, ask, build_index


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m rag")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("query")
    a.add_argument("--json", action="store_true", help="print the full result dict")
    i = sub.add_parser("index")
    i.add_argument("--corpus", default=str(CORPUS_DIR))
    sub.add_parser("cost")
    args = p.parse_args(argv)

    if args.cmd == "index":
        idx = build_index(args.corpus)
        docs = {c["doc_id"] for c in idx.chunks}
        print(f"indexed {len(docs)} docs -> {len(idx.chunks)} chunks; "
              f"spend so far ${llm_mod.LLM().spent():.6f}")
    elif args.cmd == "ask":
        r = ask(args.query)
        if args.json:
            print(json.dumps(r, indent=2, default=str))
        else:
            print(f"[{r['route']}] conf={r['confidence']} cost=${r['cost_usd']:.6f} "
                  f"{r['timings'].get('total')}ms\n\n{r['answer']}")
            if r["citations"]:
                print("\nsources: " + ", ".join(r["citations"]))
    else:
        print(json.dumps(llm_mod.summary(), indent=2))


if __name__ == "__main__":
    sys.exit(main())
