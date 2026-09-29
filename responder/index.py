#!/usr/bin/env python3
"""Embedding index for the OsO responder.

Builds and queries a vector index over the markdown files in CONTEXT_DIRS,
using an Ollama embedding model (default nomic-embed-text). Run as a script
to build or refresh the index; the responder imports search() at answer time.

Refresh is incremental: a file whose size and mtime are unchanged keeps its
stored vectors, so a run with no changes makes no embedding calls.

Index layout in RAG_INDEX_DIR:
  vectors.npy  float32 matrix, one L2-normalised row per chunk
  chunks.json  {"model": ..., "files": {path: [size, mtime]}, "chunks": [...]}
"""

import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    import numpy as np
except ImportError:  # responder falls back to keyword search
    np = None

EMBED_URL = os.environ.get("OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embed")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
INDEX_DIR = Path(
    os.environ.get("RAG_INDEX_DIR", str(Path.home() / "services" / "responder-context" / "index"))
)
CHUNK_CHARS = int(os.environ.get("RAG_CHUNK_CHARS", "1500"))
MAX_CHUNKS_PER_FILE = int(os.environ.get("RAG_MAX_CHUNKS_PER_FILE", "10"))
EMBED_BATCH = int(os.environ.get("RAG_EMBED_BATCH", "32"))
# Model-specific text prefixes; the defaults are nomic-embed-text's.
DOC_PREFIX = os.environ.get("EMBED_DOC_PREFIX", "search_document: ")
QUERY_PREFIX = os.environ.get("EMBED_QUERY_PREFIX", "search_query: ").replace("\\n", "\n")
# Colon-separated globs matched against each path relative to its context dir
# and against every directory name in it.
EXCLUDE = [
    p
    for p in os.environ.get("RAG_EXCLUDE", "archive:archived:logs:ask:*sync-conflict*").split(":")
    if p
]
SKIP_MARKERS = ("<!-- responder-processed -->", "<!-- ollama-responded -->")


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] index: {msg}", flush=True)


def context_dirs() -> list[Path]:
    raw = os.environ.get("CONTEXT_DIRS", "").strip()
    if raw:
        return [Path(p).expanduser() for p in raw.split(":") if p]
    return [Path(os.environ.get("CONTEXT_DIR", str(Path.home() / "sync" / "notes")))]


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    for pat in EXCLUDE:
        if fnmatch.fnmatch(str(rel), pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def iter_files() -> list[Path]:
    files: list[Path] = []
    for root in context_dirs():
        if not root.is_dir():
            continue
        for fp in sorted(root.rglob("*.md")):
            if not _excluded(fp.relative_to(root)):
                files.append(fp)
    return files


def chunk_text(body: str) -> list[tuple[int, str]]:
    """Split on markdown headings, then pack sections into ~CHUNK_CHARS pieces."""
    sections: list[tuple[int, str]] = []
    starts = [m.start() for m in re.finditer(r"^#{1,4} ", body, flags=re.MULTILINE)]
    bounds = [0, *[s for s in starts if s > 0], len(body)]
    for a, b in zip(bounds, bounds[1:]):
        sec = body[a:b]
        for off in range(0, len(sec), CHUNK_CHARS):
            piece = sec[off : off + CHUNK_CHARS]
            if piece.strip():
                sections.append((a + off, piece))
    chunks: list[tuple[int, str]] = []
    for start, piece in sections:
        if chunks and len(chunks[-1][1]) + len(piece) <= CHUNK_CHARS:
            chunks[-1] = (chunks[-1][0], chunks[-1][1] + piece)
        else:
            chunks.append((start, piece))
    return chunks[:MAX_CHUNKS_PER_FILE]


def embed(texts: list[str], timeout: int = 300) -> list[list[float]] | None:
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = Request(
        EMBED_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["embeddings"]
    except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
        log(f"embed failed ({EMBED_MODEL}): {type(e).__name__}: {e}")
        return None


def _normalise(mat):
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


def load_index():
    meta_path, vec_path = INDEX_DIR / "chunks.json", INDEX_DIR / "vectors.npy"
    if np is None or not meta_path.exists() or not vec_path.exists():
        return None, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        vecs = np.load(vec_path)
    except (OSError, ValueError) as e:
        log(f"index unreadable: {type(e).__name__}: {e}")
        return None, None
    if meta.get("model") != EMBED_MODEL or len(meta.get("chunks", [])) != len(vecs):
        return None, None
    return meta, vecs


def build() -> int:
    if np is None:
        log("numpy missing; cannot build index")
        return 1
    old_meta, old_vecs = load_index()
    old_files = old_meta["files"] if old_meta else {}
    kept_rows: dict[str, list[int]] = {}
    if old_meta:
        for i, c in enumerate(old_meta["chunks"]):
            kept_rows.setdefault(c["path"], []).append(i)

    files: dict[str, list[float]] = {}
    chunks: list[dict] = []
    rows: list = []
    pending: list[dict] = []
    reused = 0
    for fp in iter_files():
        try:
            st = fp.stat()
            key = str(fp)
            sig = [st.st_size, st.st_mtime]
            if old_files.get(key) == sig and key in kept_rows:
                for i in kept_rows[key]:
                    chunks.append(old_meta["chunks"][i])
                    rows.append(old_vecs[i])
                files[key] = sig
                reused += 1
                continue
            body = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files[key] = sig
        # Answered questions end with the responder's marker; notes that only
        # mention the marker are indexed.
        if body.rstrip().endswith(SKIP_MARKERS):
            continue
        for start, text in chunk_text(body):
            pending.append({"path": key, "start": start, "text": text})

    log(f"{len(files)} files ({reused} unchanged), {len(pending)} new chunks to embed")
    t0 = time.time()
    for i in range(0, len(pending), EMBED_BATCH):
        batch = pending[i : i + EMBED_BATCH]
        vecs = embed([f"{DOC_PREFIX}{Path(c['path']).stem}\n{c['text']}" for c in batch])
        if vecs is None:
            log("stopping: embedding backend failed; index left unchanged")
            return 2
        chunks.extend(batch)
        rows.extend(np.asarray(vecs, dtype=np.float32))
        done = i + len(batch)
        if done % (EMBED_BATCH * 20) == 0:
            log(f"embedded {done}/{len(pending)} in {time.time() - t0:.0f}s")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    mat = _normalise(np.vstack(rows)) if rows else np.zeros((0, 1), dtype=np.float32)
    tmp_vec, tmp_meta = INDEX_DIR / "vectors.tmp.npy", INDEX_DIR / "chunks.tmp.json"
    np.save(tmp_vec, mat)
    tmp_meta.write_text(
        json.dumps({"model": EMBED_MODEL, "files": files, "chunks": chunks}),
        encoding="utf-8",
    )
    # Vectors first, then metadata: load_index() rejects a length mismatch.
    tmp_vec.replace(INDEX_DIR / "vectors.npy")
    tmp_meta.replace(INDEX_DIR / "chunks.json")
    log(f"wrote {len(chunks)} chunks from {len(files)} files in {time.time() - t0:.0f}s")
    return 0


_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "what",
        "which",
        "who",
        "how",
        "why",
        "when",
        "where",
        "does",
        "do",
        "did",
        "my",
        "our",
        "your",
        "we",
        "i",
        "you",
        "it",
        "its",
        "this",
        "that",
        "with",
        "from",
        "about",
        "should",
        "can",
        "could",
        "would",
        "will",
        "have",
        "has",
        "had",
        "any",
        "all",
    ]
)
RRF_K = 60


def _terms(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1]


def _file_head(text: str) -> str:
    """Frontmatter name/description and first heading of a file's first chunk."""
    lines = [ln for ln in text.splitlines()[:15] if ln.startswith(("name:", "description:", "# "))]
    return " ".join(lines)


def _bm25(question: str, chunks: list[dict], k1: float = 1.5, b: float = 0.75):
    """BM25 over chunk text plus file name, so names and IDs rank by exact match."""
    q = set(_terms(question))
    if not q:
        return np.zeros(len(chunks), dtype=np.float32)
    heads = {c["path"]: _file_head(c["text"]) for c in chunks if c["start"] == 0}
    docs = [
        _terms(f"{Path(c['path']).stem} {heads.get(c['path'], '')} {c['text']}") for c in chunks
    ]
    lens = np.array([len(d) for d in docs], dtype=np.float32)
    avg = float(lens.mean()) or 1.0
    df = {t: 0 for t in q}
    tfs = []
    for d in docs:
        tf: dict[str, int] = {}
        for t in d:
            if t in q:
                tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] += 1
        tfs.append(tf)
    n = len(docs)
    idf = {t: np.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    scores = np.zeros(n, dtype=np.float32)
    for i, tf in enumerate(tfs):
        norm = k1 * (1 - b + b * lens[i] / avg)
        scores[i] = sum(idf[t] * f * (k1 + 1) / (f + norm) for t, f in tf.items())
    return scores


def search(
    question: str, top_k: int, per_file: int = 1, exclude_path: Path | None = None
) -> list[dict] | None:
    """Top chunks by reciprocal-rank fusion of embedding and BM25 rankings.

    Returns None when the index or the embedding backend is unusable.
    """
    meta, vecs = load_index()
    if meta is None or len(vecs) == 0:
        return None
    q = embed([f"{QUERY_PREFIX}{question}"], timeout=30)
    if q is None:
        return None
    qv = np.asarray(q[0], dtype=np.float32)
    qv /= np.linalg.norm(qv) or 1.0
    chunks = meta["chunks"]
    fused = np.zeros(len(chunks), dtype=np.float64)
    for scores in (vecs @ qv, _bm25(question, chunks)):
        order = np.argsort(-scores)[:200]
        for rank, i in enumerate(order):
            if scores[i] > 0:
                fused[i] += 1.0 / (RRF_K + rank)
    skip = str(exclude_path.resolve()) if exclude_path else None
    out: list[dict] = []
    seen: dict[str, int] = {}
    for i in np.argsort(-fused):
        if fused[i] <= 0:
            break
        c = chunks[int(i)]
        if skip and str(Path(c["path"]).resolve()) == skip:
            continue
        if seen.get(c["path"], 0) >= per_file:
            continue
        seen[c["path"]] = seen.get(c["path"], 0) + 1
        out.append({**c, "score": round(float(fused[i]) * 100, 2)})
        if len(out) >= top_k:
            break
    return out


if __name__ == "__main__":
    sys.exit(build())
