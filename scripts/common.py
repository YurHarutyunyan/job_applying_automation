#!/usr/bin/env python3
"""Shared job-log primitives for the CodingJobBoard auto-apply flow.

Trimmed subset of the full pipeline's apply_to_job.py that this standalone package actually
needs: no Obsidian vault, no LinkedIn/Playwright apply logic, no CV tailoring (this flow relies
on Simplify's own stored resume, never a per-job tailored CV).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_FILE = ROOT / "found_jobs.json"
LOG_FILE = ROOT / "applied_log.json"


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
