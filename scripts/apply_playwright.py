#!/usr/bin/env python3
"""Apply to staged CodingJobBoard leads without Claude: Playwright + Chrome + Simplify, human submits.

The "cursor" backend's apply step (see scripts/backend.py). Unlike codingjobboard-apply-agent,
this never clicks Submit: it does the mechanical part and then waits for you.

For each job:
  1. Opens its CodingJobBoard page in a real (headed) Chrome using a dedicated profile at
     .browser_profile/ that has Simplify installed and signed in (see --setup).
  2. Clicks the page's Apply button and follows it to the off-site ATS (new tab or same tab).
  3. Clicks Simplify's "Autofill This Page" if it can find it; otherwise tells you to click it.
  4. Attaches your CV (default ~/Downloads/Yuri’s CV.pdf, override with --cv or AUTOAPPLY_CV) to
     the form's resume/CV file input, replacing whatever Simplify put there. The file is sent as
     "Yuri's CV.pdf" regardless of its name on disk.
  5. Lists required fields that are still empty, then waits while you review, fill the rest,
     and submit it yourself in the browser window.
  6. Asks what happened and logs it through common.record(): applied / needs_manual_review /
     closed, or leaves it pending.

Usage:
    python3 scripts/apply_playwright.py --setup          # one-time: install + sign in to Simplify
    python3 scripts/apply_playwright.py                  # next pending job
    python3 scripts/apply_playwright.py --limit 3        # next 3 pending jobs, one after another
    python3 scripts/apply_playwright.py --job-id codingjobboard-18774
    python3 scripts/apply_playwright.py --cv ~/some/other.pdf
"""
import argparse
import os
import re
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_jobs, load_log, record, select_job  # noqa: E402
from pending_jobs import pending  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / ".browser_profile"
CHROME_PATH = "/usr/bin/google-chrome"
SIMPLIFY_STORE_URL = "https://chromewebstore.google.com/detail/simplify-copilot-autofill/pbanhockgagggenencehbnadejlgchfc"
SIMPLIFY_LOGIN_URL = "https://simplify.jobs/auth/login"
DEFAULT_CV = Path.home() / "Downloads" / "Yuri’s CV.pdf"
UPLOAD_NAME = "Yuri's CV.pdf"

APPLY_NAME_RE = re.compile(r"^\s*apply\b", re.I)
CLOSED_RE = re.compile(
    r"no longer (accepting applications|available)|job (has )?expired|position (has been )?filled|job not found",
    re.I,
)
SIMPLIFY_AUTOFILL_RE = re.compile(r"autofill this page|^\s*autofill\s*$", re.I)
SIMPLIFY_UNSUPPORTED_RE = re.compile(r"autofill (isn.t|is not) supported", re.I)

EMPTY_REQUIRED_JS = """() => {
  const out = [];
  const els = document.querySelectorAll(
    'input[required], textarea[required], select[required], [aria-required="true"]');
  for (const el of els) {
    if (el.type === 'hidden' || el.disabled || !el.offsetParent) continue;
    let empty;
    if (el.type === 'checkbox' || el.type === 'radio') {
      empty = el.name ? !document.querySelector(`input[name="${CSS.escape(el.name)}"]:checked`) : !el.checked;
    } else if (el.type === 'file') {
      empty = !el.files || el.files.length === 0;
    } else {
      empty = !(el.value || '').trim();
    }
    if (!empty) continue;
    const label = (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('aria-label') || el.placeholder || el.name || el.id || el.tagName;
    out.push(label.trim().replace(/\\s+/g, ' ').slice(0, 80));
  }
  return [...new Set(out)];
}"""

# Index of the file input that looks like the resume/CV slot, or -1. Cover-letter inputs are
# skipped; if nothing is labelled, a lone file input on the page is assumed to be the resume.
RESUME_INPUT_JS = """() => {
  const inputs = [...document.querySelectorAll('input[type=file]')];
  const text = el => [el.name, el.id, el.getAttribute('aria-label'),
    el.labels && [...el.labels].map(l => l.innerText).join(' '),
    el.closest('div, fieldset, li') && el.closest('div, fieldset, li').innerText.slice(0, 200)]
    .filter(Boolean).join(' ').toLowerCase();
  const scored = inputs.map((el, i) => {
    const t = text(el);
    if (/cover/.test(t) && !/resume|\\bcv\\b/.test(t)) return [i, -1];
    return [i, /resume|\\bcv\\b|curriculum/.test(t) ? 2 : 0];
  });
  const best = scored.filter(s => s[1] === 2);
  if (best.length) return best[0][0];
  const usable = scored.filter(s => s[1] === 0);
  return usable.length === 1 ? usable[0][0] : -1;
}"""


def setup():
    PROFILE_DIR.mkdir(exist_ok=True)
    print(f"Opening Chrome with the dedicated profile at {PROFILE_DIR}.")
    print("1) Click 'Add to Chrome' for Simplify Copilot.  2) Sign in to Simplify and make sure")
    print("   your resume/profile is filled in.  3) Close that Chrome window when you're done.")
    subprocess.run([CHROME_PATH, f"--user-data-dir={PROFILE_DIR}", SIMPLIFY_STORE_URL, SIMPLIFY_LOGIN_URL])


def click_apply(context, page):
    """Clicks the CodingJobBoard Apply control; returns the page showing the ATS."""
    target = page.get_by_role("link", name=APPLY_NAME_RE).or_(page.get_by_role("button", name=APPLY_NAME_RE)).first
    target.wait_for(state="visible", timeout=10000)
    before = set(context.pages)
    target.click()
    for _ in range(20):
        new_pages = [p for p in context.pages if p not in before]
        if new_pages:
            ats = new_pages[-1]
            ats.wait_for_load_state("domcontentloaded", timeout=30000)
            return ats
        page.wait_for_timeout(400)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    return page


def try_simplify(ats) -> str:
    """Returns 'clicked', 'unsupported', or 'not_found'."""
    for _ in range(15):
        for frame in ats.frames:
            try:
                if frame.get_by_text(SIMPLIFY_UNSUPPORTED_RE).first.is_visible(timeout=200):
                    return "unsupported"
                btn = frame.get_by_role("button", name=SIMPLIFY_AUTOFILL_RE).first
                if btn.is_visible(timeout=200):
                    btn.click()
                    return "clicked"
            except Exception:
                continue
        ats.wait_for_timeout(1000)
    return "not_found"


def stage_cv(cv: Path) -> Path:
    """Copies the CV to a temp dir as UPLOAD_NAME so the employer never sees the on-disk name."""
    staged = Path(tempfile.mkdtemp(prefix="autoapply_cv_")) / UPLOAD_NAME
    shutil.copyfile(cv, staged)
    return staged


def attach_cv(ats, cv: Path) -> bool:
    for frame in ats.frames:
        try:
            idx = frame.evaluate(RESUME_INPUT_JS)
            if idx >= 0:
                frame.locator("input[type=file]").nth(idx).set_input_files(str(cv))
                return True
        except Exception:
            continue
    return False


def empty_required_fields(ats) -> list:
    found = []
    for frame in ats.frames:
        try:
            found += frame.evaluate(EMPTY_REQUIRED_JS)
        except Exception:
            continue
    return list(dict.fromkeys(found))


def ask_outcome(job: dict, host: str):
    print("\nWhat happened?  [y] submitted   [n] needs manual review   [c] job closed   [s] skip (leave pending)")
    while True:
        choice = input("> ").strip().lower()
        if choice == "y":
            record(job, "applied", f"via Simplify (Playwright, human-submitted) on {host}")
            return
        if choice == "n":
            reason = input("Reason: ").strip() or "flagged during Playwright apply"
            record(job, "needs_manual_review", f"{reason} ({host})")
            return
        if choice == "c":
            record(job, "closed", f"closed ({host})")
            return
        if choice == "s":
            print("Left pending, nothing logged.")
            return
        print("Type y, n, c, or s.")


def apply_one(context, job: dict, cv: Path):
    print(f"\n=== {job['id']} — {job.get('companyName')} — {job.get('title')}")
    page = context.new_page()
    page.goto(job["link"], timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if CLOSED_RE.search(page.inner_text("body")):
        print("Page says this job is closed.")
        ask_outcome(job, urlparse(page.url).hostname or "codingjobboard.com")
        page.close()
        return

    try:
        ats = click_apply(context, page)
    except Exception as e:
        print(f"Couldn't find/click the Apply button ({e}). Open the application yourself in the window.")
        ats = page
    host = urlparse(ats.url).hostname or ats.url
    print(f"ATS: {ats.url}")

    simplify = try_simplify(ats)
    if simplify == "clicked":
        print("Clicked Simplify's autofill — giving it a few seconds…")
        ats.wait_for_timeout(8000)
    elif simplify == "unsupported":
        print("Simplify says autofill isn't supported on this site — fill it by hand or skip it.")
    else:
        print("Couldn't find Simplify's autofill button. Open the teal Simplify tab on the right edge")
        print("of the page and click 'Autofill This Page' yourself (or check Simplify is signed in).")

    if attach_cv(ats, cv):
        print(f"Attached your CV as '{UPLOAD_NAME}'.")
        ats.wait_for_timeout(3000)
    else:
        print(f"Couldn't find a resume upload field — attach {cv} yourself if the form needs one.")

    missing = empty_required_fields(ats)
    if missing:
        print("Required fields still empty (main page + iframes):")
        for label in missing:
            print(f"  - {label}")
    else:
        print("No empty required fields detected (custom widgets may not be detected — check anyway).")

    print("\nReview the form in the browser, fill anything missing from profile.md, and submit it yourself.")
    input("Press Enter here once you've submitted (or decided not to)… ")
    ask_outcome(job, host)
    for p in {page, ats}:
        try:
            p.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="Open Chrome to install + sign in to Simplify")
    parser.add_argument("--job-id")
    parser.add_argument("--limit", type=int, default=1, help="How many pending jobs to go through (default 1)")
    parser.add_argument("--cv", type=Path, default=Path(os.environ.get("AUTOAPPLY_CV", DEFAULT_CV)),
                        help=f"CV to attach (default: $AUTOAPPLY_CV or {DEFAULT_CV})")
    args = parser.parse_args()

    if args.setup:
        setup()
        return
    if not PROFILE_DIR.exists():
        raise SystemExit("No browser profile yet — run: python3 scripts/apply_playwright.py --setup")
    cv_path = args.cv.expanduser()
    if not cv_path.is_file():
        raise SystemExit(f"CV not found: {cv_path} (pass --cv or set AUTOAPPLY_CV)")
    cv = stage_cv(cv_path)

    jobs = load_jobs()
    if args.job_id:
        todo = [select_job(jobs, args.job_id)]
        if args.job_id in load_log():
            print(f"Note: {args.job_id} already has a logged status; continuing because you asked for it.")
    else:
        todo = pending(jobs, load_log())[: args.limit]
    if not todo:
        print("Nothing pending.")
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            executable_path=CHROME_PATH,
            headless=False,
            no_viewport=True,
            # Playwright disables extensions by default; Simplify has to load.
            ignore_default_args=["--disable-extensions", "--enable-automation"],
        )
        try:
            for job in todo:
                apply_one(context, job, cv)
        finally:
            context.close()


if __name__ == "__main__":
    main()
