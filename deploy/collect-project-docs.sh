#!/usr/bin/env bash
# collect-project-docs.sh — gather project READMEs and CLAUDE.md files into one
# folder so sync-memory-to-vps.sh can push them to the responder's context.
#
# Each file lands as <project>--README.md / <project>--CLAUDE.md. Files that
# match a secret pattern are skipped and reported, never copied.

set -euo pipefail

PROJECTS_ROOT="${PROJECTS_ROOT:?set PROJECTS_ROOT to the folder holding one dir per project}"
OUT="${DOCS_SRC:?set DOCS_SRC to the output folder}"
# Space-separated project names to leave out (e.g. private health notes).
SKIP_PROJECTS="${SKIP_PROJECTS:-}"
# Extra single files to include, space-separated absolute paths.
EXTRA_FILES="${EXTRA_FILES:-}"
SECRET_RE='gsk_[A-Za-z0-9]{10}|sk-ant-|sk-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{20}|github_pat_|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY'

mkdir -p "${OUT}"
find "${OUT}" -maxdepth 1 -name '*.md' -delete

copy() {  # copy <src> <dest-name>
  if grep -qE "${SECRET_RE}" "$1"; then
    echo "skip (secret pattern): $1" >&2
    return
  fi
  cp "$1" "${OUT}/$2"
}

for dir in "${PROJECTS_ROOT}"/*/; do
  name="$(basename "${dir}")"
  [[ " ${SKIP_PROJECTS} " == *" ${name} "* ]] && continue
  for f in README.md CLAUDE.md .claude/CLAUDE.md; do
    [[ -f "${dir}${f}" ]] && copy "${dir}${f}" "${name}--$(basename "${f}")"
  done
done
for f in ${EXTRA_FILES}; do
  [[ -f "${f}" ]] && copy "${f}" "extra--$(basename "$(dirname "${f}")")-$(basename "${f}")"
done

echo "collected $(find "${OUT}" -maxdepth 1 -name '*.md' | wc -l) files into ${OUT}"
