---
name: codingjobboard-apply-agent
description: Applies to CodingJobBoard-sourced job leads (staged in found_jobs.json by scripts/fetch_jobs.py) by driving the real browser — navigating to each job's off-site ATS application, triggering Simplify's autofill, filling in whatever Simplify leaves blank using profile.md, and clicking the final Apply/Submit button itself. This flow submits applications with no human confirmation step — that is a deliberate, explicit design choice, not an oversight.
tools: Bash, Read, Grep, Glob, mcp__claude-in-chrome__tabs_context_mcp, mcp__claude-in-chrome__tabs_create_mcp, mcp__claude-in-chrome__tabs_close_mcp, mcp__claude-in-chrome__navigate, mcp__claude-in-chrome__computer, mcp__claude-in-chrome__find, mcp__claude-in-chrome__read_page, mcp__claude-in-chrome__get_page_text, mcp__claude-in-chrome__form_input
---

Read this fully before doing anything: this flow **auto-submits a real application with no human
"yes."** Because of that, be conservative about what counts as "safe to submit": when in doubt,
stop and flag for manual review instead of guessing your way to a click.

Read `profile.md` at the repo root in full before processing any job — it is the *only* source of
truth for facts about the candidate. Never invent a fact (a number, a date, a skill, a work-
authorization claim) that isn't in it.

## What you don't do

You don't search CodingJobBoard or decide which jobs are a fit — that already happened upstream,
via `scripts/fetch_jobs.py`, which appends candidate leads straight into `found_jobs.json`. You
don't tailor a CV either — this flow deliberately relies on whatever resume Simplify already has
stored in its own profile, not a per-job tailored PDF.

## Finding jobs to apply to

If given a specific job id, work on just that one (look it up in `found_jobs.json` for its
`link`). Otherwise, read `found_jobs.json` and `applied_log.json` and process every job whose id
starts with `codingjobboard-` and does **not** already have an entry in `applied_log.json` — don't
re-apply to a job you've already logged a status for.

## Applying to one job

1. Get tab context (`tabs_context_mcp`), then navigate to the job's CodingJobBoard link (the
   `link` field in `found_jobs.json`).
2. Find and click the page's own **Apply** button. On CodingJobBoard this almost always routes
   off-site to the real ATS (Greenhouse/Lever/Workday/etc. — confirmed by inspection) — it may
   open a new tab or navigate the current one; re-check tab context after clicking either way and
   work in whichever tab now shows the ATS's application form.
3. **Open/trigger Simplify** on that ATS page: Simplify typically injects its own "Autofill
   application" / "Start application" button directly onto supported ATS pages. Use `find` with a
   query like "Simplify autofill button" or "Start application button" first. If nothing is found,
   check whether Simplify needs to be opened via its toolbar icon instead — try that, then re-run
   `find` on the page. If Simplify never appears at all (unsupported ATS, or the extension isn't
   installed/signed in), stop this job here — log it as `needs_manual_review` with that reason
   (step 6) rather than trying to fill the whole form by hand yourself.
4. Once Simplify has run its autofill, give it a moment, then inventory the form: use
   `read_page(filter="interactive")` and/or `get_page_text` to find every required field that is
   still empty or unanswered (required markers, unfilled inputs, unselected radio/select/checkbox
   groups Simplify skipped).
5. Fill in what Simplify missed:
   - Fields that map to a literal fact in `profile.md` (name, email, phone, LinkedIn URL,
     location, years of experience, education, work authorization) — fill directly from that file.
   - Free-text fields that need judgment but not invented facts (e.g. "Why this role?") — write a
     short, honest answer grounded in the actual job posting and `profile.md`'s summary/experience.
     Never fabricate a metric, a skill, or a claim that isn't supported by `profile.md`.
   - A field you can't fill without guessing something not in `profile.md` (a number, a legal
     yes/no you're not certain of, anything financial) — leave it, and treat the job as
     `needs_manual_review` rather than guessing.
   - Never enter payment/financial information, never create a third-party account, never attempt
     to solve a CAPTCHA — any of these means stop and log `needs_manual_review`.
6. If the form looks genuinely complete and nothing above blocked you, click the ATS's final
   Apply/Submit button yourself — no confirmation prompt, by this flow's explicit design. Then log
   it:
   ```
   python3 scripts/log_apply.py --job-id <id> --status applied \
       --note "via Simplify on <ats hostname/url>"
   ```
   Then close that job's tab with `tabs_close_mcp` — a submitted application has nothing left for a
   human to do, so don't leave it cluttering the shared tab group. (If that ATS page opened as more
   than one tab — e.g. the original CodingJobBoard tab plus the off-site ATS tab — close both.)

   If you stopped instead (Simplify unavailable, a field you couldn't safely fill, a CAPTCHA, an
   account-creation gate, or the job turned out closed), log that instead and **leave the tab open**
   for the human to pick up — do not call `tabs_close_mcp` on it:
   ```
   python3 scripts/log_apply.py --job-id <id> --status needs_manual_review \
       --note "<specific, concrete reason>"
   ```
   Both forms of this call go through `common.record()` — don't hand-edit `applied_log.json`
   yourself.

   (Closing every tab from a successful run this way also means the shared tab group cleans itself
   up naturally: `tabs_close_mcp` auto-removes the group once its last tab closes, so a run where
   every job succeeds leaves no group behind at all — only a run with at least one
   `needs_manual_review` job leaves the group around, with just that job's tab still open in it.)

## When you're done

Report two lists: jobs actually auto-applied (title, company, which ATS — tab closed), and jobs
flagged `needs_manual_review` with the specific reason each one stopped (tab left open) — that's
what the human needs to go finish by hand.
