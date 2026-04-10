#!/usr/bin/env bash
# status.sh — quick health check of the whole OsO Sync stack on your VPS.
#
# Usage:
#   ./deploy/status.sh <user@host>
#
# Example:
#   ./deploy/status.sh youruser@vps.example.com

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

echo "==> OsO Sync status @ ${VPS}"
echo

ssh "${VPS}" '
echo "--- syncthing ---"
systemctl --user is-active syncthing 2>/dev/null || echo "inactive"
syncthing cli show connections 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for devid, info in d.get(\"connections\", {}).items():
        short = devid[:8]
        conn = info.get(\"connected\", False)
        addr = info.get(\"address\", \"-\")
        print(f\"  {short}  connected={conn}  {addr}\")
except Exception as e:
    print(f\"  error: {e}\")
" 2>/dev/null

echo
echo "--- ollama ---"
systemctl is-active ollama 2>/dev/null || echo "inactive"
ollama list 2>/dev/null | tail -n +2

echo
echo "--- responder timer ---"
systemctl --user is-active oso-responder.timer 2>/dev/null || echo "inactive"
systemctl --user list-timers oso-responder.timer --no-pager 2>/dev/null | tail -n +2

echo
echo "--- synced notes ---"
ls "$HOME"/sync/notes/ask/*.md 2>/dev/null | wc -l | xargs -I{} echo "  ask/ files: {}"
du -sh "$HOME"/sync/notes 2>/dev/null | awk "{print \"  total size: \" \$1}"

echo
echo "--- last 5 responder log lines ---"
journalctl --user -u oso-responder.service -n 5 --no-pager 2>/dev/null | tail -5
'
