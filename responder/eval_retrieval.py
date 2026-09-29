#!/usr/bin/env python3
"""Score retrieval against a list of control questions.

Usage: eval_retrieval.py QUESTIONS.json [--keyword]

QUESTIONS.json is a list of {"q": question, "expect": [substring, ...]}; a
question counts as a hit when any expected substring appears in the path of
one of the top RAG_TOP_K results. Prints per-question ranks, hit@k and mean
reciprocal rank. The index and model come from the same env vars as the
responder (RAG_INDEX_DIR, EMBED_MODEL, EMBED_*_PREFIX). --keyword scores the
responder's keyword fallback instead of the index.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index  # noqa: E402
import responder  # noqa: E402


def ranked_paths(question: str, keyword: bool) -> list[str]:
    k = responder.RAG_TOP_K
    if keyword:
        text = responder._keyword_snippets(question, None)
        return re.findall(r"^### (\S+) \(score=", text, flags=re.MULTILINE)[:k]
    hits = index.search(question, k) or []
    return [h["path"] for h in hits]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    keyword = "--keyword" in sys.argv
    questions = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    hits, rr = 0, 0.0
    for item in questions:
        paths = [p.lower() for p in ranked_paths(item["q"], keyword)]
        rank = next(
            (i + 1 for i, p in enumerate(paths) if any(e.lower() in p for e in item["expect"])),
            None,
        )
        if rank:
            hits += 1
            rr += 1 / rank
        print(f"{rank or '-':>2}  {item['q']}")
    n = len(questions)
    label = "keyword" if keyword else f"hybrid:{index.EMBED_MODEL}"
    print(f"\n{label}: hit@{responder.RAG_TOP_K} {hits}/{n} = {hits / n:.0%}, MRR {rr / n:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
