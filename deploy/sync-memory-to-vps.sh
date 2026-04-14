#!/usr/bin/env bash
# sync-memory-to-vps.sh — push ~/MWM-AI/memory/ to the VPS responder context.
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

VPS="${VPS:-mats@204.168.244.173}"
SRC="${MEMORY_SRC:-${HOME}/MWM-AI/memory}"
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
