#!/usr/bin/env python3
"""Log the outcome of a codingjobboard-apply-agent run.

Standalone equivalent of the full pipeline's log_codingjobboard_application.py, minus the
Obsidian vault sync step (this package has no vault). Every write goes through common.record()
so applied_log.json is the single, consistent source of truth for status.

Usage:
    python3 scripts/log_apply.py --job-id codingjobboard-14176 --status applied \\
        --note "via Simplify on job-boards.greenhouse.io/gitlab/jobs/8621620002"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_jobs, select_job, record  # noqa: E402

STATUSES = ("applied", "needs_manual_review", "closed", "skipped_no_cv")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--status", required=True, choices=STATUSES)
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    jobs = load_jobs()
    job = select_job(jobs, args.job_id)
    record(job, args.status, args.note)


if __name__ == "__main__":
    main()
