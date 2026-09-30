# OsO Sync

**O**bsidian · **S**yncthing · **O**llama — a three-piece, zero-cloud, always-on personal AI notes stack.

Write a question into `notes/ask/foo.md` on your phone, laptop, or desktop. A minute later the answer is appended in-place, the file is fanned out to every device you own, and you've spent approximately zero cents. No vendor lock-in, no custom apps, no subscription — just three open-source tools wired together, with Groq as the fast primary LLM and self-hosted Ollama as the always-there fallback.

**You don't need a VPS.** The responder is a Python script that needs nothing beyond the standard library (only the optional notes index adds `numpy`); it runs on whatever "always-on device" you pick — a cheap VPS, a home server, a Raspberry Pi, or just your workstation. See [Deployment modes](#deployment-modes) below for three topologies, from single-laptop local-only up to multi-device cloud mesh.

## What it does

```
┌───────────┐     notes/ask/foo.md   ┌─────────────┐     notes/ask/foo.md   ┌─────────────┐
│   PHONE   │ ───────────────────▶  │  ALWAYS-ON  │ ◀─────────────────────│ WORKSTATION │
│ (Obsidian │     (Syncthing BEP)    │    HOST     │     (Syncthing BEP)    │  (Obsidian) │
│  + Sync)  │                        │ (VPS/Pi/PC) │                        │   + Sync)   │
└───────────┘                        │   ollama    │                        └─────────────┘
      ▲                              │  llama3.1   │                                ▲
      │                              │      +      │                                │
      │                              │  responder  │                                │
      │                              │  (60s tick) │                                │
      │                              │      │      │                                │
      │                              │      ▼      │                                │
      │   answer appended in-place   │   rewrites  │   answer appended in-place     │
      └──────────────────────────────│   the file  │────────────────────────────────┘
          (Syncthing BEP)            └─────────────┘        (Syncthing BEP)
```

1. You write a question on any paired device (phone, workstation, tablet, second laptop…)
2. Syncthing propagates it to the always-on host over BEP
3. A systemd timer on that host fires every 60s and runs the responder
4. Responder queries Groq (primary) with Ollama as the fallback, and appends the answer
5. Syncthing propagates the updated file back to every device

Total latency: ~2–60 seconds depending on where you are in the polling cycle.

If you run **local-only** (one laptop, no other devices), steps 2 and 5 collapse into a local filesystem write — no Syncthing needed. The responder just polls a directory on the same machine.

## The three tools

The name **OsO** comes from **O**bsidian · **s**yncthing · **O**llama — two capital O's with a lowercase s for Syncthing in the middle.

- **Obsidian** — the note-taking app. Your vault lives in plain markdown files in a folder. That's the only contract. Any markdown-vault app works.
- **Syncthing** — open-source, end-to-end-encrypted file sync over a P2P mesh. No central server required; the VPS is just "a device that's always on". Zero vendor dependency.
- **Ollama** — local LLM runtime. On the VPS it runs `llama3.1:8b` on CPU as the fallback when Groq is rate-limited or down, and it can also serve `nomic-embed-text` embeddings for the optional notes index (see [Grounding the model in your own context](#grounding-the-model-in-your-own-context)). On your workstation you can point at bigger local models; on your phone there is no Ollama — the VPS handles mobile.

The responder uses **Groq `qwen/qwen3.8-27b` as the primary** LLM: hosted, fast (~500 tok/s), and free for personal-scale use. Questions that look reasoning-heavy (they contain a trigger such as "why", "compare", "trade-off", "design" or "step by step", or run past 1200 characters) go to `openai/gpt-oss-120b` instead. Groq rate limits are per model, so when a call fails the responder tries the primary model and then the models in `GROQ_FALLBACK_MODELS` (default `openai/gpt-oss-20b`); a 429 that asks for a wait of `GROQ_MAX_RETRY_WAIT` seconds or less (default 10) is retried once on the same model. Ollama only engages if every Groq model fails or the key is unset, and `OLLAMA_FALLBACK=0` turns even that off so the note waits for the next tick. This was a deliberate flip from an early "Ollama-primary" design once it was clear that on an always-online VPS with an existing Groq free tier, Groq-primary dominates on every axis (speed, quality, CPU headroom) while Ollama remains invaluable as disaster-recovery insurance.

Groq retires models from time to time (it retired `llama-3.3-70b-versatile` and `deepseek-r1-distill-llama-70b`, the earlier defaults here, which made every request fail with HTTP 404). If your `obsidian.env` still names a retired model, set `GROQ_MODEL` and `GROQ_REASONING_MODEL` to ones Groq currently serves.

The clever bit: none of these three tools know about each other. They're glued together by a ~400-line Python script (`responder/responder.py`) that polls the synced folder and writes answers back. The glue has no dependencies outside the Python stdlib; the optional notes index (`responder/index.py`) adds `numpy`.

## Deployment modes

Pick whichever matches the hardware you already own. The responder code is identical in all three — only the install target changes.

### 1. Local-only (one machine, no sync needed)

Simplest mode. The responder runs on your laptop/desktop and watches a local folder. Good for: getting started, single-device use, fully air-gapped setups.

- ✅ Zero network dependencies — works on a plane
- ✅ No Syncthing, no SSH, no VPS
- ✅ Ollama-only mode works offline too (leave `GROQ_API_KEY` unset)
- ❌ Question only answered when the machine is awake
- ❌ No phone access

Install: `systemctl --user enable --now oso-responder.timer` after pointing `NOTES_ASK_DIR` at any local folder you like (e.g. `~/vault/ask/`).

### 2. Two-device (workstation ↔ phone, no VPS)

Your workstation is the always-on host AND one of the edit surfaces. The phone pairs directly with the workstation via Syncthing. Good for: users whose workstation is usually on, who don't want to rent a VPS.

- ✅ No rental cost at all
- ✅ Bigger local models possible (workstation has real RAM/GPU)
- ❌ Questions written on the phone only get answered when the workstation is on and on the same network (or reachable over a relay / VPN)
- ❌ Workstation needs to stay paired with the phone even when you're travelling

Install: same as Local-only, plus the Syncthing pairing from [Step 2](#2-workstation-side--pair-with-host-share-the-notes-folder) below — but pair phone ↔ workstation instead of phone ↔ VPS.

### 3. Multi-device mesh with a dedicated always-on host (the quickstart below)

A small VPS, a home server, or a Raspberry Pi sits between your devices and runs the responder 24/7. Good for: multiple edit devices, reliable mobile answers, already-owned always-on hardware.

- ✅ Phone gets answers even when workstation is off
- ✅ Multi-device mesh — add a second laptop, a tablet, etc. and they all just work
- ✅ One Groq key, one responder, one log to check
- ❌ ~€5–11/month if you rent a VPS (€0 if you already have one)
- ❌ Initial pairing is 3 devices instead of 1 or 2

The quickstart below walks through mode 3 because it's the richest; the other two are subsets. If you only want mode 1 or 2, skip the VPS SSH blocks and substitute "workstation" or "this machine" for "VPS" throughout.

## Project layout

```
oso-sync/
├── README.md                  this file
├── responder/
│   ├── responder.py           the glue: polls notes/ask/, adds context, queries Groq/Ollama, appends answer
│   ├── index.py               optional embedding + BM25 index over CONTEXT_DIRS (build it by running the script)
│   ├── eval_retrieval.py      scores retrieval against a JSON list of control questions
│   ├── embed_trial.sh         builds a second index with another embedding model and compares it to the live one
│   └── .env.example           template for /etc/oso-sync/obsidian.env on the VPS
├── systemd/
│   ├── oso-responder.service  systemd user unit for the responder (uses %h specifier)
│   ├── oso-responder.timer    60s cadence timer
│   ├── oso-indexer.service    systemd user unit that refreshes the embedding index
│   └── oso-indexer.timer      hourly refresh timer
├── syncthing/
│   └── DEVICES.md             template ledger for the paired-devices mesh
├── docs/
│   ├── architecture.md        deeper walkthrough, data flow, extension points
│   └── phone-pairing.md       manual phone ↔ VPS pairing (Syncthing app steps)
└── deploy/
    ├── install-vps.sh         one-shot deploy from the repo to your VPS
    ├── status.sh              whole-stack health check
    ├── obsidian-context.md.example   template for the static workspace brief (CONTEXT_FILE)
    ├── sync-memory-to-vps.sh  rsync a local folder of markdown notes (and optionally project docs) to the host for CONTEXT_DIRS
    ├── collect-project-docs.sh       gather project READMEs and CLAUDE.md files into one folder for that sync
    ├── generate-system-facts.sh      weekly hardware/OS snapshot (markdown) for the responder's RAG
    └── systemd-workstation/   workstation-side user units: oso-memory-sync.{service,timer} (48h),
                               oso-system-facts.{service,timer} (weekly)
```

## Quick start

### Prerequisites

- An **always-on Linux host** — a cheap VPS (€5–11/month), a home server, a Raspberry Pi 4/5, or your own workstation. Anything with 2+ GB RAM, Ubuntu/Debian, and systemd works. (For local-only mode, this is just your own machine — skip the VPS-specific steps.)
- Syncthing on every device you want to write notes from (skip if local-only)
- Phone with the [Syncthing-Fork Android app](https://f-droid.org/packages/com.github.catfriend1.syncthingandroid/) if you want phone access
- Obsidian installed wherever you want to edit (or any other markdown-vault app that uses plain files)
- A free [Groq API key](https://console.groq.com/keys) (optional — without one, you run Ollama-only)

Throughout this README I use a shell variable for the always-on host so you only fill it in once. For **VPS mode** this is an SSH target; for **local mode** leave it empty and skip every `ssh $VPS …` block (just run the inner command directly).

```bash
VPS=youruser@vps.example.com       # VPS mode: your SSH target
# VPS=                             # Local mode: leave empty, skip ssh $VPS blocks
```

### 1. Host side — install Ollama, Syncthing, model

(Run on the VPS via `ssh $VPS …`, or on your workstation directly for local-only mode.)

```bash
ssh $VPS 'bash -s' <<'EOF'
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b
sudo apt-get install -y syncthing
loginctl enable-linger $USER
systemctl --user enable --now syncthing
sudo ufw allow 22000/tcp comment "Syncthing BEP"
syncthing --device-id
EOF
```

Copy the device ID that prints at the bottom — you'll need it in step 2.

### 2. Workstation side — pair with host, share the notes folder

**Skip this section** in local-only mode — your workstation IS the host. Jump to step 4.

```bash
VPS_DEVICE_ID=...paste-from-step-1...
VPS_ID_PREFIX=${VPS_DEVICE_ID:0:7}

# Add the VPS as a device
syncthing cli config devices add --device-id "$VPS_DEVICE_ID"
syncthing cli config devices "$VPS_ID_PREFIX" addresses add \
  "tcp://${VPS#*@}:22000"
syncthing cli config devices "$VPS_ID_PREFIX" name set "vps"

# Share the notes folder with it (the folder must already exist and be
# shared on the workstation — if you don't have one yet, create it first
# with --id obsidian-vault --path ~/notes --label "Obsidian Vault")
syncthing cli config folders obsidian-vault devices add \
  --device-id "$VPS_DEVICE_ID"
```

### 3. VPS side — accept the device and create the folder

```bash
WORKSTATION_DEVICE_ID=$(syncthing --device-id)   # run this on the workstation first

ssh $VPS "syncthing cli config devices add \
  --device-id ${WORKSTATION_DEVICE_ID}"

ssh $VPS "syncthing cli config folders add \
  --id obsidian-vault \
  --path \$HOME/sync/notes \
  --label 'Obsidian Vault' \
  --type sendreceive"

ssh $VPS "syncthing cli config folders obsidian-vault devices add \
  --device-id ${WORKSTATION_DEVICE_ID}"
```

Within ~10 seconds the two devices will find each other over IPv6 or the Syncthing global discovery service, and your notes will start flowing to the VPS.

### 4. Host side — deploy the responder

(In local mode, run these commands directly on your workstation without the `ssh $VPS …` wrapper.)

Create the secrets file first (mode 0600, never committed anywhere). The location `/etc/oso-sync/` is just a convention — you can put it anywhere as long as the systemd unit's `EnvironmentFile=` matches.

```bash
ssh $VPS 'bash -s' <<EOF
sudo mkdir -p /etc/oso-sync
sudo tee /etc/oso-sync/obsidian.env >/dev/null <<ENV
NOTES_ASK_DIR=\$HOME/sync/notes/ask
OLLAMA_URL=http://127.0.0.1:11434/api/generate
OLLAMA_MODEL=llama3.1:8b
GROQ_API_KEY=gsk_paste_your_groq_key_here
GROQ_MODEL=qwen/qwen3.8-27b
GROQ_REASONING_MODEL=openai/gpt-oss-120b
ENV
sudo chown \$USER:\$USER /etc/oso-sync/obsidian.env
sudo chmod 600 /etc/oso-sync/obsidian.env
EOF
```

> **Important**: the env file must be owned by the user that runs the systemd user service (not root), because user-scope systemd cannot read root-owned files. Mode 0600 + user-owned gives the same security posture as root-owned 0600 for a single-user VPS.

From the workstation, run the deploy script:

```bash
./deploy/install-vps.sh $VPS
```

This rsyncs `responder/` to `~/services/responder/` on the VPS, installs the systemd units under `~/.config/systemd/user/`, enables the 60s responder timer and the hourly indexer timer, and runs a smoke-test.

### 5. Phone — pair with host manually (optional)

Skip if you're not using a phone. See [`docs/phone-pairing.md`](docs/phone-pairing.md). It's five screens of tapping in the Syncthing-Fork app, one `syncthing cli` command on the host to accept the pending device, and — if you're on a Samsung or other aggressive-battery Android — an important list of OS-level settings you have to change or the daemon will get killed in the background. The doc has the full gotcha list. The steps are identical whether the host is a VPS or your workstation.

### 6. Try it

From any device:

```bash
echo "Explain Beta-Binomial thresholds in one paragraph" > ~/notes/ask/test-beta.md
```

Within 60 seconds, open the same file on any other paired device and you'll see:

```markdown
Explain Beta-Binomial thresholds in one paragraph

---

**🤖 groq:qwen/qwen3.8-27b** · 2026-04-10 09:32

The Beta-Binomial model...

<!-- responder-processed -->
```

The `🤖` tag records which backend and model actually answered. `groq:<model>` means Groq answered: the primary model, `groq:openai/gpt-oss-120b` for a reasoning-heavy question, or a backup model after a failure. `ollama:<model> (small local backup model, check facts)` means every Groq model failed and the fallback took over: check `journalctl --user -u oso-responder.service -n 50` on the host (via SSH or directly) for the reason.

## Configuration

The responder reads all config from environment variables loaded via the systemd `EnvironmentFile=` directive. All knobs:

| Var | Default | Meaning |
|---|---|---|
| `NOTES_ASK_DIR` | `~/sync/notes/ask` | where to poll for questions |
| `GROQ_API_KEY` | *(unset)* | primary LLM; unset → local-only (Ollama-only) mode |
| `GROQ_MODEL` | `qwen/qwen3.8-27b` | primary model |
| `GROQ_REASONING_MODEL` | `openai/gpt-oss-120b` | model for questions that look reasoning-heavy |
| `GROQ_FALLBACK_MODELS` | `openai/gpt-oss-20b` | colon-separated Groq models tried, in order, when the ones above fail |
| `GROQ_MAX_RETRY_WAIT` | `10` | longest `Retry-After` (seconds) a 429 may ask for and still be retried once on the same model |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/generate` | fallback Ollama endpoint |
| `OLLAMA_MODEL` | `llama3.1:8b` | fallback model |
| `OLLAMA_FALLBACK` | `1` | `0` = never answer with Ollama; the note waits for the next tick |
| `SYSTEM_PROMPT` | *(built-in prompt)* | replaces the base system prompt |
| `MAX_TOKENS` | `2000` | max tokens per answer |
| `CONTEXT_FILE` | `/etc/oso-sync/obsidian-context.md` | static workspace brief prepended to every prompt |
| `CONTEXT_DIRS` | *(falls back to `CONTEXT_DIR`)* | colon-separated dirs of markdown notes to retrieve from (with the keyword fallback, earlier = scanned first) |
| `CONTEXT_DIR` | `~/sync/notes` | legacy single-dir RAG path |
| `RAG_TOP_K` | `5` | note excerpts retrieved per question |
| `RAG_SNIPPET_CHARS` | `600` | characters per retrieved excerpt |
| `RAG_MAX_FILES` | `400` | max markdown files scanned per question by the keyword fallback |

Leave `GROQ_API_KEY` unset to run 100% local-only. If both backends fail, the responder logs it and leaves the file unprocessed, and it retries on the next poll cycle. `NOTES_ASK_DIR` must be an absolute path (or unset): the responder does not expand `$HOME` in env values.

### Grounding the model in your own context

The default system prompt is generic. To make answers actually useful, populate two things:

1. **`CONTEXT_FILE`** — a short workspace brief (identity, projects, conventions) that is prepended to every prompt. `install-vps.sh` uploads `~/backup/obsidian-context.md` to `/etc/oso-sync/obsidian-context.md` automatically. See `deploy/obsidian-context.md.example`.
2. **`CONTEXT_DIRS`** — any number of markdown dirs the responder searches per question for the `RAG_TOP_K` best-matching excerpts. There are two ways it does that:
   - **Embedding index (preferred).** `responder/index.py` splits each note into chunks on markdown headings (about `RAG_CHUNK_CHARS` each), embeds them with an Ollama model (`nomic-embed-text` by default) and writes the vectors to `RAG_INDEX_DIR`. At question time the responder ranks chunks twice, by embedding similarity and by BM25 over the chunk text plus the file name, merges the two rankings by reciprocal rank fusion and returns at most one chunk per note. A refresh only re-embeds notes whose size or modification time changed, and `oso-indexer.timer` runs it every hour (first run 5 minutes after boot). Changing `EMBED_MODEL` rebuilds the index from scratch. Answered question files (the ones ending in the responder marker) and paths matching `RAG_EXCLUDE` are left out. You need `numpy` for the `python3` the unit runs and `ollama pull nomic-embed-text` on the host; embedding goes to your own Ollama, so indexing sends nothing off the machine.
   - **Keyword fallback.** With no index, no `numpy` or an unreachable embedding backend, the responder scores markdown files by keyword overlap instead. Put the richest / smallest dirs first: later dirs are only searched if the `RAG_MAX_FILES` budget is still open.

The index has its own knobs, read by `responder/index.py` from the same env file:

| Var | Default | Meaning |
|---|---|---|
| `EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `OLLAMA_EMBED_URL` | `http://127.0.0.1:11434/api/embed` | Ollama embedding endpoint |
| `RAG_INDEX_DIR` | `~/services/responder-context/index` | where the index is written and read |
| `RAG_EXCLUDE` | `archive:archived:logs:ask:*sync-conflict*` | colon-separated globs; a note is skipped if its path or any folder name in it matches |
| `RAG_CHUNK_CHARS` | `1500` | target size of one indexed chunk |
| `RAG_MAX_CHUNKS_PER_FILE` | `10` | chunks kept per note |
| `RAG_EMBED_BATCH` | `32` | chunks sent to Ollama per embedding call |
| `EMBED_DOC_PREFIX` / `EMBED_QUERY_PREFIX` | `search_document: ` / `search_query: ` | text prefixes the embedding model expects (the defaults are `nomic-embed-text`'s) |

To check retrieval, write a JSON list of control questions, `[{"q": "...", "expect": ["part-of-the-right-note's-path"]}]`, and run `python3 responder/eval_retrieval.py questions.json` on the host. It prints the rank of the first expected note for each question, then hit@`RAG_TOP_K` and mean reciprocal rank; `--keyword` scores the keyword fallback instead. `responder/embed_trial.sh MODEL TRIAL_INDEX_DIR QUESTIONS.json REPORT.md` builds a second index with another embedding model and scores it next to the live one on the same questions, without touching the live index or the responder's settings (set `TRIAL_DOC_PREFIX` and `TRIAL_QUERY_PREFIX` if the model expects text prefixes).

If you keep a notes/memory repo outside the synced folder, push it to the VPS on a timer. The `deploy/sync-memory-to-vps.sh` script + `deploy/systemd-workstation/oso-memory-sync.{service,timer}` units in this repo do exactly that (every 48h). Install on the workstation:

```bash
ln -sf "$PWD/deploy/systemd-workstation/oso-memory-sync.service" ~/.config/systemd/user/
ln -sf "$PWD/deploy/systemd-workstation/oso-memory-sync.timer"   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now oso-memory-sync.timer
```

Before the `enable` step above, add a drop-in (`systemctl --user edit oso-memory-sync.service`) that points `ExecStart=` at your clone and sets your target and source. The shipped units assume the clone is at `~/oso-sync`. `VPS` has no default and the script exits if it is unset; `MEMORY_SRC` defaults to `~/memory` and `MEMORY_DEST` to `services/responder-context/memory`:

```ini
[Service]
Environment=VPS=user@host
Environment=MEMORY_SRC=/path/to/markdown/notes
ExecStart=
ExecStart=/path/to/oso-sync/deploy/sync-memory-to-vps.sh
```
 `oso-system-facts.timer` works the same way: it runs `generate-system-facts.sh` weekly and writes a markdown snapshot of the workstation's CPU, RAM, GPU and OS to `$OUT`, so keep `$OUT` inside the folder the memory sync pushes (by default it is `$MEMORY_SRC/global/system-facts.md`; `PROJECTS_DIR`, default `~/projects`, is the folder it counts projects in).

`sync-memory-to-vps.sh` can push more than the notes folder, each part switched on by its own variable:

- `PROJECTS_ROOT` (a folder with one dir per project) plus `DOCS_SRC` (an output folder): first runs `deploy/collect-project-docs.sh`, which copies each project's `README.md` and `CLAUDE.md` into `DOCS_SRC` as `<project>--README.md` and so on, then rsyncs `DOCS_SRC` to `DOCS_DEST` (default `services/responder-context/docs`). The collector deletes the `*.md` files already in `DOCS_SRC` first, so give it a folder of its own, and it skips and reports any file that matches a secret pattern instead of copying it. `SKIP_PROJECTS` (space-separated names) leaves projects out and `EXTRA_FILES` adds single files.
- `BRIEF_SRC`: pushes the static workspace brief to `BRIEF_DEST` (default `services/responder-context/obsidian-context.md`).
- `REINDEX=1`: starts `oso-indexer.service` on the host afterwards, so new notes are searchable without waiting for the hourly timer.

Then point `CONTEXT_DIRS` at the remote paths on the VPS (e.g. `CONTEXT_DIRS=/home/you/services/responder-context/memory:/home/you/services/responder-context/docs:/home/you/sync/notes`).

## Observability

```bash
./deploy/status.sh $VPS                              # whole-stack health check
ssh $VPS 'journalctl --user -fu oso-responder.service'    # live tail
ssh $VPS 'systemctl --user list-timers oso-responder.timer oso-indexer.timer'
ssh $VPS 'journalctl --user -u oso-indexer.service -n 20'    # last embedding index refresh
ssh $VPS 'syncthing cli show connections | python3 -m json.tool'
```

## Cost

| Component | Local-only | Dedicated VPS | Shared VPS / home server |
|---|---|---|---|
| Host hardware | **€0** (your own machine) | ~€5–11/month | **€0** (already owned) |
| Bandwidth | €0 | €0 (included at this scale) | €0 |
| Syncthing | €0 | €0 | €0 |
| Ollama + `llama3.1:8b` | €0 (CPU) | €0 (CPU) | €0 (CPU) |
| Groq primary | ~€0 (free tier, 30 req/min) | ~€0 | ~€0 |
| Obsidian | €0 | €0 | €0 |
| **Total** | **€0/month** | **~€5–11/month** | **€0/month** |

Local-only mode is the cheapest; a Pi 4/5 or reused old laptop as the always-on host is nearly free. Dedicated VPS only makes sense if you want rock-solid phone access while travelling and don't want to leave your own hardware running 24/7.

## Security posture

- **End-to-end encrypted file sync** — Syncthing BEP uses mutual-authenticated TLS per device ID. A device can only join the mesh if you explicitly add its device ID on both sides.
- **Minimal public attack surface** — only Syncthing BEP port 22000/tcp is exposed to the internet. The Syncthing web GUI (8384) is bound to `127.0.0.1` only.
- **Secrets never in git** — the `.gitignore` excludes `.env*` and `secrets/`. Provision secrets via `/etc/oso-sync/obsidian.env` on the host (mode 0600, owned by your user).
- **Responder runs unprivileged** as a systemd user service with `NoNewPrivileges=true` and `PrivateTmp=true`. The more aggressive hardening directives (`MemoryDenyWriteExecute`, `ProtectKernelTunables`, etc.) fail in user scope because systemd can't manipulate capabilities without root, so they're deliberately omitted — user scope already gives you per-uid isolation.
- **Ollama bound to 127.0.0.1** on the host — not exposed to the internet.
- **What leaves your machine**: with `GROQ_API_KEY` set, each question plus the workspace brief and retrieved note excerpts attached to it is sent to `api.groq.com`. Leave the key unset and nothing is sent to a hosted LLM.
- **Groq key scoped per workload** — if you run multiple services off the same host (e.g. an Obsidian responder + a public chat endpoint), give each its own env file and key so a compromise blast-radius is one service, not all of them.

## Status

- [x] Host-side Syncthing + Ollama + responder deploy validated end-to-end
- [x] Workstation ↔ host pair over IPv6, bidirectional folder sync
- [x] Phone ↔ host pair over cellular (via Syncthing public relay pool, see `docs/phone-pairing.md`)
- [x] Groq primary + Ollama fallback (Cloudflare UA workaround baked in to `responder/query_groq()`)
- [x] Responder sentinel deduplication (new + legacy markers) so historic files from earlier versions aren't double-answered
- [x] Context grounding v2 (2026-04-14) — responder names projects (Belliq, TrakTek) correctly from workspace memory; memory rsync to VPS every 48h via `oso-memory-sync.timer`
- [x] Reasoning-model routing: questions that look reasoning-heavy go to `openai/gpt-oss-120b`, with the primary and backup Groq models behind it if it fails
- [x] Weekly workstation system-facts snapshot (`oso-system-facts.timer`) so the responder can answer device and spec questions
- [x] Groq defaults moved to `qwen/qwen3.8-27b` + `openai/gpt-oss-120b` (2026-09-28) after Groq retired `llama-3.3-70b-versatile`
- [x] Groq backup models (`GROQ_FALLBACK_MODELS`) tried before Ollama, short 429s retried once (2026-09-29)
- [x] Embedding index over `CONTEXT_DIRS` with BM25 fusion, refreshed hourly by `oso-indexer.timer`, plus a retrieval eval and an embedding-model trial script (2026-09-29)
- [ ] Optional: per-folder system prompts (e.g. `notes/ask-code/` uses a coder prompt, `notes/ask-writing/` a writing-coach prompt), extension point, not built yet

## Known limitations

- **60 second polling cadence** — the systemd timer fires every 60s. Worst-case latency is 60s + Groq response time. Good enough for notes, not for chat-like use. A file-watch (inotify) variant would be trivial to add but complicates restart semantics.
- **Cellular uses Syncthing relay pool, not direct connections** — carrier-grade NAT on most mobile networks blocks direct peer connections, so Syncthing falls back to its public relay network automatically. Traffic is still E2E TLS encrypted (relay sees only opaque bytes), but there's a small latency + data overhead. Documented in `docs/phone-pairing.md`. This is normal, not a misconfiguration.
- **Samsung One UI aggressively kills background services** even with "unrestricted battery" set. The full list of Android/Samsung settings you need to flip is in `docs/phone-pairing.md`. Non-Samsung Android and iOS users can skip that section.

## License

MIT.
