#!/usr/bin/env python3
"""Shared job-log primitives for the CodingJobBoard auto-apply flow.

Trimmed subset of the full pipeline's apply_to_job.py that this standalone package actually
needs: no Obsidian vault, no LinkedIn/Playwright apply logic, no CV tailoring (this flow relies
on Simplify's own stored resume, never a per-job tailored CV).
"""
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_FILE = ROOT / "found_jobs.json"
LOG_FILE = ROOT / "applied_log.json"
DEFAULT_MAX_AGE_DAYS = 30
_DATE_LINE_RE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$", re.M)
# \bjava\b does not match "JavaScript"; bare "spring" is too often the season.
_JAVA_RE = re.compile(r"\bjava\b|\bspring (boot|framework|cloud)\b", re.I)
_OTHER_LANG_RE = re.compile(
    r"\b(go|golang|python|php|ruby|node\.?js|typescript|c#|\.net|rust|elixir|scala)\b|c\+\+", re.I
)
_OTHER_STACK_TITLE_RE = re.compile(
    r"\b(go|golang|python|django|php|laravel|ruby|rails|node\.?js|\.net|c#|rust|elixir|"
    r"react|frontend|front-end|ios|android|flutter|data engineer|devops|sre|gtm|sales|marketing)\b|c\+\+",
    re.I,
)


def is_java_role(job: dict) -> bool:
    """The candidate only knows Java. A title naming Java qualifies; a title naming another
    stack doesn't; a neutral title ("Backend Engineer") needs Java to be the posting's main
    language — mentioned at least as often as all other languages combined, so an
    "or Python/Java/Node" alternatives list doesn't count."""
    title = job.get("title") or ""
    if _JAVA_RE.search(title):
        return True
    if _OTHER_STACK_TITLE_RE.search(title):
        return False
    desc = job.get("descriptionText") or ""
    java = len(_JAVA_RE.findall(desc))
    return java > 0 and java >= len(_OTHER_LANG_RE.findall(desc))


def posting_age_days(job: dict) -> int | None:
    """Days since the posting went up, or None if unknown.

    Uses "postedAt" (ISO date, set by fetch_jobs.py); leads saved before that field existed fall
    back to the MM/DD/YYYY header line that is often captured at the top of descriptionText.
    """
    posted = None
    if job.get("postedAt"):
        try:
            posted = date.fromisoformat(job["postedAt"])
        except ValueError:
            pass
    if posted is None:
        m = _DATE_LINE_RE.search((job.get("descriptionText") or "")[:1500])
        if m:
            try:
                posted = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
    return (date.today() - posted).days if posted else None


def load_jobs(jobs_file: Path = JOBS_FILE) -> list:
    if not jobs_file.exists():
        return []
    return json.loads(jobs_file.read_text(encoding="utf-8"))


def save_jobs(jobs: list, jobs_file: Path = JOBS_FILE) -> None:
    jobs_file.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


def select_job(jobs: list, job_id: str) -> dict:
    for job in jobs:
        if job.get("id") == job_id:
            return job
    raise SystemExit(f"No job found with id={job_id}")


def load_log(log_file: Path = LOG_FILE) -> dict:
    if log_file.exists():
        return json.loads(log_file.read_text(encoding="utf-8"))
    return {}


def save_log(log: dict, log_file: Path = LOG_FILE) -> None:
    log_file.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record(job: dict, status: str, note: str = "", log_file: Path = LOG_FILE) -> None:
    log = load_log(log_file)
    log[job["id"]] = {
        "title": job.get("title"),
        "company": job.get("companyName"),
        "link": job.get("link"),
        "status": status,
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    save_log(log, log_file)
    print(f"Logged: {job['id']} -> {status}" + (f" ({note})" if note else ""))
