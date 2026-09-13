# CodingJobBoard Auto-Apply

A self-contained pipeline that finds remote software-engineering leads on
[CodingJobBoard](https://www.codingjobboard.com), then applies to them for you by driving a real
Chrome browser: opening each job's off-site ATS application (Greenhouse/Lever/Workday/etc.),
triggering the [Simplify](https://simplify.jobs) browser extension's autofill, filling in
whatever Simplify leaves blank from your own `profile.md`, and clicking the final Apply/Submit
button.

## Read this before running it

**This flow submits real applications with no human "yes."** There is no review step, no
confirmation prompt — once it decides a job's form is complete, it clicks Submit itself. This is
a deliberate design choice (it was extracted from a larger pipeline where it's the one flow built
to work this way), not a default you should expect from automation in general. Only run it if
you're comfortable with that.

It is conservative on purpose: if Simplify doesn't support the ATS, a required field can't be
filled without guessing something not in `profile.md`, a CAPTCHA appears, or the job wants you to
create a third-party account, it stops and logs `needs_manual_review` instead of guessing its way
to a submit — that job's browser tab is left open for you to finish by hand.

## Prerequisites

1. **[Claude Code](https://claude.com/claude-code)** installed and authenticated (`claude` on your
   PATH). This pipeline is run *as* Claude Code, not as a standalone script — the "applying" step
   is an agent (`.claude/agents/codingjobboard-apply-agent.md`) that drives the browser via tool
   calls, not a fixed script. The fetch step also shells out to headless `claude -p` for
   citizenship/work-authorization screening (see step 2 below), so `claude` needs to work
   non-interactively too, not just inside an interactive session.
2. **Google Chrome**, plus the **Claude in Chrome** browser extension — install it from the
   Chrome Web Store if you don't have it, enable it, and pair it to your Claude Code install (run
   `/chrome` inside Claude Code to pair). Its connection can also go idle and need re-pairing even
   once installed — if `/apply-codingjobboard` reports no connected browser tabs, open `/chrome`
   again and use "Reconnect extension." See [Troubleshooting](#troubleshooting) for both cases.
3. **[Simplify's Chrome extension](https://simplify.jobs/)** — install it from the Chrome Web
   Store if you don't have it, enable it, sign in, and fill in your resume/profile on Simplify's
   own site. This flow relies entirely on Simplify's stored resume/autofill data — it never
   generates or uploads a tailored CV itself, so nothing here works without Simplify actually
   signed in and populated.
4. **Python 3.10+**, with:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
   (only needed for the lead-fetching step, `scripts/fetch_jobs.py` — the apply step itself has
   no Python browser dependency, it drives Chrome through Claude Code's own browser tools.)

## Setup

```bash
cp profile.md.example profile.md
```

Fill in `profile.md` with your own real facts — name, contact info, work authorization, skills,
experience, education. This is the **only** source of truth the apply agent is allowed to invent
facts from when Simplify leaves a field blank; never let it fill in something that isn't here.

The **Work Authorization** line specifically is also required before you can run the fetch step —
`fetch_jobs.py` reads it to screen every posting's citizenship requirement (see step 2 below) and
exits with an error if `profile.md` doesn't exist yet. Be specific here (e.g. "Armenian citizen,
operate via a US LLC for C2C/1099 contracts, no US citizenship or work visa" rather than just
"authorized to work remotely") — the more precisely this states your actual situation, the better
Claude can judge whether a posting's requirement is compatible.

## Usage guide

### 1. Confirm both extensions are installed, enabled, and connected

Before running anything, check both extensions in Chrome's toolbar — this is the step that
silently breaks most often (see [Troubleshooting](#troubleshooting) for what to do in each case):

- **Claude in Chrome** — if it's missing from the toolbar entirely, it isn't installed; install
  and enable it, then pair it via `/chrome` in Claude Code. If it's there, click it and confirm
  it shows "Connected" (not just "Enabled" — the icon can say enabled while the underlying
  connection is dead).
- **Simplify** — if it's missing from the toolbar, install and enable it, then sign in and fill in
  your resume on Simplify's own site. If it's there, click it on any job page and confirm you're
  actually signed in with your resume showing (an installed-but-signed-out Simplify looks the
  same as a working one at a glance).

### 2. Pull fresh leads

```bash
python3 scripts/fetch_jobs.py
```

This opens CodingJobBoard's remote-jobs listing in a headless Chrome, pages through it, and for
every posting that mentions a backend-relevant stack keyword, screens its citizenship/work-
authorization requirement against your `profile.md`'s "Work Authorization" line by calling
headless Claude (`claude -p`) — the same subprocess pattern the full pipeline's `tailor_cv.py`
uses. Only postings Claude judges compatible ("passed") are appended to `found_jobs.json`;
postings that explicitly require a citizenship/authorization you don't have ("excluded"), or that
don't say either way ("ambiguous"), are printed with the reason but never saved — since the apply
step later submits with no human review, nothing reaches it that hasn't cleared this check.
Expect output like:

```
Distinct listings found: 143
new: Acme Corp — Senior Backend Engineer
already logged, skipped: Globex — Platform Engineer (codingjobboard-15421)
excluded (citizenship): Initech — Backend Engineer — requires US citizenship, candidate is not a US citizen
ambiguous (citizenship), excluding conservatively: Foo Inc — Platform Engineer — posting doesn't mention work authorization
...
Summary: 6 new, 1 updated, 120 unchanged, 16 already logged, 3 excluded (citizenship), 1 ambiguous (excluded conservatively)
found_jobs.json updated (147 total leads).
```

This step requires `profile.md` to exist (see [Setup](#setup)) — it exits with an error before
touching the browser if it's missing, since it can't screen citizenship without your "Work
Authorization" line. It also requires the `claude` CLI on PATH and authenticated, same as any
other headless `claude -p` call in this repo.

Run with `--dry-run` first if you just want to see what it *would* add without writing anything
(the citizenship screening calls still run under `--dry-run`, so the preview is accurate):
```bash
python3 scripts/fetch_jobs.py --dry-run
```

Nothing here applies to a job or opens a tab you'd need to review — this step only writes JSON
(and makes read-only `claude -p` screening calls).

### 3. (Optional) Skim what got staged

```bash
python3 -c "
import json
jobs = json.load(open('found_jobs.json'))
log = json.load(open('applied_log.json'))
pending = [j for j in jobs if j['id'] not in log]
for j in pending:
    print(j['id'], '-', j['companyName'], '-', j['title'])
print(f'{len(pending)} pending')
"
```

This is just a sanity check — the apply agent does its own filtering, you don't have to prune
anything by hand. Skip this step entirely if you trust the fetcher's stack-keyword filter.

### 4. Apply

From inside Claude Code, in this directory:

```
/apply-codingjobboard
```

This launches `codingjobboard-apply-agent`, which processes every pending job one at a time:
navigates to it, follows its Apply button off-site, triggers Simplify, fills any gaps from
`profile.md`, and either submits (logging `applied`) or stops and logs `needs_manual_review` with
a tab left open for you. **Re-read the warning at the top of this file before your first run** —
successful applications submit with no confirmation step.

To limit a run to one specific lead instead of everything pending:
```
/apply-codingjobboard codingjobboard-14176
```

The agent prints a final summary: which jobs it auto-applied to (and closed the tab for), and
which ones it flagged `needs_manual_review` (tab left open) with the specific reason each one
stopped.

### 5. Handle anything flagged `needs_manual_review`

Each such job has its browser tab left open on purpose. Go through them one at a time, finish
(or abandon) the application yourself, then log the outcome so it isn't re-surfaced next run:

```bash
python3 scripts/log_apply.py --job-id codingjobboard-14176 --status applied --note "finished manually — Simplify didn't support Workday"
```

Valid `--status` values: `applied`, `needs_manual_review`, `closed`, `skipped_no_cv`.

### 6. Repeat

Re-run step 2 whenever you want new leads (daily is reasonable — CodingJobBoard's listing changes
gradually) and step 4 whenever you want to work through whatever's pending. Both steps are
idempotent: a job already in `applied_log.json` is never re-fetched-into-relevance or re-applied
to.

## How state is tracked

Two local JSON files, no external vault or database:

- **`found_jobs.json`** — every lead that passed `fetch_jobs.py`'s citizenship screen, keyed by id
  (`codingjobboard-<numeric-id>`). Each entry carries a `fitVerdict: {status: "passed", reason}`
  field recording why it was kept. Re-running the fetcher updates a job in place if its posting
  text changed (re-screening it in the process), and leaves everything else alone. Postings
  screened out (`excluded`/`ambiguous`) never appear here at all — they're only ever printed to
  the console at fetch time.
- **`applied_log.json`** — one entry per job once the apply agent has processed it: `applied`,
  `needs_manual_review`, or `closed` (job no longer accepting applications), each with a
  timestamp and a note. A job with any entry here is treated as already handled and skipped by
  both the fetcher and the apply agent.

Nothing here is git-tracked by default (see `.gitignore`) — they're your personal, local state.

## Troubleshooting

**The Claude in Chrome extension isn't installed at all.**
Install it from the Chrome Web Store (search "Claude in Chrome" or get the link from
`claude.com/claude-code`'s browser-extension docs), then pin it to the toolbar and pair it to
your local Claude Code install — running `/chrome` inside Claude Code will walk you through
pairing on first use. Without this extension installed and paired, `/apply-codingjobboard` has no
way to open a browser at all and will fail immediately with no connected tabs.

**The Simplify extension isn't installed at all.**
Install it from the Chrome Web Store (search "Simplify Jobs" or go to
[simplify.jobs](https://simplify.jobs/) and follow their "Add to Chrome" link), then sign in and
fill in your resume/profile on Simplify's own site before running anything here — this flow has
no fallback autofill of its own, so a job's ATS page will just look unfilled if Simplify isn't
present, and the agent will correctly flag it `needs_manual_review` rather than try to fake its
way through the form. Check its toolbar icon shows you're signed in, not just that the icon
exists (an installed-but-signed-out Simplify looks identical to a working one at a glance).

**`/apply-codingjobboard` reports no connected browser tabs / can't find Chrome.**
The Claude in Chrome extension's background connection dies silently on its own — this is a known
Chrome limitation (idle extensions get shut down after ~30s), not a bug in this repo. Fix: run
Claude Code's `/chrome` command and choose "Reconnect extension," then try again. The toolbar
icon can say "enabled" the whole time this is happening, so don't trust it — check for an actual
open, responsive tab.

**Simplify never appears on an ATS page.**
Either the ATS isn't one Simplify supports, or the extension isn't signed in. The agent already
handles this itself — it logs `needs_manual_review` with a note explaining why and leaves the tab
open rather than guessing. Open Simplify's toolbar icon on that tab to check which case it is.

**`fetch_jobs.py` fails with a Chrome executable error.**
`CHROME_PATH` in `scripts/fetch_jobs.py` is hardcoded to `/usr/bin/google-chrome` (standard on
Linux). On macOS, change it to something like
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`; on Windows, to your
`chrome.exe` path. Alternatively, drop the `executable_path=CHROME_PATH` argument entirely to let
Playwright use its own bundled Chromium (run `playwright install chromium` first if you do).

**`fetch_jobs.py` fails with `ModuleNotFoundError: No module named 'playwright'`.**
Run `pip install -r requirements.txt && playwright install chromium` (see
[Prerequisites](#prerequisites)) — this is only needed for the fetch step, not the apply step.

**A job keeps getting flagged `needs_manual_review` for the same reason every run.**
That's expected once you've logged it — any job id present in `applied_log.json` (regardless of
status) is treated as already handled and won't be re-processed by `/apply-codingjobboard` or
re-touched by `fetch_jobs.py`. If you want another attempt at it, delete its entry from
`applied_log.json` first.

## Files

```
profile.md.example          # copy to profile.md and fill in your own facts
scripts/
  common.py                  # shared load/save/record helpers for found_jobs.json + applied_log.json
  fetch_jobs.py               # scrapes CodingJobBoard's remote listings into found_jobs.json
  log_apply.py                # records one job's outcome into applied_log.json
.claude/
  agents/codingjobboard-apply-agent.md   # the agent that actually drives the browser and applies
  commands/apply-codingjobboard.md       # /apply-codingjobboard — launches the agent above
```
# job_applying_automation
