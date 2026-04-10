#!/usr/bin/env python3
"""OsO Sync responder — Groq primary, local Ollama fallback.

Watches the Syncthing-synced notes/ask/ dir for question files. For each
unprocessed .md it POSTs the content to Groq (llama-3.3-70b-versatile by
default) which returns in 1-2s at ~500 tok/s. On Groq failure (rate limit,
network, provider down) it falls back to local Ollama on 127.0.0.1 (slower
but self-hosted). The answer is appended in-place with a sentinel so it is
not re-processed, and Syncthing fans the updated file out to every paired
device automatically.

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

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a concise assistant. Keep responses short and actionable. "
    "Use markdown formatting. The user writes questions from their phone "
    "or laptop via Syncthing; answers sync back automatically. Prefer "
    "crisp lists over paragraphs.",
)
MAX_TOKENS = 600
OLLAMA_TIMEOUT = 90
GROQ_TIMEOUT = 30
SENTINEL = "<!-- responder-processed -->"
# Legacy sentinels written by earlier versions of the responder.
# Recognize them so we don't double-answer historic files when OsO Sync
# takes over from a previous local-only setup.
LEGACY_SENTINELS = ("<!-- ollama-responded -->",)


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def query_ollama(question: str) -> str | None:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": question,
        "system": SYSTEM_PROMPT,
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


def query_groq(question: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
    }
    # Cloudflare (fronting api.groq.com) returns 403 error:1010 if the UA
    # looks like "Python-urllib/3.x" — it flags it as a banned signature.
    # Any non-default UA is accepted.
    req = Request(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "oso-sync/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=GROQ_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip() or None
    except (URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
        log(f"groq failed: {type(e).__name__}: {e}")
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

    # Groq primary (fast, 70B, hosted). Ollama fallback (slower, 8B, self-hosted)
    # takes over on Groq rate-limit, outage, or missing key.
    backend = "groq"
    answer = query_groq(question) if GROQ_API_KEY else None
    if answer is None:
        if GROQ_API_KEY:
            log(f"groq failed, falling back to ollama for {fpath.name}")
        answer = query_ollama(question)
        backend = "ollama"

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
