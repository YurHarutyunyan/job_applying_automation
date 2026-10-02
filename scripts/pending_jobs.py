#!/usr/bin/env python3
"""Compact view of staged leads (CodingJobBoard, Greenhouse/Lever/Ashby, Himalayas) for the apply agent.

found_jobs.json holds every posting's full description text (hundreds of KB), so the agent must
never read it directly — that alone costs more tokens than applying to several jobs. This prints
only what the agent needs.

A lead is "pending" when its id starts with one of SOURCE_PREFIXES, it has no entry in applied_log.json,
its fitVerdict status is "passed" (leads screened "excluded"/"ambiguous" are never applied to), and
it isn't older than --max-age-days (default 30; leads with no readable posted date are kept), and
it is a Java role (see common.is_java_role; --any-stack disables this).

Usage:
    python3 scripts/pending_jobs.py                 # id<TAB>company<TAB>title<TAB>posted<TAB>url, max 10
    python3 scripts/pending_jobs.py --limit 3
    python3 scripts/pending_jobs.py --max-age-days 0   # ignore posting age
    python3 scripts/pending_jobs.py --ids-only      # just ids, one per line
    python3 scripts/pending_jobs.py --job-id codingjobboard-14176   # one job, with description
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEFAULT_MAX_AGE_DAYS, is_java_role, load_jobs, load_log, posting_age_days, select_job  # noqa: E402

DEFAULT_LIMIT = 10
SOURCE_PREFIXES = ("codingjobboard-", "greenhouse-", "lever-", "ashby-", "himalayas-")
DESCRIPTION_CHARS = 2500


def fresh_enough(job: dict, max_age_days: int) -> bool:
    age = posting_age_days(job)
    return not max_age_days or age is None or age <= max_age_days


def pending(jobs: list, log: dict, max_age_days: int = DEFAULT_MAX_AGE_DAYS, java_only: bool = True) -> list:
    return [
        j for j in jobs
        if j.get("id", "").startswith(SOURCE_PREFIXES)
        and j["id"] not in log
        and (j.get("fitVerdict") or {}).get("status") == "passed"
        and fresh_enough(j, max_age_days)
        and (not java_only or is_java_role(j))
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--ids-only", action="store_true")
    parser.add_argument("--job-id", help="Print one job's details (with truncated description)")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"Hide leads posted more than this many days ago (default {DEFAULT_MAX_AGE_DAYS}; 0 = no limit)")
    parser.add_argument("--any-stack", action="store_true",
                        help="Include non-Java roles (by default only Java/Spring roles are listed)")
    args = parser.parse_args()

    jobs = load_jobs()

    if args.job_id:
        j = select_job(jobs, args.job_id)
        print(f"id: {j['id']}\ntitle: {j.get('title')}\ncompany: {j.get('companyName')}")
        age = posting_age_days(j)
        print(f"location: {j.get('location')}\nposted: {'unknown' if age is None else f'{age} days ago'}")
        print(f"link: {j.get('link')}")
        if j.get("applyUrl"):
            print(f"applyUrl: {j['applyUrl']}")
        desc = j.get("descriptionText") or ""
        suffix = " […truncated]" if len(desc) > DESCRIPTION_CHARS else ""
        print(f"description:\n{desc[:DESCRIPTION_CHARS]}{suffix}")
        return

    rows = pending(jobs, load_log(), args.max_age_days, java_only=not args.any_stack)[: args.limit]
    for j in rows:
        if args.ids_only:
            print(j["id"])
        else:
            age = posting_age_days(j)
            posted = "?" if age is None else f"{age}d"
            print(f"{j['id']}\t{j.get('companyName')}\t{j.get('title')}\t{posted}\t{j.get('applyUrl') or j.get('link')}")


if __name__ == "__main__":
    main()
