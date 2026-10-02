#!/usr/bin/env python3
"""Pull remote Java leads open to Armenia from Himalayas (himalayas.app), resolved to a real ATS form.

Himalayas' public search API takes a country filter, so `country=AM` returns only postings whose
location restrictions include Armenia (or have none). But its own "Apply now" button needs a
Himalayas account (and the site sits behind Cloudflare), so a Himalayas link is useless to the apply
agent. Instead, each posting is matched by exact (normalised) title against the hiring company's
own public job board — Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters or Breezy,
trying a few slug spellings derived from the company name — and saved with that board's form as
its applyUrl. Postings that can't be matched are printed (with their Himalayas link) but not saved.

Per posting, in order:
  1. Fresh (--max-age-days, default 30), a developer title (QA/test/release/support/manager/
     analyst/artist/... titles are dropped) and a Java role (common.is_java_role); --any-stack skips
     the role checks.
  2. Resolved to a company ATS form (above).
  3. Citizenship/work-authorization screen, reusing fetch_jobs.py's hard-exclusion regex, LLM
     prompt and screened_jobs.json verdict cache — Himalayas' country filter is coarse, a posting
     can still say e.g. "EU residents only" in its text. Only "passed" postings are saved.

Ids are "himalayas-<company slug>-<job slug>". Dedup and updates work like fetch_jobs.py.

Usage:
    python3 scripts/fetch_himalayas_jobs.py
    python3 scripts/fetch_himalayas_jobs.py --dry-run
    python3 scripts/fetch_himalayas_jobs.py --query kotlin --pages 3
"""
import argparse
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEFAULT_MAX_AGE_DAYS, is_java_role, load_jobs, load_log, posting_age_days, save_jobs  # noqa: E402
import backend  # noqa: E402
from fetch_ats_jobs import NON_DEV_TITLE_RE, get_json, html_to_text  # noqa: E402
from fetch_jobs import (  # noqa: E402
    hard_exclusion, load_screen_cache, posting_hash, read_work_authorization, save_screen_cache, screen_citizenship,
)

SEARCH_URL = "https://himalayas.app/jobs/api/search?q={query}&country={country}&page={page}"
DEFAULT_PAGES = 8  # 20 per page; relevance drops off fast after the first few pages
DEFAULT_LIMIT = 20
NOT_DEV_TITLE_RE = re.compile(
    r"\b(manager|analyst|artist|designer|coordinator|owner|recruiter|consultant|academy|intern)\b", re.I
)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def try_json(url: str):
    try:
        return get_json(url)
    except Exception:
        return None


def board_postings(slug: str) -> list[tuple[str, str]]:
    """(title, apply url) for every open posting on any supported ATS board under this slug."""
    out = []
    d = try_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if isinstance(d, dict) and "jobs" in d:
        out += [(j["title"], j["absolute_url"]) for j in d["jobs"]]
    d = try_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if isinstance(d, list):
        out += [(j["text"], j.get("applyUrl") or j["hostedUrl"]) for j in d]
    d = try_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if isinstance(d, dict) and "jobs" in d:
        out += [(j["title"], j.get("applyUrl") or j["jobUrl"]) for j in d["jobs"]]
    d = try_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if isinstance(d, dict) and "jobs" in d:
        out += [(j["title"], j.get("application_url") or j["url"]) for j in d["jobs"]]
    d = try_json(f"https://{slug}.recruitee.com/api/offers/")
    if isinstance(d, dict) and "offers" in d:
        out += [(j["title"], j.get("careers_apply_url") or j["careers_url"]) for j in d["offers"]]
    d = try_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    if isinstance(d, dict) and d.get("content"):
        out += [(j["name"], f"https://jobs.smartrecruiters.com/{slug}/{j['id']}") for j in d["content"]]
    d = try_json(f"https://{slug}.breezy.hr/json")
    if isinstance(d, list):
        out += [(j["name"], j["url"]) for j in d]
    return out


def resolve_apply_url(posting: dict) -> str | None:
    name = norm(posting["companyName"])
    slugs = dict.fromkeys([posting["companySlug"], posting["companySlug"].replace("-", ""),
                           name.replace(" ", ""), name.replace(" ", "-")])
    for slug in slugs:
        for title, url in board_postings(slug):
            if norm(title) == norm(posting["title"]):
                return url
    return None


def search(query: str, pages: int) -> list[dict]:
    seen = {}
    for page in range(1, pages + 1):
        data = try_json(SEARCH_URL.format(query=urllib.parse.quote(query), country="AM", page=page))
        if not data or not data.get("jobs"):
            break
        for p in data["jobs"]:
            seen.setdefault(p["guid"], p)
    return list(seen.values())


def to_job(p: dict) -> dict:
    restrictions = p.get("locationRestrictions") or []
    return {
        "id": "himalayas-" + "-".join(urllib.parse.urlparse(p["guid"]).path.strip("/").split("/")[1::2]),
        "title": p["title"],
        "companyName": p["companyName"],
        "location": "Remote — " + (", ".join(restrictions) if restrictions else "worldwide"),
        "descriptionText": html_to_text(p.get("description")),
        "postedAt": datetime.fromtimestamp(p["pubDate"], timezone.utc).date().isoformat(),
        "link": p["guid"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; write nothing to found_jobs.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Stop once this many new/updated leads are saved (default {DEFAULT_LIMIT})")
    parser.add_argument("--query", default="java", help="Himalayas search query (default: java)")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help=f"Result pages to read (default {DEFAULT_PAGES})")
    parser.add_argument("--backend", choices=backend.BACKENDS,
                        help="Screening engine for this run (default: ./autoapply.sh backend setting)")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"Skip postings older than this (default {DEFAULT_MAX_AGE_DAYS}; 0 = no limit)")
    parser.add_argument("--any-stack", action="store_true", help="Keep non-Java / non-developer roles too")
    args = parser.parse_args()
    if args.backend:
        os.environ["AUTOAPPLY_BACKEND"] = args.backend

    work_authorization = read_work_authorization()
    jobs_by_id = {j["id"]: j for j in load_jobs()}
    logged = set(load_log().keys())
    cache = load_screen_cache()
    counts = dict.fromkeys(("new", "updated", "unchanged", "logged", "stale", "not_java", "unresolved",
                            "excluded", "ambiguous", "llm_calls"), 0)

    postings = search(args.query, args.pages)
    print(f"Himalayas: {len(postings)} postings open to Armenia for {args.query!r}")
    candidates = []
    for p in postings:
        job = to_job(p)
        if job["id"] in logged:
            counts["logged"] += 1
            continue
        age = posting_age_days(job)
        if args.max_age_days and age is not None and age > args.max_age_days:
            counts["stale"] += 1
            continue
        if not args.any_stack and (NON_DEV_TITLE_RE.search(job["title"]) or NOT_DEV_TITLE_RE.search(job["title"])
                                   or not is_java_role(job)):
            counts["not_java"] += 1
            continue
        candidates.append((p, job))

    with ThreadPoolExecutor(12) as pool:
        apply_urls = list(pool.map(lambda pj: resolve_apply_url(pj[0]), candidates))

    for (p, job), apply_url in zip(candidates, apply_urls):
        if counts["new"] + counts["updated"] >= args.limit:
            print(f"\nLimit of {args.limit} new/updated leads reached — stopped early.")
            break
        label = f"{job['companyName']} — {job['title']} [{job['location']}]"
        if not apply_url:
            counts["unresolved"] += 1
            print(f"no public ATS form found, skipping: {label} — {job['link']}")
            continue
        job["applyUrl"] = apply_url
        existing = jobs_by_id.get(job["id"])
        if existing is not None and existing.get("descriptionText") == job["descriptionText"]:
            counts["unchanged"] += 1
            continue

        h = posting_hash(job)
        entry = cache.get(job["id"])
        if entry and entry.get("hash") == h and "verdict" in entry:
            verdict, tag = entry["verdict"], " (cached)"
        else:
            verdict, cacheable, tag = hard_exclusion(job), True, ""
            if verdict is None:
                verdict, cacheable = screen_citizenship(work_authorization, job)
                counts["llm_calls"] += 1
            if cacheable:
                cache[job["id"]] = {"hash": h, "verdict": verdict}
                save_screen_cache(cache)
        job["fitVerdict"] = verdict
        if verdict["status"] != "passed":
            counts[verdict["status"]] += 1
            print(f"{verdict['status']} (citizenship){tag}: {label} — {verdict['reason']}")
            continue

        counts["updated" if existing is not None else "new"] += 1
        print(f"{'changed, updating' if existing is not None else 'new'}{tag}: {label} -> {apply_url}")
        job.update({"descriptionHtml": "", "jobPosterName": None, "companyWebsite": ""})
        if not args.dry_run:
            jobs_by_id[job["id"]] = job

    print(
        f"\nSummary: {counts['new']} new, {counts['updated']} updated, {counts['unchanged']} unchanged, "
        f"{counts['logged']} already logged, {counts['stale']} older than {args.max_age_days} days, "
        f"{counts['not_java']} not a Java developer role, {counts['unresolved']} without a public ATS form, "
        f"{counts['excluded']} excluded + {counts['ambiguous']} ambiguous (citizenship). "
        f"{counts['llm_calls']} screening call(s)."
    )
    if not args.dry_run and counts["new"] + counts["updated"]:
        save_jobs(list(jobs_by_id.values()))
        print(f"found_jobs.json updated ({len(jobs_by_id)} total leads).")


if __name__ == "__main__":
    main()
