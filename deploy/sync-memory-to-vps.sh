#!/usr/bin/env bash
# sync-memory-to-vps.sh — push a local folder of markdown notes to the VPS responder context.
#
# The VPS responder (services/responder/responder.py) reads CONTEXT_DIRS which
# points at ~/services/responder-context/memory/ on the VPS. This script keeps
# that copy in sync with the workstation source of truth.
#
# Scheduled by oso-memory-sync.timer (every 48h). Safe to run ad-hoc any time.
#
# Only markdown files are synced; secrets never live in memory/ (audited
# 2026-04-14 — 0 hits for gsk_/sk-/ghp_/AKIA/BEGIN/password/token patterns).

set -euo pipefail

VPS="${VPS:?set VPS=user@host, e.g. with Environment= in a drop-in for oso-memory-sync.service}"
SRC="${MEMORY_SRC:-${HOME}/memory}"
DEST="${MEMORY_DEST:-services/responder-context/memory}"

if [[ ! -d "${SRC}" ]]; then
  echo "error: memory source not found: ${SRC}" >&2
  exit 2
fi

# --delete keeps VPS copy strictly in sync (handles renames + deletes).
# Markdown-only include pattern prevents any accidental binary/secret leak.
rsync -a --delete \
  --include='*.md' --include='*/' --exclude='*' \
  "${SRC}/" "${VPS}:${DEST}/"

echo "memory synced to ${VPS}:${DEST}/"

# Optional: project docs gathered by collect-project-docs.sh.
if [[ -n "${PROJECTS_ROOT:-}" && -n "${DOCS_SRC:-}" ]]; then
  "$(dirname "$0")/collect-project-docs.sh"
  rsync -a --delete --include='*.md' --exclude='*' \
    "${DOCS_SRC}/" "${VPS}:${DOCS_DEST:-services/responder-context/docs}/"
  echo "docs synced to ${VPS}:${DOCS_DEST:-services/responder-context/docs}/"
fi

# Optional: the static workspace brief the responder loads as CONTEXT_FILE.
if [[ -n "${BRIEF_SRC:-}" ]]; then
  rsync -a "${BRIEF_SRC}" "${VPS}:${BRIEF_DEST:-services/responder-context/obsidian-context.md}"
  echo "brief synced"
fi

# Optional: refresh the responder's embedding index after new context lands.
if [[ "${REINDEX:-0}" == "1" ]]; then
  ssh "${VPS}" 'systemctl --user start --no-block oso-indexer.service' || echo "reindex trigger failed" >&2
fi
