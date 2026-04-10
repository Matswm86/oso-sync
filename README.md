# OsO Sync

**O**bsidian · **S**yncthing · **O**llama — a three-piece, zero-cloud, always-on personal AI notes stack.

Write a question into `notes/ask/foo.md` on your phone from anywhere. A minute later the answer is appended in-place, the file is fanned out to every device you own, and you've spent approximately zero cents. No vendor lock-in, no custom apps, no subscription — just three open-source tools wired together, with Groq as the fast primary LLM and self-hosted Ollama as the always-there fallback.

## What it does

```
┌───────────┐     notes/ask/foo.md   ┌───────────┐     notes/ask/foo.md   ┌─────────────┐
│   PHONE   │ ───────────────────▶  │    VPS    │ ◀─────────────────────│ WORKSTATION │
│ (Obsidian │     (Syncthing BEP)    │           │     (Syncthing BEP)    │  (Obsidian) │
│  + Sync)  │                        │           │                        │   + Sync)   │
└───────────┘                        │  ollama   │                        └─────────────┘
      ▲                              │  llama3.1 │                                ▲
      │                              │     +     │                                │
      │                              │ responder │                                │
      │                              │  (60s tick)                                │
      │                              │     │     │                                │
      │                              │     ▼     │                                │
      │   answer appended in-place   │  rewrites │   answer appended in-place     │
      └──────────────────────────────│  the file │────────────────────────────────┘
          (Syncthing BEP)            └───────────┘        (Syncthing BEP)
```

1. You write a question anywhere (phone, workstation, tablet)
2. Syncthing propagates it to the VPS over BEP
3. A systemd timer on the VPS fires every 60s and runs the responder
4. Responder queries Groq (primary) with Ollama as the fallback, and appends the answer
5. Syncthing propagates the updated file back to every device

Total latency: ~2–60 seconds depending on where you are in the polling cycle.

## The three tools

The name **OsO** comes from **O**bsidian · **s**yncthing · **O**llama — two capital O's with a lowercase s for Syncthing in the middle.

- **Obsidian** — the note-taking app. Your vault lives in plain markdown files in a folder. That's the only contract. Any markdown-vault app works.
- **Syncthing** — open-source, end-to-end-encrypted file sync over a P2P mesh. No central server required; the VPS is just "a device that's always on". Zero vendor dependency.
- **Ollama** — local LLM runtime. On the VPS it runs `llama3.1:8b` on CPU as the fallback when Groq is rate-limited or down. On your workstation you can point at bigger local models; on your phone there is no Ollama — the VPS handles mobile.

The responder uses **Groq `llama-3.3-70b-versatile` as the primary** LLM — hosted, fast (~500 tok/s), and free for personal-scale use. Ollama only engages if Groq fails or the key is unset. This was a deliberate flip from an early "Ollama-primary" design once it was clear that on an always-online VPS with an existing Groq free tier, Groq-primary dominates on every axis (speed, quality, CPU headroom) while Ollama remains invaluable as disaster-recovery insurance.

The clever bit: none of these three tools know about each other. They're glued together by a ~200-line Python script (`responder/responder.py`) that polls the synced folder and writes answers back. The glue has no dependencies outside the Python stdlib.

## Project layout

```
oso-sync/
├── README.md                  this file
├── responder/
│   ├── responder.py           the glue: polls notes/ask/, queries Groq/Ollama, appends answer
│   └── .env.example           template for /etc/oso-sync/obsidian.env on the VPS
├── systemd/
│   ├── oso-responder.service  systemd user unit for the responder (uses %h specifier)
│   └── oso-responder.timer    60s cadence timer
├── syncthing/
│   └── DEVICES.md             template ledger for the paired-devices mesh
├── docs/
│   ├── architecture.md        deeper walkthrough, data flow, extension points
│   └── phone-pairing.md       manual phone ↔ VPS pairing (Syncthing app steps)
└── deploy/
    ├── install-vps.sh         one-shot deploy from the repo to your VPS
    └── status.sh              whole-stack health check
```

## Quick start

### Prerequisites

- A Linux VPS you control (any small instance works — tested on a 16 GB Hetzner box, but anything with 2+ GB RAM and Ubuntu/Debian will do)
- Workstation with Syncthing already installed
- Phone with the [Syncthing-Fork Android app](https://f-droid.org/packages/com.github.catfriend1.syncthingandroid/)
- Obsidian installed on phone + workstation (or any other markdown-vault app that uses plain files)
- A free [Groq API key](https://console.groq.com/keys) (optional — without one, you'll run Ollama-only)

Throughout this README I use a shell variable for the VPS so you only have to fill it in once:

```bash
VPS=youruser@vps.example.com       # replace with your actual SSH target
```

### 1. VPS side — install Ollama, Syncthing, model

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

### 2. Workstation side — pair with VPS, share the notes folder

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

### 4. VPS side — deploy the responder

Create the secrets file first (mode 0600, never committed anywhere). The location `/etc/oso-sync/` is just a convention — you can put it anywhere as long as the systemd unit's `EnvironmentFile=` matches.

```bash
ssh $VPS 'bash -s' <<EOF
sudo mkdir -p /etc/oso-sync
sudo tee /etc/oso-sync/obsidian.env >/dev/null <<'ENV'
NOTES_ASK_DIR=\$HOME/sync/notes/ask
OLLAMA_URL=http://127.0.0.1:11434/api/generate
OLLAMA_MODEL=llama3.1:8b
GROQ_API_KEY=gsk_paste_your_groq_key_here
GROQ_MODEL=llama-3.3-70b-versatile
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

This rsyncs `responder/` to `~/services/responder/` on the VPS, installs the systemd units under `~/.config/systemd/user/`, enables the 60s timer, and runs a smoke-test.

### 5. Phone — pair with VPS manually

See [`docs/phone-pairing.md`](docs/phone-pairing.md). It's five screens of tapping in the Syncthing-Fork app, one `syncthing cli` command on the VPS to accept the pending device, and — if you're on a Samsung or other aggressive-battery Android — an important list of OS-level settings you have to change or the daemon will get killed in the background. The doc has the full gotcha list.

### 6. Try it

From any device:

```bash
echo "Explain Beta-Binomial thresholds in one paragraph" > ~/notes/ask/test-beta.md
```

Within 60 seconds, open the same file on any other paired device and you'll see:

```markdown
Explain Beta-Binomial thresholds in one paragraph

---

**🤖 groq** · 2026-04-10 09:32

The Beta-Binomial model...

<!-- responder-processed -->
```

The `🤖` tag records which backend actually answered. `groq` means primary path. `ollama` means Groq failed and the fallback took over — check `journalctl --user -u oso-responder.service -n 50` on the VPS for the reason.

## Configuration

The responder reads all config from environment variables loaded via the systemd `EnvironmentFile=` directive. All knobs:

| Var | Default | Meaning |
|---|---|---|
| `NOTES_ASK_DIR` | `~/sync/notes/ask` | where to poll for questions |
| `GROQ_API_KEY` | *(unset)* | primary LLM; unset → local-only (Ollama-only) mode |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | primary model |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/generate` | fallback Ollama endpoint |
| `OLLAMA_MODEL` | `llama3.1:8b` | fallback model |

Leave `GROQ_API_KEY` unset to run 100% local-only. The responder will silently skip files if both backends fail — they'll retry on the next poll cycle.

## Observability

```bash
./deploy/status.sh $VPS                              # whole-stack health check
ssh $VPS 'journalctl --user -fu oso-responder.service'    # live tail
ssh $VPS 'systemctl --user list-timers oso-responder.timer'
ssh $VPS 'syncthing cli show connections | python3 -m json.tool'
```

## Cost

Typical monthly cost for a dedicated VPS running only OsO Sync:

| Component | Monthly | Notes |
|---|---|---|
| VPS (small instance, 2–16 GB RAM) | ~€5–11 | any provider; Hetzner, Scaleway, DO all work |
| Bandwidth | €0 | included on most providers at this scale |
| Syncthing | €0 | open source, runs peer-to-peer |
| Ollama + `llama3.1:8b` | €0 | self-hosted, CPU inference |
| Groq primary | ~€0 | free tier (30 req/min) easily covers personal use |
| Obsidian | €0 | free for personal use |

If your VPS is already running other things (reverse proxy, blog, small apps) the marginal cost of OsO Sync is effectively zero — it shares a box that exists.

## Security posture

- **End-to-end encrypted file sync** — Syncthing BEP uses mutual-authenticated TLS per device ID. A device can only join the mesh if you explicitly add its device ID on both sides.
- **Minimal public attack surface** — only Syncthing BEP port 22000/tcp is exposed to the internet. The Syncthing web GUI (8384) is bound to `127.0.0.1` only.
- **Secrets never in git** — the `.gitignore` excludes `.env*` and `secrets/`. Provision secrets via `/etc/oso-sync/obsidian.env` on the VPS (mode 0600, owned by your user).
- **Responder runs unprivileged** as a systemd user service with `NoNewPrivileges=true` and `PrivateTmp=true`. The more aggressive hardening directives (`MemoryDenyWriteExecute`, `ProtectKernelTunables`, etc.) fail in user scope because systemd can't manipulate capabilities without root, so they're deliberately omitted — user scope already gives you per-uid isolation.
- **Ollama bound to 127.0.0.1** on the VPS — not exposed to the internet.
- **Groq key scoped per workload** — if you run multiple services off the same VPS (e.g. an Obsidian responder + a public chat endpoint), give each its own env file and key so a compromise blast-radius is one service, not all of them.

## Status

- [x] VPS-side Syncthing + Ollama + responder deploy validated end-to-end
- [x] Workstation ↔ VPS pair over IPv6, bidirectional folder sync
- [x] Phone ↔ VPS pair over cellular (via Syncthing public relay pool, see `docs/phone-pairing.md`)
- [x] Groq primary + Ollama fallback (Cloudflare UA workaround baked in to `responder/query_groq()`)
- [x] Responder sentinel deduplication (new + legacy markers) so historic files from earlier versions aren't double-answered
- [ ] Optional: per-folder system prompts (e.g. `notes/ask-code/` uses a coder prompt, `notes/ask-writing/` a writing-coach prompt) — extension point, not built yet

## Known limitations

- **60 second polling cadence** — the systemd timer fires every 60s. Worst-case latency is 60s + Groq response time. Good enough for notes, not for chat-like use. A file-watch (inotify) variant would be trivial to add but complicates restart semantics.
- **Cellular uses Syncthing relay pool, not direct connections** — carrier-grade NAT on most mobile networks blocks direct peer connections, so Syncthing falls back to its public relay network automatically. Traffic is still E2E TLS encrypted (relay sees only opaque bytes), but there's a small latency + data overhead. Documented in `docs/phone-pairing.md`. This is normal, not a misconfiguration.
- **Samsung One UI aggressively kills background services** even with "unrestricted battery" set. The full list of Android/Samsung settings you need to flip is in `docs/phone-pairing.md`. Non-Samsung Android and iOS users can skip that section.

## License

MIT.
