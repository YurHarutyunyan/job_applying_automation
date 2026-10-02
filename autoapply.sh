#!/usr/bin/env bash
# One entry point for the whole flow, with a switch between two engines:
#   claude  screening via `claude -p`; apply via Claude Code + Claude in Chrome (auto-submits)
#   cursor  screening via `cursor-agent -p`; apply via Playwright + Simplify (you submit)
#
#   ./autoapply.sh backend              show the active backend
#   ./autoapply.sh backend cursor       switch (saved in .backend)
#   ./autoapply.sh fetch [--limit N] [--dry-run]   CodingJobBoard, Greenhouse/Lever/Ashby, Himalayas
#   ./autoapply.sh pending              list the next jobs to apply to
#   ./autoapply.sh apply [job-id]       apply to one job (default: next pending)
#   ./autoapply.sh setup-browser        cursor backend only: install + sign in to Simplify
#
# AUTOAPPLY_BACKEND=claude|cursor overrides the saved setting for a single command.
set -euo pipefail
cd "$(dirname "$0")"

backend() { python3 scripts/backend.py; }

cmd="${1:-help}"
shift || true

case "$cmd" in
  backend)
    python3 scripts/backend.py "$@"
    ;;
  fetch)
    python3 scripts/fetch_jobs.py "$@"
    python3 scripts/fetch_ats_jobs.py "$@"
    python3 scripts/fetch_himalayas_jobs.py "$@"
    ;;
  pending)
    python3 scripts/pending_jobs.py "$@"
    ;;
  setup-browser)
    python3 scripts/apply_playwright.py --setup
    ;;
  apply)
    job_id="${1:-}"
    if [[ "$(backend)" == "cursor" ]]; then
      if [[ -n "$job_id" ]]; then
        python3 scripts/apply_playwright.py --job-id "$job_id"
      else
        python3 scripts/apply_playwright.py
      fi
    else
      echo "claude backend: this auto-submits with no confirmation step."
      claude -p "/apply-codingjobboard ${job_id}" --chrome \
        --allowedTools "mcp__claude-in-chrome__*" "Bash(python3 scripts/*)" "Read" "Grep" "Glob" "Task" "Agent"
    fi
    ;;
  *)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
