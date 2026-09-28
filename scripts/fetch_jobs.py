#!/usr/bin/env python3
"""Pull new remote backend job leads from CodingJobBoard.com.

Standalone equivalent of the full pipeline's fetch_codingjobboard_jobs.py. Same scraping
approach (Playwright + a real Chrome, since the site is JS-rendered and blocks plain HTTP
requests on some paths), but dedup is against found_jobs.json + applied_log.json directly
instead of an Obsidian vault's staging notes — this package has no vault.

  1. Loads https://www.codingjobboard.com/jobs-in/remote (NOT /alljobs/, which 403s outright)
     and clicks "Show More Jobs" up to MAX_LOAD_MORE_CLICKS times to page in more listing cards
     (this listing page is infinite-scroll style, AJAX-appended, no distinct per-page URL). The
     page's own "keywords" search box was tried here and reverted — confirmed by inspection that
     it only RE-RANKS the full listing (puts likely matches near the top) rather than actually
     reducing which postings are returned; a search for "kubernetes" still returned the same ~150
     total jobs as no search at all, with unrelated postings ("Software QA Engineer") still
     present near the bottom. So there is no way to filter server-side before fetching — every
     posting on the listing has to be detail-fetched and stack-checked regardless.
  2. Collects each listing card's job-title link (href pattern /job/<slug>/<numeric-id>) and
     dedupes by that trailing numeric id.
  3. Opens each candidate's detail page and extracts title/company/location from the <title> tag
     (format: "<title> at <company> in <location>") and the body description text, bounded
     between the job-meta header block and the "APPLY" button.
  4. Requires a stack keyword to appear in the posting's own title/location/descriptionText (the
     /jobs-in/remote listing is already remote-scoped, so no separate remote-keyword check), and
     — unless --any-stack — requires a Java role (common.is_java_role: Java/Spring mentioned, and
     the title doesn't name another primary stack like Go/Python/Node/React). The candidate only
     knows Java.
  5. Screens each stack-matching posting's citizenship/work-authorization requirement against
     profile.md's "Work Authorization" line via a headless LLM — `claude -p` or `cursor-agent -p`,
     whichever backend is active (see scripts/backend.py). Only postings judged "passed" get
     saved — "excluded" and "ambiguous" postings are printed with the reason but never written to
     found_jobs.json, since this package's apply step submits with no human review and shouldn't
     see a job it hasn't cleared. A posting already saved with unchanged description text is never
     re-screened; a changed description is re-screened just like it's re-saved.
  6. Fallback: if that leaves zero new/updated leads (e.g. a run where everything came back
     "ambiguous" or "excluded"), java postings that specifically came back "ambiguous" (not
     "excluded" — those have an explicit disqualifying requirement, re-asking won't change that)
     are re-screened once more with a stricter, decisive prompt (`screen_fully_remote`) that only
     accepts postings explicitly open to work from anywhere with no location/citizenship
     restriction at all — no more "ambiguous" bucket, just passed/excluded. This never triggers on
     a run that already found something; it's a safety net against ending completely empty-handed.
  7. Stops as soon as --limit (default 10) new/updated leads have been saved this run, so a run
     never screens more postings than it needs.
  8. Freshness: each detail page's header carries the posted date (MM/DD/YYYY). Postings older
     than --max-age-days (default 30; 0 disables) are skipped before any screening, and the date
     is saved as "postedAt". A posting whose date can't be read is kept, with a note printed.

Token budget: every LLM verdict (including "excluded"/"ambiguous" ones, which never reach
found_jobs.json) is cached in screened_jobs.json keyed by job id + a hash of the posting text, so a
rejected posting is never paid for twice. Postings that trip an unambiguous hard-exclusion pattern
(e.g. "must be a US citizen", security clearance) are excluded without an LLM call at all. The
call itself sends only profile.md's "Work Authorization" line with a minimal system prompt, from a
temp dir so no CLAUDE.md/rules get auto-loaded. Models: SCREEN_MODEL (claude backend, default
"haiku") or CURSOR_SCREEN_MODEL (cursor backend, default: the account's default model).

A job already logged with ANY status in applied_log.json is skipped outright — it's already been
handled. A job already present in found_jobs.json is updated in place if its description text
changed, otherwise left alone. Everything else is appended as a new lead (after passing the
citizenship screen, or the java-only fallback screen, above).

Usage:
    python3 scripts/fetch_jobs.py
    python3 scripts/fetch_jobs.py --limit 5   # stop after 5 new/updated leads
    python3 scripts/fetch_jobs.py --max-age-days 14   # only postings from the last 2 weeks
    python3 scripts/fetch_jobs.py --dry-run   # show what would happen (citizenship screening
                                               # included), write nothing to found_jobs.json
                                               # (verdicts are still cached)
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEFAULT_MAX_AGE_DAYS, is_java_role, load_jobs, save_jobs, load_log, posting_age_days  # noqa: E402
import backend  # noqa: E402
from backend import call_llm  # noqa: E402

CHROME_PATH = "/usr/bin/google-chrome"
LISTING_URL = "https://www.codingjobboard.com/jobs-in/remote"
MAX_LOAD_MORE_CLICKS = 4
DEFAULT_LIMIT = 10
ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / "profile.md"
SCREEN_CACHE_FILE = ROOT / "screened_jobs.json"
DESCRIPTION_CHARS = 4000
JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")
SCREEN_SYSTEM_PROMPT = "You classify job postings. Reply with only the requested single-line JSON object."
WORK_AUTH_RE = re.compile(r"^\s*Work Authorization:\s*(.+)$", re.I | re.M)

# Unambiguous disqualifiers for this candidate — excluded without spending an LLM call.
HARD_EXCLUDE_RE = re.compile(
    r"\b(must be an? (u\.?s\.?|united states) citizen"
    r"|(u\.?s\.?|united states) citizens?(hip)?( is)? (required|only)"
    r"|ts/sci|top secret|secret clearance|active (security )?clearance|security clearance( is)? required"
    r"|w-?2 only)\b",
    re.I,
)

SCREEN_PROMPT_TEMPLATE = """You are screening a single job posting for whether its citizenship/
work-authorization requirement is compatible with this candidate.

Candidate's work authorization (verbatim from their profile):
{work_authorization}

Job posting:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

IMPORTANT: a posting that restricts eligibility to a specific list of countries/regions (e.g.
"open to applicants in Spain, Sweden, UK, Ireland, or Germany", "must be based in the EU") is a
citizenship/work-authorization restriction even if it never uses the words "citizenship" or "work
authorization" — judge it the same way you would an explicit citizenship requirement. If Armenia
(the candidate's own country) is not in that list, this is "excluded", never "passed", regardless
of whether the posting also says "remote" — "remote" describing a role that is nonetheless scoped
to specific countries is not the same as remote-anywhere.

Decide one of:
- "passed" — the posting does not restrict eligibility to a specific country/region list that
  excludes Armenia, and does not require a citizenship/work-authorization status the candidate
  lacks (e.g. no citizenship or location restriction mentioned at all, explicitly flexible/
  remote-anywhere/worldwide, C2C/1099/contract-friendly with no location caveat, visa sponsorship
  offered, or a country list that DOES include Armenia).
- "excluded" — the posting explicitly requires a citizenship or work authorization the candidate's
  profile does not state they have (e.g. "must be a US citizen", "must be authorized to work in
  the EU", a security-clearance role requiring citizenship the candidate doesn't hold), OR it
  restricts eligibility to a country/region list that does not include Armenia (see IMPORTANT
  above) — this is the more common case in practice, not an edge case.
- "ambiguous" — the posting doesn't clearly state its citizenship/work-authorization requirement
  either way, AND doesn't list any specific eligible countries/regions either.

Do not guess a genuinely unclear posting into "excluded" — use "ambiguous" instead. But a country/
region list that excludes Armenia is NOT ambiguous — it's "excluded", per the IMPORTANT rule above.

Respond with ONLY a single-line JSON object, no other text: {{"status": "passed|excluded|ambiguous", "reason": "one short sentence"}}
"""

FALLBACK_PROMPT_TEMPLATE = """You are re-screening a Java job posting that came back "ambiguous"
on a first pass — this is a fallback used only because that normal run found zero postings worth
saving. This time, give a decisive answer instead of "ambiguous".

Candidate's work authorization (verbatim from their profile):
{work_authorization}

Job posting:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Decide one of:
- "passed" — the posting is genuinely open to being done from anywhere in the world, with no
  location/citizenship restriction: it explicitly says remote-anywhere, hires globally, or is
  C2C/1099/contract-friendly with no location caveat.
- "excluded" — anything else, including a location tag next to "Remote" that suggests a regional
  restriction (e.g. "Remote" + "Spain"), or the posting simply never saying either way. Unlike the
  first pass, silence here means "excluded," not another round of "ambiguous" — this fallback only
  wants postings that are unambiguously open to anyone, anywhere.

Respond with ONLY a single-line JSON object, no other text: {{"status": "passed|excluded", "reason": "one short sentence"}}
"""


def read_work_authorization() -> str:
    if not PROFILE_FILE.exists():
        raise SystemExit(
            f"{PROFILE_FILE} not found — copy profile.md.example to profile.md and fill in your "
            "own facts (including Work Authorization) before running the fetcher, since it now "
            "screens every posting's citizenship requirement against that field."
        )
    m = WORK_AUTH_RE.search(PROFILE_FILE.read_text(encoding="utf-8"))
    if not m or not m.group(1).strip():
        raise SystemExit(
            f'{PROFILE_FILE} has no "Work Authorization: ..." line — add one before running the '
            "fetcher, it's the only profile fact the citizenship screen uses."
        )
    return m.group(1).strip()


def ask_verdict(template: str, work_authorization: str, job: dict, allowed: tuple) -> dict:
    prompt = template.format(
        work_authorization=work_authorization,
        title=job["title"],
        company=job["companyName"],
        location=job["location"],
        description=job["descriptionText"][:DESCRIPTION_CHARS],
    )
    raw = call_llm(prompt, SCREEN_SYSTEM_PROMPT)
    m = JSON_OBJECT_RE.search(raw)
    if not m:
        raise ValueError(f"no JSON object in reply: {raw[:200]!r}")
    verdict = json.loads(m.group(0))
    if verdict.get("status") not in allowed:
        raise ValueError(f"unexpected status: {verdict.get('status')!r}")
    return {"status": verdict["status"], "reason": verdict.get("reason", "")}


def hard_exclusion(job: dict) -> dict | None:
    m = HARD_EXCLUDE_RE.search(f"{job['title']} {job['location']} {job['descriptionText']}")
    if not m:
        return None
    return {"status": "excluded", "reason": f"hard-exclusion pattern matched: {m.group(0)!r}"}


def screen_citizenship(work_authorization: str, job: dict) -> tuple[dict, bool]:
    """Returns (verdict, cacheable). Screening failures are not cacheable — they're transient."""
    try:
        return ask_verdict(SCREEN_PROMPT_TEMPLATE, work_authorization, job, ("passed", "excluded", "ambiguous")), True
    except Exception as e:
        return {"status": "excluded", "reason": f"citizenship screening failed, excluding conservatively: {e}"}, False


def screen_fully_remote(work_authorization: str, job: dict) -> tuple[dict, bool]:
    try:
        return ask_verdict(FALLBACK_PROMPT_TEMPLATE, work_authorization, job, ("passed", "excluded")), True
    except Exception as e:
        return {"status": "excluded", "reason": f"fallback screening failed, excluding conservatively: {e}"}, False


def posting_hash(job: dict) -> str:
    text = f"{job['title']}\n{job['location']}\n{job['descriptionText']}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_screen_cache() -> dict:
    if SCREEN_CACHE_FILE.exists():
        return json.loads(SCREEN_CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_screen_cache(cache: dict) -> None:
    SCREEN_CACHE_FILE.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


STACK_RE = re.compile(
    r"\b(java|spring boot|spring|golang|node\.?js|react|vue|typescript|backend|"
    r"back-end|full[- ]?stack|microservices|mongodb|postgres|rest api|kubernetes|docker)\b",
    re.I,
)
JAVA_ONLY_RE = re.compile(r"\bjava\b", re.I)
JOB_LINK_ID_RE = re.compile(r"/job/[^/]+/(\d+)")
TITLE_TAG_RE = re.compile(r"^(.*?) at (.*?) in (.*)$")
DESCRIPTION_END_RE = re.compile(r"^(APPLY)$")
POSTED_DATE_RE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$")


def extract_posted_date(body_text: str) -> str | None:
    """First standalone MM/DD/YYYY line in the job-meta header, as an ISO date."""
    for line in body_text.split("\n")[:80]:
        m = POSTED_DATE_RE.match(line)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
            except ValueError:
                return None
    return None


def list_remote_results(page) -> list[dict]:
    page.goto(LISTING_URL, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    for _ in range(MAX_LOAD_MORE_CLICKS):
        more_btn = page.get_by_role("button", name=re.compile("show more jobs", re.I)).first
        try:
            if not more_btn.is_visible(timeout=2000):
                break
            more_btn.click(timeout=5000)
            page.wait_for_timeout(1500)
        except Exception:
            break
    return page.eval_on_selector_all(
        'a[href^="/job/"]',
        "els => els.map(el => ({title: el.textContent.trim(), href: el.href}))",
    )


def extract_description(body_text: str) -> str:
    lines = body_text.split("\n")
    end = len(lines)
    for i, line in enumerate(lines):
        if DESCRIPTION_END_RE.match(line.strip()):
            end = i
            break
    # Description content starts after the job-meta header block (title/company/type/location/
    # date/share-icon lines) — skip short (<40 char) lines at the top before real prose kicks in.
    start = 0
    for i, line in enumerate(lines[:end]):
        if len(line.strip()) > 40:
            start = i
            break
    return "\n".join(lines[start:end]).strip()


def fetch_detail(page, url: str) -> dict | None:
    page.goto(url, timeout=20000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    m = TITLE_TAG_RE.match(page.title().strip())
    if not m:
        return None
    title, company, location = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()

    body_text = page.inner_text("body")
    description = extract_description(body_text)
    return {"title": title, "company": company, "location": location, "descriptionText": description,
            "postedAt": extract_posted_date(body_text)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen; write nothing to found_jobs.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Stop once this many new/updated leads are saved (default {DEFAULT_LIMIT})")
    parser.add_argument("--backend", choices=backend.BACKENDS,
                        help="Screening engine for this run (default: ./autoapply.sh backend setting)")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"Skip postings older than this (default {DEFAULT_MAX_AGE_DAYS}; 0 = no limit)")
    parser.add_argument("--any-stack", action="store_true",
                        help="Keep non-Java roles too (by default only Java/Spring roles are kept)")
    args = parser.parse_args()
    if args.backend:
        os.environ["AUTOAPPLY_BACKEND"] = args.backend
    active_backend = backend.get_backend()
    print(f"Screening backend: {active_backend} ({backend.model_label(active_backend)})")

    from playwright.sync_api import sync_playwright

    work_authorization = read_work_authorization()
    jobs = load_jobs()
    jobs_by_id = {j["id"]: j for j in jobs}
    applied_ids = set(load_log().keys())
    cache = load_screen_cache()

    new_count = updated_count = unchanged_count = already_logged_count = 0
    excluded_count = ambiguous_count = stack_match_count = stale_count = 0
    llm_calls = cached_hits = prefiltered = 0
    java_ambiguous_candidates = []
    limit_reached = False

    def cached_or_screen(job: dict, verdict_key: str, screen_fn) -> tuple[dict, bool]:
        """Returns (verdict, came_from_cache), caching any fresh, cacheable verdict."""
        nonlocal llm_calls, prefiltered
        h = posting_hash(job)
        entry = cache.get(job["id"])
        if entry and entry.get("hash") == h and verdict_key in entry:
            return entry[verdict_key], True
        if not entry or entry.get("hash") != h:
            entry = {"hash": h}
        verdict = hard_exclusion(job) if verdict_key == "verdict" else None
        cacheable = True
        if verdict:
            prefiltered += 1
        else:
            verdict, cacheable = screen_fn(work_authorization, job)
            llm_calls += 1
        if cacheable:
            entry[verdict_key] = verdict
            cache[job["id"]] = entry
            save_screen_cache(cache)
        return verdict, False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME_PATH)
        page = browser.new_page()

        try:
            items = list_remote_results(page)
        except Exception as e:
            browser.close()
            raise SystemExit(f"Failed to load listing page ({LISTING_URL}): {e}")

        seen_ids = set()
        listing_candidates = []
        for it in items:
            href = it.get("href") or ""
            m = JOB_LINK_ID_RE.search(href)
            if not m:
                continue
            jid = f"codingjobboard-{m.group(1)}"
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            listing_candidates.append({"id": jid, "href": href, "list_title": it.get("title") or ""})

        print(f"Distinct listings found: {len(listing_candidates)}")

        for c in listing_candidates:
            if new_count + updated_count >= args.limit:
                limit_reached = True
                break
            jid = c["id"]
            if jid in applied_ids:
                already_logged_count += 1
                print(f"already logged, skipping: {c['list_title']} ({jid})")
                continue
            try:
                detail = fetch_detail(page, c["href"])
            except Exception as e:
                print(f"skipping {c['list_title']} (failed to load detail page: {e})")
                continue
            page.wait_for_timeout(600)
            if not detail:
                continue

            age = posting_age_days(detail)
            if age is None:
                print(f"no posted date found, keeping: {detail['title']} ({jid})")
            elif args.max_age_days and age > args.max_age_days:
                stale_count += 1
                print(f"too old ({age} days), skipping: {detail['company']} — {detail['title']}")
                continue

            haystack = f"{detail['title']} {detail['location']} {detail['descriptionText']}"
            if not STACK_RE.search(haystack):
                continue
            if not args.any_stack and not is_java_role(detail):
                print(f"not a Java role, skipping: {detail['company']} — {detail['title']}")
                continue
            stack_match_count += 1

            job = {
                "id": jid,
                "title": detail["title"],
                "companyName": detail["company"],
                "location": detail["location"],
                "descriptionText": detail["descriptionText"],
                "postedAt": detail["postedAt"],
                "descriptionHtml": "",
                "link": c["href"],
                "applyUrl": "",
                "jobPosterName": None,
                "companyWebsite": "",
            }
            existing = jobs_by_id.get(jid)
            if existing is not None and existing.get("descriptionText") == job["descriptionText"]:
                unchanged_count += 1
                continue

            verdict, from_cache = cached_or_screen(job, "verdict", screen_citizenship)
            cached_hits += from_cache
            tag = " (cached)" if from_cache else ""
            job["fitVerdict"] = verdict

            if verdict["status"] == "excluded":
                excluded_count += 1
                print(f"excluded (citizenship){tag}: {job['companyName']} — {job['title']} — {verdict['reason']}")
                continue
            if verdict["status"] == "ambiguous":
                # No human reviews this package's apply step before it submits, so an unclear
                # citizenship requirement is excluded here rather than included-and-flagged the way
                # the full pipeline's interactive browse-jobs-agent handles ambiguity.
                ambiguous_count += 1
                print(f"ambiguous (citizenship), excluding conservatively{tag}: {job['companyName']} — {job['title']} — {verdict['reason']}")
                if JAVA_ONLY_RE.search(f"{job['title']} {job['descriptionText']}"):
                    java_ambiguous_candidates.append(job)
                continue

            if existing is not None:
                updated_count += 1
                print(f"changed, updating: {job['companyName']} — {job['title']}")
            else:
                new_count += 1
                print(f"new: {job['companyName']} — {job['title']}")
            if not args.dry_run:
                jobs_by_id[jid] = job

        browser.close()

    if limit_reached:
        print(f"\nLimit of {args.limit} new/updated leads reached — stopped early.")
    print(
        f"\nSummary: {new_count} new, {updated_count} updated, "
        f"{unchanged_count} unchanged, {already_logged_count} already logged, "
        f"{stale_count} older than {args.max_age_days} days, "
        f"{excluded_count} excluded (citizenship), {ambiguous_count} ambiguous (excluded conservatively), "
        f"{stack_match_count} matched stack"
    )

    fallback_new_count = 0
    if new_count == 0 and updated_count == 0 and java_ambiguous_candidates:
        print(
            f"\nNo leads passed the normal run — re-screening {len(java_ambiguous_candidates)} "
            "java posting(s) that came back ambiguous, with a decisive fully-remote-only bar:"
        )
        for job in java_ambiguous_candidates:
            if fallback_new_count >= args.limit:
                print(f"Limit of {args.limit} reached — stopping java fallback early.")
                break
            verdict, from_cache = cached_or_screen(job, "fallbackVerdict", screen_fully_remote)
            cached_hits += from_cache
            tag = " (cached)" if from_cache else ""
            job["fitVerdict"] = verdict
            if verdict["status"] == "passed":
                fallback_new_count += 1
                print(f"java fallback, passed{tag}: {job['companyName']} — {job['title']} — {verdict['reason']}")
                if not args.dry_run:
                    jobs_by_id[job["id"]] = job
            else:
                print(f"java fallback, still excluded{tag}: {job['companyName']} — {job['title']} — {verdict['reason']}")
        print(f"Java fallback summary: {fallback_new_count} passed out of {len(java_ambiguous_candidates)}")

    print(
        f"Screening cost: {llm_calls} {active_backend} call(s) ({backend.model_label(active_backend)}), "
        f"{cached_hits} cached verdict(s) reused, {prefiltered} hard-excluded without a call."
    )

    if args.dry_run or (new_count == 0 and updated_count == 0 and fallback_new_count == 0):
        return

    save_jobs(list(jobs_by_id.values()))
    print(f"found_jobs.json updated ({len(jobs_by_id)} total leads).")


if __name__ == "__main__":
    main()
