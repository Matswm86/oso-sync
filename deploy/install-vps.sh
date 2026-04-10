#!/usr/bin/env bash
# install-vps.sh — one-shot deploy of the OsO Sync stack onto your VPS.
#
# Idempotent. Re-runs just refresh the responder + systemd units.
#
# Prereqs on the VPS (handled manually first time — see README §1):
#   - Any modern Linux (tested on Ubuntu 24.04)
#   - SSH key auth from this machine
#   - Ollama installed + model pulled: `ollama pull llama3.1:8b`
#   - Syncthing installed + `systemctl --user enable --now syncthing`
#   - /etc/oso-sync/obsidian.env created with GROQ_API_KEY
#     (mode 0600, owned by the user that will run the systemd user service)
#
# Usage:
#   ./deploy/install-vps.sh <user@host>
#
# Example:
#   ./deploy/install-vps.sh youruser@vps.example.com

set -euo pipefail

VPS="${1:-}"
if [[ -z "$VPS" ]]; then
  echo "usage: $0 <user@host>" >&2
  echo "example: $0 youruser@vps.example.com" >&2
  exit 2
fi

if [[ "$VPS" != *@* ]]; then
  echo "error: VPS target must be in user@host form (got: $VPS)" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> deploying OsO Sync to ${VPS}"
echo "    repo root: ${REPO_ROOT}"

# 1. Ensure service + sync directories exist on the remote, then push
#    the responder code. Paths use the remote user's $HOME via ~/, which
#    the remote shell expands — works for any user.
echo "==> syncing responder script"
ssh "${VPS}" 'mkdir -p ~/services/responder ~/sync/notes/ask ~/sync/notes/answers'
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.env*' \
  "${REPO_ROOT}/responder/" "${VPS}:services/responder/"

# 2. Push systemd user units. `~/.config/systemd/user/` is the canonical
#    path for user-scope systemd units.
echo "==> installing systemd units (user scope)"
ssh "${VPS}" 'mkdir -p ~/.config/systemd/user'
rsync -a "${REPO_ROOT}/systemd/" "${VPS}:.config/systemd/user/"

# 3. Reload systemd user manager + enable + start the timer
echo "==> reloading + enabling oso-responder.timer"
ssh "${VPS}" '
  systemctl --user daemon-reload
  systemctl --user enable --now oso-responder.timer
  systemctl --user status oso-responder.timer --no-pager | head -10
'

# 4. Smoke test: force one service run and tail the log
echo "==> smoke test (one-shot service run)"
ssh "${VPS}" 'systemctl --user start oso-responder.service; sleep 2; journalctl --user -u oso-responder.service -n 20 --no-pager'

echo "==> done. Drop a test file into ~/sync/notes/ask/ to see it processed within 60s."
