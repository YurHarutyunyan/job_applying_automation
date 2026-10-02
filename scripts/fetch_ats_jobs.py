#!/usr/bin/env python3
"""Pull remote Java leads straight from companies' own Greenhouse / Lever / Ashby job boards.

Second lead source next to fetch_jobs.py (CodingJobBoard). These three ATSes publish every
company's openings as public JSON, so no browser is needed, and each posting's apply URL is the
ATS form itself — the same forms Simplify autofills. Which companies to poll lives in
ats_companies.json ({"greenhouse": [board slugs], "lever": [...], "ashby": [...]}); a slug is the
company's segment in its board URL (job-boards.greenhouse.io/<slug>, jobs.lever.co/<slug>,
jobs.ashbyhq.com/<slug>).

Per posting, in order (cheapest first, so most postings never cost an LLM call):
  1. Remote only — Greenhouse/Lever location text naming remote/anywhere, Lever workplaceType
     "remote", or Ashby isRemote.
  2. Location not scoped to a country/region list that obviously excludes Armenia ("Remote - US",
     "Remote (Canada)", "Seattle, WA") — unless it also names EMEA/Europe/Asia/global/anywhere.
  3. Fresh (--max-age-days, default 30), a developer role (QA/test/release/support titles are
     dropped) and a Java role (common.is_java_role; --any-stack skips both role checks).
  4. Citizenship/work-authorization screen, reusing fetch_jobs.py's hard-exclusion regex, LLM
     prompt and screened_jobs.json verdict cache. Only "passed" postings are saved.

Ids are "<ats>-<slug>-<posting id>". Dedup and updates work like fetch_jobs.py: anything already in
applied_log.json is skipped, a saved lead is updated if its description changed.

Usage:
    python3 scripts/fetch_ats_jobs.py
    python3 scripts/fetch_ats_jobs.py --dry-run
    python3 scripts/fetch_ats_jobs.py --limit 5 --max-age-days 14
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEFAULT_MAX_AGE_DAYS, ROOT, is_java_role, load_jobs, load_log, posting_age_days, save_jobs  # noqa: E402
import backend  # noqa: E402
from fetch_jobs import (  # noqa: E402
    hard_exclusion, load_screen_cache, posting_hash, read_work_authorization, save_screen_cache, screen_citizenship,
)

COMPANIES_FILE = ROOT / "ats_companies.json"
DEFAULT_LIMIT = 20
TAG_RE = re.compile(r"<[^>]+>")
REMOTE_RE = re.compile(r"\b(remote|anywhere|worldwide|distributed)\b", re.I)
OPEN_REGION_RE = re.compile(r"\b(emea|europe|eu|asia|apac|global|anywhere|worldwide|armenia|international)\b", re.I)
NON_DEV_TITLE_RE = re.compile(r"\b(qa|quality|test|tester|testing|release|support|sales|solutions? architect)\b", re.I)
CLOSED_REGION_RE = re.compile(
    r"(?i:\b(us|usa|u\.s\.|united states|canada|brazil|brasil|india|mexico|latam|australia|japan|uk|"
    r"united kingdom|germany|france|spain|poland|lithuania|portugal|netherlands|singapore)\b)"
    r"|,\s*[A-Z]{2}\b"  # a US state suffix, e.g. "Seattle, WA"
)


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def html_to_text(s: str) -> str:
    return re.sub(r"\n\s*\n+", "\n\n", TAG_RE.sub("\n", html.unescape(html.unescape(s or "")))).strip()


def iso_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def greenhouse(slug: str) -> list[dict]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [{
        "id": f"greenhouse-{slug}-{j['id']}",
        "title": j["title"],
        "companyName": j.get("company_name") or slug,
        "location": (j.get("location") or {}).get("name", ""),
        "remote": bool(REMOTE_RE.search((j.get("location") or {}).get("name", ""))),
        "descriptionText": html_to_text(j.get("content")),
        "postedAt": iso_date(j.get("first_published") or j.get("updated_at")),
        "link": j["absolute_url"],
        "applyUrl": j["absolute_url"],  # the Greenhouse posting page hosts the form itself
    } for j in data.get("jobs", [])]


def lever(slug: str) -> list[dict]:
    jobs = []
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        location = (j.get("categories") or {}).get("location") or ""
        lists = "\n\n".join(f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in j.get("lists", []))
        jobs.append({
            "id": f"lever-{slug}-{j['id']}",
            "title": j["text"],
            "companyName": slug.capitalize(),
            "location": location,
            "remote": j.get("workplaceType") == "remote" or bool(REMOTE_RE.search(location)),
            "descriptionText": f"{j.get('descriptionPlain', '')}\n\n{lists}\n\n{j.get('additionalPlain', '')}".strip(),
            "postedAt": iso_date(j.get("createdAt")),
            "link": j["hostedUrl"],
            "applyUrl": j.get("applyUrl") or j["hostedUrl"],
        })
    return jobs


def ashby(slug: str) -> list[dict]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for j in data.get("jobs", []):
        locations = [j.get("location") or ""] + [l.get("location", "") for l in j.get("secondaryLocations") or []]
        jobs.append({
            "id": f"ashby-{slug}-{j['id']}",
            "title": j["title"],
            "companyName": slug.capitalize(),
            "location": "; ".join(l for l in locations if l),
            "remote": bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            "descriptionText": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            "postedAt": iso_date(j.get("publishedAt")),
            "link": j["jobUrl"],
            "applyUrl": j.get("applyUrl") or j["jobUrl"],
        })
    return jobs


SOURCES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def region_closed(location: str) -> bool:
    return bool(CLOSED_REGION_RE.search(location)) and not OPEN_REGION_RE.search(location)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; write nothing to found_jobs.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Stop once this many new/updated leads are saved (default {DEFAULT_LIMIT})")
    parser.add_argument("--backend", choices=backend.BACKENDS,
                        help="Screening engine for this run (default: ./autoapply.sh backend setting)")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"Skip postings older than this (default {DEFAULT_MAX_AGE_DAYS}; 0 = no limit)")
    parser.add_argument("--any-stack", action="store_true", help="Keep non-Java roles too")
    args = parser.parse_args()
    if args.backend:
        os.environ["AUTOAPPLY_BACKEND"] = args.backend

    work_authorization = read_work_authorization()
    companies = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
    jobs = load_jobs()
    jobs_by_id = {j["id"]: j for j in jobs}
    logged = set(load_log().keys())
    cache = load_screen_cache()
    counts = dict.fromkeys(("new", "updated", "unchanged", "logged", "not_remote", "region", "stale",
                            "not_java", "excluded", "ambiguous", "llm_calls"), 0)

    candidates = []
    for ats, slugs in companies.items():
        for slug in slugs:
            try:
                postings = SOURCES[ats](slug)
            except Exception as e:
                print(f"{ats}:{slug} failed to load ({e}), skipping")
                continue
            print(f"{ats}:{slug}: {len(postings)} postings")
            candidates.extend(postings)

    for job in candidates:
        if counts["new"] + counts["updated"] >= args.limit:
            print(f"\nLimit of {args.limit} new/updated leads reached — stopped early.")
            break
        if job["id"] in logged:
            counts["logged"] += 1
            continue
        if not job.pop("remote"):
            counts["not_remote"] += 1
            continue
        if region_closed(job["location"]):
            counts["region"] += 1
            continue
        age = posting_age_days(job)
        if args.max_age_days and age is not None and age > args.max_age_days:
            counts["stale"] += 1
            continue
        if not args.any_stack and (NON_DEV_TITLE_RE.search(job["title"]) or not is_java_role(job)):
            counts["not_java"] += 1
            continue
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
        label = f"{job['companyName']} — {job['title']} [{job['location']}]"
        if verdict["status"] != "passed":
            counts[verdict["status"]] += 1
            print(f"{verdict['status']} (citizenship){tag}: {label} — {verdict['reason']}")
            continue

        counts["updated" if existing is not None else "new"] += 1
        print(f"{'changed, updating' if existing is not None else 'new'}{tag}: {label}")
        job.update({"descriptionHtml": "", "jobPosterName": None, "companyWebsite": ""})
        if not args.dry_run:
            jobs_by_id[job["id"]] = job

    print(
        f"\nSummary: {counts['new']} new, {counts['updated']} updated, {counts['unchanged']} unchanged, "
        f"{counts['logged']} already logged, {counts['not_remote']} not remote, {counts['region']} region-locked, "
        f"{counts['stale']} older than {args.max_age_days} days, {counts['not_java']} not Java, "
        f"{counts['excluded']} excluded + {counts['ambiguous']} ambiguous (citizenship). "
        f"{counts['llm_calls']} screening call(s)."
    )
    if not args.dry_run and counts["new"] + counts["updated"]:
        save_jobs(list(jobs_by_id.values()))
        print(f"found_jobs.json updated ({len(jobs_by_id)} total leads).")


if __name__ == "__main__":
    main()
