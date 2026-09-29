#!/usr/bin/env bash
# embed_trial.sh — build a second index with another embedding model and score
# it against the live index on the same control questions. The live index and
# the responder's settings are not touched.
#
# Usage: embed_trial.sh MODEL TRIAL_INDEX_DIR QUESTIONS.json REPORT.md
# Optional env: TRIAL_DOC_PREFIX, TRIAL_QUERY_PREFIX (the model's text prefixes).

set -euo pipefail

MODEL="$1" TRIAL_DIR="$2" QUESTIONS="$3" REPORT="$4"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-/etc/oso-sync/obsidian.env}"

set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

start=$(date -u +%FT%TZ)
{
  echo "# Embedding model trial: ${MODEL} vs ${EMBED_MODEL:-nomic-embed-text}"
  echo
  echo "Started ${start}. Same questions, same hybrid search (embedding + BM25)."
  echo
  echo '## Live index'
  echo '```'
  python3 "${HERE}/eval_retrieval.py" "${QUESTIONS}"
  echo '```'
} > "${REPORT}.tmp"

build_log=$(
  EMBED_MODEL="${MODEL}" RAG_INDEX_DIR="${TRIAL_DIR}" \
  EMBED_DOC_PREFIX="${TRIAL_DOC_PREFIX:-}" EMBED_QUERY_PREFIX="${TRIAL_QUERY_PREFIX:-}" \
  nice -n 15 python3 "${HERE}/index.py" 2>&1 | tail -3
)

{
  echo
  echo "## Trial index (${MODEL})"
  echo '```'
  echo "${build_log}"
  echo
  EMBED_MODEL="${MODEL}" RAG_INDEX_DIR="${TRIAL_DIR}" \
  EMBED_DOC_PREFIX="${TRIAL_DOC_PREFIX:-}" EMBED_QUERY_PREFIX="${TRIAL_QUERY_PREFIX:-}" \
    python3 "${HERE}/eval_retrieval.py" "${QUESTIONS}"
  echo '```'
  echo
  echo "Finished $(date -u +%FT%TZ)."
} >> "${REPORT}.tmp"
mv "${REPORT}.tmp" "${REPORT}"
echo "report: ${REPORT}"
