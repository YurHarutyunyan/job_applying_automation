---
description: Run codingjobboard-apply-agent to apply to staged leads (CodingJobBoard, Greenhouse, Lever, Ashby) via Simplify autofill
argument-hint: [optional: fill-only] [optional: a specific job id — otherwise processes up to 10 staged leads]
---

Launch the `codingjobboard-apply-agent` subagent (defined in
`.claude/agents/codingjobboard-apply-agent.md`) to apply to staged job leads.

If the arguments below include `fill-only`, tell every agent you launch to run in **fill-only
mode**: fill each form but never click the final Submit, log `ready_to_submit`, and leave the tab
open for the human.

**Read this before running it**: this agent submits applications with no human confirmation
step — it drives the browser to open each job's off-site ATS page, trigger Simplify's autofill,
fill in whatever Simplify misses using `profile.md`, and click the final Apply/Submit button
itself. That is a deliberate design choice for this flow, not something to assume elsewhere.

It works off leads already staged in `found_jobs.json` by `scripts/fetch_jobs.py` and
`scripts/fetch_ats_jobs.py` — it doesn't
search or filter jobs itself, and it doesn't tailor a CV (Simplify uses its own stored resume).

$ARGUMENTS

Do **not** read `found_jobs.json` or `applied_log.json` yourself — `found_jobs.json` is hundreds
of KB of job descriptions. Get the work list with:

```
python3 scripts/pending_jobs.py --ids-only
```

(capped at 10, already excludes logged jobs and anything not screened "passed").

If a job id was given above, launch the agent once for just that job. Otherwise launch the agent
**once per job id from that list, one after another** — a fresh agent per job, passing it the
single job id — rather than one agent for the whole list. One long-lived agent would carry every
previous job's page dumps in its context and re-send them on every call. After all jobs are done,
combine the agents' reports into the lists the agent describes (auto-applied and
`needs_manual_review` with reasons, or in fill-only mode the jobs left ready to submit).
