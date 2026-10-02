#!/usr/bin/env python3
"""Tailor a CV to one staged job and stage a copy ready to upload as "Yuri's CV.pdf".

Thin wrapper around the full pipeline's scripts/tailor_cv.py (one directory up), pointed at this
package's found_jobs.json and profile.md, so the honesty constraints live in one place: the
tailoring prompt may only re-word and re-order facts that are in profile.md, never add new ones.

  1. Writes cv/tailored/<company>_<title>_<id>.md and .pdf (the internal, tracking filename).
  2. Copies the PDF to cv/upload/<id>/Yuri's CV.pdf — an employer must never see the internal
     filename — and prints that absolute path on the last line for the apply agent to upload.

An existing tailored PDF for the job is reused unless --regenerate. Override the tailoring script
with TAILOR_CV_SCRIPT=/path/to/tailor_cv.py.

Usage:
    python3 scripts/tailor_cv_for_job.py --job-id lever-binance-6fff9a51-...
    python3 scripts/tailor_cv_for_job.py --job-id <id> --regenerate
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import JOBS_FILE, ROOT, load_jobs, select_job  # noqa: E402

TAILOR_SCRIPT = Path(os.environ.get("TAILOR_CV_SCRIPT", ROOT.parent / "scripts" / "tailor_cv.py"))
TAILORED_DIR = ROOT / "cv" / "tailored"
UPLOAD_DIR = ROOT / "cv" / "upload"
UPLOAD_NAME = "Yuri's CV.pdf"


def tailored_pdf(job_id: str) -> Path | None:
    matches = sorted(TAILORED_DIR.glob(f"*_{job_id}.pdf"))
    return matches[-1] if matches else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--regenerate", action="store_true", help="Tailor again even if a PDF already exists")
    args = parser.parse_args()

    select_job(load_jobs(), args.job_id)  # fail early on an unknown id
    pdf = None if args.regenerate else tailored_pdf(args.job_id)
    if pdf:
        print(f"Reusing tailored CV: {pdf}")
    else:
        if not TAILOR_SCRIPT.exists():
            raise SystemExit(f"Tailoring script not found: {TAILOR_SCRIPT} (set TAILOR_CV_SCRIPT)")
        TAILORED_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(TAILOR_SCRIPT), "--jobs-file", str(JOBS_FILE),
                        "--profile", str(ROOT / "profile.md"), "--output-dir", str(TAILORED_DIR),
                        "--job-id", args.job_id, "--pdf"], check=True)
        pdf = tailored_pdf(args.job_id)
        if not pdf:
            raise SystemExit(f"Tailoring finished but no PDF for {args.job_id} in {TAILORED_DIR}")

    upload = UPLOAD_DIR / args.job_id / UPLOAD_NAME
    upload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf, upload)
    print(upload.resolve())


if __name__ == "__main__":
    main()
