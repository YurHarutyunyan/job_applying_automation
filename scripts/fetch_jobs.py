#!/usr/bin/env python3
"""Pull new remote backend job leads from CodingJobBoard.com.

Standalone equivalent of the full pipeline's fetch_codingjobboard_jobs.py. Same scraping
approach (Playwright + a real Chrome, since the site is JS-rendered and blocks plain HTTP
requests on some paths), but dedup is against found_jobs.json + applied_log.json directly
instead of an Obsidian vault's staging notes — this package has no vault.

  1. Loads https://www.codingjobboard.com/jobs-in/remote (NOT /alljobs/, which 403s outright)
     and clicks "Show More Jobs" up to MAX_LOAD_MORE_CLICKS times to page in more listing cards
     (this listing page is infinite-scroll style, AJAX-appended, no distinct per-page URL).
  2. Collects each listing card's job-title link (href pattern /job/<slug>/<numeric-id>) and
     dedupes by that trailing numeric id.
  3. Opens each candidate's detail page and extracts title/company/location from the <title> tag
     (format: "<title> at <company> in <location>") and the body description text, bounded
     between the job-meta header block and the "APPLY" button.
  4. Requires a stack keyword to appear in the posting's own title/location/descriptionText (the
     /jobs-in/remote listing is already remote-scoped, so no separate remote-keyword check).
  5. Screens each stack-matching posting's citizenship/work-authorization requirement against
     profile.md's "Work Authorization" line via headless Claude (`claude -p`, same subprocess
     pattern tailor_cv.py uses in the full pipeline). Only postings Claude judges "passed" get
     saved — "excluded" and "ambiguous" postings are printed with the reason but never written to
     found_jobs.json, since this package's apply step submits with no human review and shouldn't
     see a job it hasn't cleared. A posting already saved with unchanged description text is never
     re-screened; a changed description is re-screened just like it's re-saved.

A job already logged with ANY status in applied_log.json is skipped outright — it's already been
handled. A job already present in found_jobs.json is updated in place if its description text
changed, otherwise left alone. Everything else is appended as a new lead (after passing the
citizenship screen above).

Usage:
    python3 scripts/fetch_jobs.py
    python3 scripts/fetch_jobs.py --dry-run   # show what would happen (citizenship screening
                                               # included), write nothing
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_jobs, save_jobs, load_log  # noqa: E402

CHROME_PATH = "/usr/bin/google-chrome"
LISTING_URL = "https://www.codingjobboard.com/jobs-in/remote"
MAX_LOAD_MORE_CLICKS = 4
ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / "profile.md"

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

Decide one of:
- "passed" — the posting does not require a citizenship/work-authorization status the candidate
  lacks (e.g. no citizenship mentioned, flexible/remote-anywhere, C2C/1099/contract-friendly,
  visa sponsorship offered, or it matches the candidate's stated authorization).
- "excluded" — the posting explicitly requires a citizenship or work authorization the candidate's
  profile does not state they have (e.g. "must be a US citizen", "must be authorized to work in
  the EU", a security-clearance role requiring citizenship the candidate doesn't hold).
- "ambiguous" — the posting doesn't clearly state its citizenship/work-authorization requirement
  either way.

Do not guess a genuinely unclear posting into "excluded" — use "ambiguous" instead.

Respond with ONLY a single-line JSON object, no other text: {{"status": "passed|excluded|ambiguous", "reason": "one short sentence"}}
"""


def read_work_authorization() -> str:
    if not PROFILE_FILE.exists():
        raise SystemExit(
            f"{PROFILE_FILE} not found — copy profile.md.example to profile.md and fill in your "
            "own facts (including Work Authorization) before running the fetcher, since it now "
            "screens every posting's citizenship requirement against that field."
        )
    return PROFILE_FILE.read_text(encoding="utf-8")


def call_claude(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", "--tools", "", "--no-session-persistence", "--strict-mcp-config"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {result.returncode}): {result.stderr.strip()}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"claude -p returned empty output. stderr: {result.stderr.strip()}")
    return output


def screen_citizenship(profile_text: str, job: dict) -> dict:
    prompt = SCREEN_PROMPT_TEMPLATE.format(
        work_authorization=profile_text.strip(),
        title=job["title"],
        company=job["companyName"],
        location=job["location"],
        description=job["descriptionText"][:4000],
    )
    try:
        raw = call_claude(prompt)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        verdict = json.loads(raw)
        if verdict.get("status") not in ("passed", "excluded", "ambiguous"):
            raise ValueError(f"unexpected status: {verdict.get('status')!r}")
        return verdict
    except Exception as e:
        return {"status": "excluded", "reason": f"citizenship screening failed, excluding conservatively: {e}"}

STACK_RE = re.compile(
    r"\b(java|spring boot|spring|golang|node\.?js|react|vue|typescript|backend|"
    r"back-end|full[- ]?stack|microservices|mongodb|postgres|rest api|kubernetes|docker)\b",
    re.I,
)
JOB_LINK_ID_RE = re.compile(r"/job/[^/]+/(\d+)")
TITLE_TAG_RE = re.compile(r"^(.*?) at (.*?) in (.*)$")
DESCRIPTION_END_RE = re.compile(r"^(APPLY)$")


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
    return {"title": title, "company": company, "location": location, "descriptionText": description}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, write nothing")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    jobs = load_jobs()
    jobs_by_id = {j["id"]: j for j in jobs}
    applied_ids = set(load_log().keys())

    candidates = []
    already_logged_count = 0
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

            haystack = f"{detail['title']} {detail['location']} {detail['descriptionText']}"
            if not STACK_RE.search(haystack):
                continue

            candidates.append({
                "id": jid,
                "title": detail["title"],
                "companyName": detail["company"],
                "location": detail["location"],
                "descriptionText": detail["descriptionText"],
                "descriptionHtml": "",
                "link": c["href"],
                "applyUrl": "",
                "jobPosterName": None,
                "companyWebsite": "",
            })

        browser.close()

    print(f"Postings matching stack: {len(candidates)}")

    work_authorization = read_work_authorization()

    new_count = updated_count = unchanged_count = 0
    excluded_count = ambiguous_count = 0

    for c in candidates:
        jid = c["id"]
        existing = jobs_by_id.get(jid)
        if existing is not None and existing.get("descriptionText") == c["descriptionText"]:
            unchanged_count += 1
            continue

        verdict = screen_citizenship(work_authorization, c)
        c["fitVerdict"] = verdict

        if verdict["status"] == "excluded":
            excluded_count += 1
            print(f"excluded (citizenship): {c['companyName']} — {c['title']} — {verdict['reason']}")
            continue
        if verdict["status"] == "ambiguous":
            # No human reviews this package's apply step before it submits, so an unclear
            # citizenship requirement is excluded here rather than included-and-flagged the way
            # the full pipeline's interactive browse-jobs-agent handles ambiguity.
            ambiguous_count += 1
            print(f"ambiguous (citizenship), excluding conservatively: {c['companyName']} — {c['title']} — {verdict['reason']}")
            continue

        if existing is not None:
            updated_count += 1
            print(f"changed, updating: {c['companyName']} — {c['title']}")
        else:
            new_count += 1
            print(f"new: {c['companyName']} — {c['title']}")
        if not args.dry_run:
            jobs_by_id[jid] = c

    print(
        f"\nSummary: {new_count} new, {updated_count} updated, "
        f"{unchanged_count} unchanged, {already_logged_count} already logged, "
        f"{excluded_count} excluded (citizenship), {ambiguous_count} ambiguous (excluded conservatively)"
    )

    if args.dry_run or (new_count == 0 and updated_count == 0):
        return

    save_jobs(list(jobs_by_id.values()))
    print(f"found_jobs.json updated ({len(jobs_by_id)} total leads).")


if __name__ == "__main__":
    main()
