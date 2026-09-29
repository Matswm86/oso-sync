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
        if any(m in body for m in SKIP_MARKERS):
            continue
        for start, text in chunk_text(body):
            pending.append({"path": key, "start": start, "text": text})

    log(f"{len(files)} files ({reused} unchanged), {len(pending)} new chunks to embed")
    t0 = time.time()
    for i in range(0, len(pending), EMBED_BATCH):
        batch = pending[i : i + EMBED_BATCH]
        vecs = embed([f"search_document: {Path(c['path']).stem}\n{c['text']}" for c in batch])
        if vecs is None:
            log("stopping: embedding backend failed; index left unchanged")
            return 2
        chunks.extend(batch)
        rows.extend(np.asarray(vecs, dtype=np.float32))
        done = i + len(batch)
        if done % (EMBED_BATCH * 20) == 0:
            log(f"embedded {done}/{len(pending)} in {time.time() - t0:.0f}s")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    mat = _normalise(np.vstack(rows)) if rows else np.zeros((0, 768), dtype=np.float32)
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


def search(
    question: str, top_k: int, per_file: int = 2, exclude_path: Path | None = None
) -> list[dict] | None:
    """Return top chunks for question, or None when the index is unusable."""
    meta, vecs = load_index()
    if meta is None or len(vecs) == 0:
        return None
    q = embed([f"search_query: {question}"], timeout=30)
    if q is None:
        return None
    qv = np.asarray(q[0], dtype=np.float32)
    qv /= np.linalg.norm(qv) or 1.0
    scores = vecs @ qv
    # Exact-term bonus so IDs and project names that embeddings blur still rank.
    terms = {t for t in re.findall(r"[a-z0-9][a-z0-9_.-]{3,}", question.lower())}
    order = np.argsort(-scores)[: top_k * 20]
    ranked = []
    for i in order:
        c = meta["chunks"][int(i)]
        text = c["text"].lower()
        bonus = 0.02 * min(sum(1 for t in terms if t in text), 5)
        ranked.append((float(scores[i]) + bonus, c))
    ranked.sort(key=lambda t: t[0], reverse=True)
    skip = str(exclude_path.resolve()) if exclude_path else None
    out: list[dict] = []
    seen: dict[str, int] = {}
    for score, c in ranked:
        if skip and str(Path(c["path"]).resolve()) == skip:
            continue
        if seen.get(c["path"], 0) >= per_file:
            continue
        seen[c["path"]] = seen.get(c["path"], 0) + 1
        out.append({**c, "score": round(score, 3)})
        if len(out) >= top_k:
            break
    return out


if __name__ == "__main__":
    sys.exit(build())
