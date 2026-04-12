#!/usr/bin/env python3
"""OsO Sync responder — Groq primary, local Ollama fallback.

Watches the Syncthing-synced notes/ask/ dir for question files. For each
unprocessed .md it POSTs the content to Groq (llama-3.3-70b-versatile by
default) which returns in 1-2s at ~500 tok/s. On Groq failure (rate limit,
network, provider down) it falls back to local Ollama on 127.0.0.1 (slower
but self-hosted). The answer is appended in-place with a sentinel so it is
not re-processed, and Syncthing fans the updated file out to every paired
device automatically.

Context injection (2026-04-13):
  - CONTEXT_FILE (default /etc/oso-sync/obsidian-context.md): static workspace
    brief loaded once at startup and prepended to the system prompt so the
    model knows the user's projects, stack, conventions, and identity.
  - CONTEXT_DIR (default ~/sync/notes): scanned per-question with a cheap
    keyword-overlap score to pull top-5 relevant note snippets as dynamic
    context. Pure-local RAG, zero API cost.

Why Groq-primary on an always-on VPS:
  - ~100x faster than CPU Ollama (1-2s vs 5-10s per request)
  - 70B params vs 8B — much better at nuanced questions
  - Frees VPS CPU for the other vhosts sharing the box
  - Free tier (30 req/min) easily covers personal notes scale
Ollama stays as disaster-recovery fallback: real outages, rate limits,
or running offline during Groq incidents.

Runs as a systemd timer (60s cadence). Stdlib-only, no venv.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

NOTES_ASK_DIR = Path(
    os.environ.get("NOTES_ASK_DIR", str(Path.home() / "sync" / "notes" / "ask"))
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

# Model swap: GROQ_REASONING_MODEL activates for questions flagged as
# reasoning-heavy by _looks_like_reasoning(). Free on Groq, slower but
# much better at planning/comparison/analysis.
GROQ_REASONING_MODEL = os.environ.get(
    "GROQ_REASONING_MODEL", "deepseek-r1-distill-llama-70b"
)

BASE_SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a concise assistant embedded in the user's Obsidian notes "
    "vault. Keep responses short and actionable. Use markdown formatting. "
    "Answers sync back automatically via Syncthing. Prefer crisp lists "
    "over paragraphs. When workspace context is provided, ground answers "
    "in it; if a question is outside that context, say so briefly rather "
    "than guessing.",
)

CONTEXT_FILE = Path(
    os.environ.get("CONTEXT_FILE", "/etc/oso-sync/obsidian-context.md")
)
CONTEXT_DIR = Path(
    os.environ.get("CONTEXT_DIR", str(Path.home() / "sync" / "notes"))
)
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
RAG_SNIPPET_CHARS = int(os.environ.get("RAG_SNIPPET_CHARS", "600"))
RAG_MAX_FILES = int(os.environ.get("RAG_MAX_FILES", "400"))

MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2000"))
OLLAMA_TIMEOUT = 120
GROQ_TIMEOUT = 45
SENTINEL = "<!-- responder-processed -->"
LEGACY_SENTINELS = ("<!-- ollama-responded -->",)

_STOPWORDS = frozenset(
    "the a an and or of to in on for is are was were be been being with "
    "this that these those it its as at by from into up out so do does did "
    "not no if then when while i you he she we they me him her us them "
    "what which who whose how why where there here my your our their his "
    "can could would should may might will shall just also about over".split()
)


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def _load_static_context() -> str:
    """Load the always-on workspace brief once per invocation."""
    try:
        text = CONTEXT_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not text:
        return ""
    return f"\n\n## Workspace context (static)\n\n{text}"


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _rag_snippets(question: str, self_path: Path | None) -> str:
    """Keyword-score markdown files in CONTEXT_DIR, return top-K excerpts."""
    if not CONTEXT_DIR.is_dir():
        return ""
    q_tokens = _tokenize(question)
    if not q_tokens:
        return ""
    scored: list[tuple[int, Path, str]] = []
    count = 0
    for fp in CONTEXT_DIR.rglob("*.md"):
        if count >= RAG_MAX_FILES:
            break
        count += 1
        if self_path is not None and fp.resolve() == self_path.resolve():
            continue
        try:
            body = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SENTINEL in body or any(s in body for s in LEGACY_SENTINELS):
            # Skip already-answered Q&A files; they'd echo prior answers
            # into every follow-up and poison the retrieval.
            continue
        doc_tokens = _tokenize(body)
        score = len(q_tokens & doc_tokens)
        if score >= 2:
            scored.append((score, fp, body))
    if not scored:
        return ""
    scored.sort(key=lambda t: t[0], reverse=True)
    blocks: list[str] = []
    for score, fp, body in scored[:RAG_TOP_K]:
        # Find the first chunk that contains the most query tokens
        best_start, best_hits = 0, 0
        step = max(RAG_SNIPPET_CHARS // 2, 100)
        for start in range(0, len(body), step):
            window = body[start : start + RAG_SNIPPET_CHARS]
            hits = sum(1 for t in q_tokens if t in window.lower())
            if hits > best_hits:
                best_hits = hits
                best_start = start
            if hits == len(q_tokens):
                break
        excerpt = body[best_start : best_start + RAG_SNIPPET_CHARS].strip()
        rel = fp.name
        try:
            rel = str(fp.relative_to(CONTEXT_DIR))
        except ValueError:
            pass
        blocks.append(f"### {rel} (score={score})\n\n{excerpt}")
    return "\n\n## Notes context (retrieved)\n\n" + "\n\n---\n\n".join(blocks)


def _build_system_prompt(question: str, self_path: Path | None) -> str:
    parts = [BASE_SYSTEM_PROMPT, _load_static_context(), _rag_snippets(question, self_path)]
    return "\n".join(p for p in parts if p).strip()


def _looks_like_reasoning(question: str) -> bool:
    q = question.lower()
    triggers = (
        "why",
        "how should",
        "compare",
        "trade-off",
        "tradeoff",
        "pros and cons",
        "plan ",
        "design ",
        "derive",
        "prove",
        "reason",
        "step by step",
        "explain why",
    )
    return any(t in q for t in triggers) or len(question) > 1200


def query_ollama(question: str, system_prompt: str) -> str | None:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": question,
        "system": system_prompt,
        "stream": False,
        "options": {"num_predict": MAX_TOKENS},
    }
    req = Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "").strip() or None
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        log(f"ollama failed: {type(e).__name__}: {e}")
        return None


def query_groq(question: str, system_prompt: str, model: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }
    # Cloudflare (fronting api.groq.com) returns 403 error:1010 if the UA
    # looks like "Python-urllib/3.x" — it flags it as a banned signature.
    req = Request(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "oso-sync/0.2",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=GROQ_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"].strip() or None
            # deepseek-r1-distill emits <think>…</think> reasoning; strip it
            # so Obsidian doesn't get a wall of chain-of-thought noise.
            if content and "<think>" in content:
                content = re.sub(
                    r"<think>.*?</think>\s*", "", content, flags=re.DOTALL
                ).strip()
            return content
    except (URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
        log(f"groq failed ({model}): {type(e).__name__}: {e}")
        return None


def process_file(fpath: Path, dry_run: bool) -> bool:
    try:
        content = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log(f"read failed {fpath.name}: {e}")
        return False

    if SENTINEL in content:
        return False
    if any(legacy in content for legacy in LEGACY_SENTINELS):
        return False

    question = content.strip()
    if not question:
        log(f"skip empty {fpath.name}")
        return False

    log(f"processing {fpath.name}")
    system_prompt = _build_system_prompt(question, fpath)
    model = GROQ_REASONING_MODEL if _looks_like_reasoning(question) else GROQ_MODEL

    backend = f"groq:{model}"
    answer = query_groq(question, system_prompt, model) if GROQ_API_KEY else None
    if answer is None and GROQ_API_KEY and model != GROQ_MODEL:
        log(f"reasoning model failed, retrying with {GROQ_MODEL}")
        answer = query_groq(question, system_prompt, GROQ_MODEL)
        backend = f"groq:{GROQ_MODEL}"
    if answer is None:
        if GROQ_API_KEY:
            log(f"groq failed, falling back to ollama for {fpath.name}")
        answer = query_ollama(question, system_prompt)
        backend = f"ollama:{OLLAMA_MODEL}"

    if answer is None:
        log(f"both backends failed for {fpath.name}; leaving unprocessed")
        return False

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = f"\n\n---\n\n**🤖 {backend}** · {timestamp}\n\n{answer}\n\n{SENTINEL}\n"
    new_content = content.rstrip() + block

    if dry_run:
        print(f"--- DRY RUN: {fpath.name} ({backend}) ---\n{block}")
        return True

    try:
        fpath.write_text(new_content, encoding="utf-8")
        log(f"updated {fpath.name} via {backend}")
        return True
    except OSError as e:
        log(f"write failed {fpath.name}: {e}")
        return False


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    NOTES_ASK_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(NOTES_ASK_DIR.glob("*.md"))
    if not files:
        return  # silent no-op, keeps systemd journal clean

    log(f"found {len(files)} file(s) in {NOTES_ASK_DIR}")
    updated = sum(1 for f in files if process_file(f, dry_run))
    if updated:
        log(f"updated {updated}/{len(files)}")


if __name__ == "__main__":
    main()
