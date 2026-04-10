# OsO Sync — Architecture

## The problem

You want to capture thoughts and questions from anywhere (phone, workstation, any device), have them answered by an AI, and keep the conversation in your Obsidian vault — **without depending on home wifi, without depending on the workstation being on, and without sending your notes to a third-party cloud service.**

## The stack

Three off-the-shelf pieces, wired together:

```
┌────────────────┐   Syncthing    ┌────────────────┐
│   PHONE        │<-------------->│  WORKSTATION   │
│   (Obsidian)   │     (LAN       │   (Obsidian)   │
└─────┬──────────┘      BEP)      └────────┬───────┘
      │                                     │
      │          Syncthing BEP              │
      │       (internet, IPv4/IPv6)         │
      │                                     │
      └────────────┬────────────────────────┘
                   │
                   v
           ┌───────────────┐
           │      VPS      │  ← always-on, public internet
           │  (any Linux)  │
           │               │
           │  - syncthing  │
           │  - ollama     │
           │  - responder  │ (polls notes/ask/ every 60s)
           │  - [Groq key] │ at /etc/oso-sync/obsidian.env
           └───────────────┘
```

## Data flow

1. You write a question on any device: `notes/ask/my-question.md`
2. Syncthing propagates the file to the VPS (~seconds over WiFi/LTE)
3. VPS systemd user timer fires `oso-responder.service` every 60s
4. Responder finds the new `.md` file in `$NOTES_ASK_DIR` (default `~/sync/notes/ask/`)
5. Responder POSTs to Groq (`llama-3.3-70b-versatile`) — **primary**, using the key from the env file. Returns in ~1–2 seconds.
6. On Groq failure (rate limit, outage, missing key), responder falls back to local Ollama (`llama3.1:8b`) at `127.0.0.1:11434`. ~5–10s on CPU.
7. Response is appended to the original file with a sentinel marker (the `🤖` label records which backend actually answered):
   ```markdown
   ---
   **🤖 groq** · 2026-04-10 09:28
   
   <response>
   
   <!-- responder-processed -->
   ```
8. Sentinel prevents re-processing on subsequent timer fires
9. Syncthing propagates the updated file back to all paired devices
10. Any device reading the file sees the answer

## Why polling (60s) vs. file-watching?

Polling via systemd timer is:

- **Simple** — no inotify, no long-running python process, no socket lifecycle to manage
- **Self-healing** — if a run crashes, the next one picks up unprocessed files
- **Low-overhead** — the responder exits in ~200 ms when `ask/` is empty, so there's no idle resource use
- **Restart-safe** — systemd timers automatically resume across reboots via `Persistent=false` (no backlog execution) or `Persistent=true` (replay missed runs)

File-watching would be slightly more responsive but adds complexity: inotify edge cases on networked/Syncthing-managed filesystems, handling mid-write restarts, keeping a Python process alive as a foreground service. 60-second latency is invisible for a conversational note workflow where you write a question and come back to it a minute later anyway.

If you need lower latency, swap the timer for a Syncthing `post-sync` hook (see `extension points` below).

## Why Groq primary + Ollama fallback?

- **Groq** is the primary because on an always-online VPS it wins on every axis for a conversational-notes workload:
  - **Speed**: `llama-3.3-70b-versatile` at ~500 tok/s → responses in 1–2 s
  - **Quality**: 70B params vs. the 8B local model, noticeably better at nuance
  - **CPU headroom**: pushing inference to Groq keeps the VPS responsive for anything else it's serving
  - **Cost**: Groq's free tier (30 req/min) easily covers personal-notes scale
- **Ollama** is the fallback, not because it's secondary in value but because its *use case* is secondary: it only engages when Groq is rate-limited, returns an HTTP error, or the key is missing entirely. `llama3.1:8b` on CPU returns in 5–10 s — acceptable in emergencies and as a disaster-recovery path if Groq ever has an outage.
- The responder degrades gracefully: Groq → Ollama → skip (file stays unprocessed, retries on the next 60 s poll cycle).

Originally this was designed as Ollama-primary. The flip to Groq-primary was a deliberate decision after end-to-end validation made it clear that on an always-online VPS the slower local model had no practical advantage as the default path. Leaving `GROQ_API_KEY` empty in the env file reverts to Ollama-only mode with a single config change.

### Cloudflare UA trap (important implementation detail)

`api.groq.com` is fronted by Cloudflare, which returns `HTTP 403 error:1010` ("banned browser signature") for Python's default `Python-urllib/3.x` User-Agent. The responder sets `User-Agent: oso-sync/0.1` explicitly on every Groq request — without this header, the entire Groq path fails and everything falls back to Ollama even when the key is valid.

If you write additional Python-stdlib code that calls `api.groq.com`, set a `User-Agent` header or you'll hit the same trap. `curl`, `requests`, `httpx`, and `aiohttp` all set their own UA and are not affected.

## Hardware requirements (VPS)

| Component | Minimum | Notes |
|---|---|---|
| RAM | 2 GB free | Groq-only mode; add 6 GB if you want Ollama fallback too |
| Disk | 10 GB | OS + ~5 GB for `llama3.1:8b` fallback model |
| CPU | 2+ cores | Groq does the heavy lifting; CPU is idle most of the time |
| GPU | not required | Groq handles all inference by default |
| Network | public IPv4 or IPv6 | for Syncthing BEP port 22000 |
| OS | any modern Linux | tested on Ubuntu 24.04, should work on Debian 12+ and derivatives |

Runs comfortably on any small VPS ($5–10/mo range). If your VPS is already hosting other things, OsO Sync's marginal overhead is negligible (a few tens of MB RAM for the idle syncthing + responder, ~5 GB disk for the Ollama model).

## Security model

- **Syncthing**: devices only pair with explicit device IDs. Traffic is TLS-encrypted + mutually authenticated. Port 22000/tcp is exposed to the internet but only accepts connections from known device IDs; port 8384 (GUI) is bound to `127.0.0.1` only. Access the GUI via SSH tunnel for troubleshooting: `ssh -L 8384:127.0.0.1:8384 $VPS`.
- **Ollama**: bound to `127.0.0.1`, not exposed to the internet.
- **Groq key**: stored at `/etc/oso-sync/obsidian.env` (mode 0600, owned by the user that runs the systemd user service). Loaded by systemd `EnvironmentFile=`. Never committed to git (the `.gitignore` excludes `.env*` and `secrets/`).
- **Responder**: runs unprivileged as a systemd user service with `NoNewPrivileges=true` and `PrivateTmp=true`. More aggressive hardening directives (`MemoryDenyWriteExecute`, `ProtectKernelTunables`, `RestrictSUIDSGID`, `ProtectKernelModules`, `ProtectControlGroups`) are deliberately omitted because they fail with `status=218/CAPABILITIES` in user scope — systemd cannot manipulate capabilities without root. User-scope execution already provides per-uid isolation.
- **Per-workload key scoping**: if you run multiple services off the same VPS (e.g. an Obsidian responder + a public AI endpoint), provision separate env files with separate Groq keys. Blast radius of a compromise = one service, not all.

## Observability

```bash
# assuming VPS=youruser@vps.example.com is set

# Live tail of the responder
ssh $VPS 'journalctl --user -fu oso-responder.service'

# Status of the whole stack
./deploy/status.sh $VPS

# Syncthing connection state
ssh $VPS 'syncthing cli show connections | python3 -m json.tool'
```

## Extension points

- **More watched dirs** — add new `notes/ask-XYZ/` paths, run parallel responder services with different system prompts (e.g. `ask-code/` uses a coder prompt, `ask-writing/` uses a writing-coach prompt)
- **Bigger models** — swap `OLLAMA_MODEL` or `GROQ_MODEL` in the env file
- **Answer routing** — responder currently appends in-place; could instead write `notes/answers/<question>.md` for a cleaner vault
- **Webhook trigger** — replace the systemd timer with a Syncthing `post-sync` hook for sub-second latency
- **Per-question model selection** — parse a frontmatter hint (`model: groq-70b` vs `model: ollama-8b`) to let the user pick per-question which backend to use
