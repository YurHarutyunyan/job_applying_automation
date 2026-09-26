#!/usr/bin/env python3
"""Compact view of staged CodingJobBoard leads for the apply agent.

found_jobs.json holds every posting's full description text (hundreds of KB), so the agent must
never read it directly — that alone costs more tokens than applying to several jobs. This prints
only what the agent needs.

A lead is "pending" when its id starts with codingjobboard-, it has no entry in applied_log.json,
and its fitVerdict status is "passed" (leads screened "excluded"/"ambiguous" are never applied to).

Usage:
    python3 scripts/pending_jobs.py                 # id<TAB>company<TAB>title<TAB>link, max 10
    python3 scripts/pending_jobs.py --limit 3
    python3 scripts/pending_jobs.py --ids-only      # just ids, one per line
    python3 scripts/pending_jobs.py --job-id codingjobboard-14176   # one job, with description
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_jobs, load_log, select_job  # noqa: E402

DEFAULT_LIMIT = 10
DESCRIPTION_CHARS = 2500


def pending(jobs: list, log: dict) -> list:
    return [
        j for j in jobs
        if j.get("id", "").startswith("codingjobboard-")
        and j["id"] not in log
        and (j.get("fitVerdict") or {}).get("status") == "passed"
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--ids-only", action="store_true")
    parser.add_argument("--job-id", help="Print one job's details (with truncated description)")
    args = parser.parse_args()

    jobs = load_jobs()

    if args.job_id:
        j = select_job(jobs, args.job_id)
        print(f"id: {j['id']}\ntitle: {j.get('title')}\ncompany: {j.get('companyName')}")
        print(f"location: {j.get('location')}\nlink: {j.get('link')}")
        desc = j.get("descriptionText") or ""
        suffix = " […truncated]" if len(desc) > DESCRIPTION_CHARS else ""
        print(f"description:\n{desc[:DESCRIPTION_CHARS]}{suffix}")
        return

    rows = pending(jobs, load_log())[: args.limit]
    for j in rows:
        if args.ids_only:
            print(j["id"])
        else:
            print(f"{j['id']}\t{j.get('companyName')}\t{j.get('title')}\t{j.get('link')}")


if __name__ == "__main__":
    main()
