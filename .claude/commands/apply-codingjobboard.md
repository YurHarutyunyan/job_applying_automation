---
description: Run codingjobboard-apply-agent to apply to staged CodingJobBoard leads via Simplify autofill
argument-hint: [optional: a specific job id — otherwise processes every staged CodingJobBoard lead]
---

Launch the `codingjobboard-apply-agent` subagent (defined in
`.claude/agents/codingjobboard-apply-agent.md`) to apply to CodingJobBoard-sourced job leads.

**Read this before running it**: this agent submits applications with no human confirmation
step — it drives the browser to open each job's off-site ATS page, trigger Simplify's autofill,
fill in whatever Simplify misses using `profile.md`, and click the final Apply/Submit button
itself. That is a deliberate design choice for this flow, not something to assume elsewhere.

It works off leads already staged in `found_jobs.json` by `scripts/fetch_jobs.py` — it doesn't
search or filter jobs itself, and it doesn't tailor a CV (Simplify uses its own stored resume).

$ARGUMENTS

If a job id was given above, apply to just that one job. Otherwise, process every job in
`found_jobs.json` whose id starts with `codingjobboard-` and doesn't already have a logged status
in `applied_log.json`.
