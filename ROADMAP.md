# Roadmap

This file is written so that someone who has never seen this codebase can pick
any item, understand why it exists, and build it. Read the primer first; every
task below names the files involved, what to change, and how to check it worked.

**Ground rules for all work here:**
- LLM/API calls are free for us — never trade quality for call volume. Use the
  model generously (extraction, re-scoring, critique passes).
- No security work needed (single user, private deployment).
- The only metric is how good the system is. Priorities, in order:
  **(1) full, correct, structured job data; (2) find every job.**
  Everything else serves those two.

---

## Primer: how the system works today

The app is a self-hosted job pipeline: **FastAPI** web UI + **Celery** workers
+ **Postgres** + **Redis**, deployed with docker compose. A Chrome extension
("the agent") runs browser-side work the server can't do.

The pipeline, end to end:

1. **Fetch** (`app/tasks/fetch.py` → `app/services/job_fetcher.py`): every 5
   hours, ~25 source adapters in `app/services/sources/` pull job postings
   (aggregator APIs like Adzuna/Jooble, ATS boards like Greenhouse/Lever/Ashby,
   scrapers like Dice). Jobs are deduplicated (`app/services/deduplication.py`,
   a hash of normalized company+title+location) and stored in the `jobs` table.
2. **Match** (`app/tasks/match.py` → `app/services/matcher.py`): each `new` job
   goes through cheap keyword filters first (title vs target roles, location,
   excluded companies, min skill mentions — see `evaluate_keyword_filter`),
   then an LLM scores fit 0–100. Below the user's minimum → `filtered_out`
   with a stored `filter_reason`; above → `matched` + an `applications` row.
3. **Generate** (`app/tasks/generate.py` → `app/services/doc_generator.py`):
   matched jobs get a tailored resume + cover letter (6 LLM calls: JD insights,
   content selection, bullets, summary, cover letter, retry) compiled to PDF
   via LaTeX.
4. **The user applies by hand**, tracked in `/apps`. Outreach, interview-notes,
   and mailbox subsystems exist but are secondary.

Supporting cast:
- **The board registry** (`app/services/company_boards.py`, `company_boards`
  table): every company ATS board we've learned about (configured, seeded, or
  auto-discovered from job links), ranked by yield. The fetch cycle polls these.
- **The agent queue** (`app/services/browser_tasks.py`, `/api/agent/*` in
  `app/routers/agent.py`): server queues tasks (`resolve_link`, `fetch_json`);
  the Chrome extension long-polls, runs them in a real browser (residential
  IP + user's logins), posts results back. Used for hosts that block servers.
- **The LLM layer** (`app/llm/providers.py`): multi-provider failover chains
  (FreeInference → Anthropic → Gemini → NVIDIA NIM). Every call is logged with
  its full prompt+reply in the `llm_calls` table (`app/services/llm_log.py`).
- **Observability**: `/runs` shows fetch-cycle history (`fetch_runs` table),
  a system panel, and LLM logs at `/llm`. The pattern to copy for anything new:
  a per-run history table + a panel that reads it.
- **Profile** (`app/models/profile.py`): a single row whose JSONB `data` blob
  holds the user's roles/skills/experience AND various caches (expanded search
  queries, discovered boards, settings overrides via `app/services/tunables.py`).

**What the data audit found (Aug 2026, 151k jobs):** the numbers that justify
Phase 1. Adzuna = half the database and its API truncates every description at
exactly 500 chars; LinkedIn jobs are 90% description-less; a third of all
descriptions are raw/double-escaped HTML; "resolved" apply links often stop at
click-tracker URLs (click.appcast.io) instead of the real ATS page; 79k
aggregator links were never resolved at all. ~25k jobs were auto-rejected for
"too few skills"/"no description" — i.e. for having thin data, not for being
bad jobs.

Run `scripts/db_report.sql` and `scripts/db_samples.sql` against the live DB
(commands in each file's header) to reproduce these numbers at any time.

---

## Phase 1 — Perfect job data

### 1.1 Canonical description cleaning
**Why:** ~50k stored descriptions are HTML or double-escaped HTML
(`&lt;div&gt;...`). The skill filter greps this soup and the doc generator
quotes it. Some "descriptions" are literally Cloudflare block pages.

**Build:**
- New `app/services/descriptions.py` with one function `clean(text) -> str`:
  1. `html.unescape()` twice (Greenhouse escapes its HTML before sending).
  2. Strip tags but keep structure: `<li>` → "- ", `<p>`/`<br>` → newlines.
     Python's `html.parser.HTMLParser` is enough; no new dependency needed.
  3. Collapse runs of blank lines/spaces.
  4. Return `""` for junk: if the text matches block-page markers
     ("verify you are human", "enable javascript and cookies", "cloudflare",
     "security check") treat it as *no description at all*.
- Call `clean()` in every path that writes `Job.description`:
  `job_fetcher.fetch_and_save_jobs`, `harvest.save_harvested_jobs`,
  `deduplication.merge_or_skip`, and the future enrichment writes (1.2).
- One-time backfill: a Celery task that walks all jobs where the description
  looks HTML-ish (`description ~ '<(p|div|ul|li|br)[ >/]' OR description ~
  '&(amp|lt|gt|nbsp);'` — see section D of `scripts/db_samples.sql`), cleans
  in place, commits in batches of ~500. Do NOT stamp `description_updated_at`
  (cleaning reformats; it doesn't add information, so it must not trigger the
  "regenerate your docs" nudge).
- Adapter bug fixes (all visible in `scripts/db_samples.sql` output):
  - `sources/himalayas.py` stores the literal string `"name"` as every
    company — the parser reads a key name instead of its value. Fix the field
    lookup.
  - `sources/hnhiring.py` puts the location text into the company field
    ("New York, NY (In-Office)" as company). HN posts are
    `Company | Role | Location |...` pipe-separated; fix the split order.
  - `sources/remoteok.py` stores Cloudflare block pages as descriptions —
    `clean()` fixes storage, but also skip such jobs entirely.
  - `sources/dice.py` returns 0-length descriptions and no dates for ~97% of
    jobs. Fix its detail-page extraction, or disable it (set enabled=False
    like Indeed) if Dice now requires a browser.
  - `sources/wellfound.py`: 140 jobs, zero descriptions, ever. Default
    `WELLFOUND_ENABLED=false`.

**Verify:** re-run `scripts/db_report.sql` section D → `html_ish_total` should
drop to ~0 after backfill. New fetches store plain text. Test suite has
`tests/` per-adapter fixtures to extend.

### 1.2 The enrichment engine (the centerpiece)
**Why:** the #1 finding. Adzuna (77k jobs) truncates at 500 chars; LinkedIn is
90% empty; Jooble ships ~300-char teasers. Meanwhile every job row carries a
`url`/`apply_url` where the FULL posting lives. Nothing ever goes and gets it.

**Build:**
- New `app/services/enrichment.py` + `app/tasks/enrich.py` (+ beat entry in
  `app/celery_app.py`, plus a tail-call at the end of each fetch cycle so new
  jobs are enriched within minutes, before matching runs).
- Target selection: jobs whose description is missing or shorter than ~1,500
  chars, newest first, **title-passing first** (don't spend requests on jobs
  the title gate would reject anyway — reuse
  `matcher._title_matches_roles`). Include previously filtered jobs whose
  `filter_reason` is `no_description`/`few_skills`.
- For each target, try in order (stop at first success):
  1. **ATS API by URL pattern.** If `url`/`apply_url` matches a known ATS,
     the clean JD is one JSON call away — no scraping. Patterns and endpoints:
     - `boards.greenhouse.io/<slug>/jobs/<id>` or `job-boards.greenhouse.io/…`
       → `GET boards-api.greenhouse.io/v1/boards/<slug>/jobs/<id>` (field
       `content`, HTML-escaped — run through `clean()`).
     - `jobs.lever.co/<slug>/<id>` → `api.lever.co/v0/postings/<slug>/<id>`.
     - `jobs.ashbyhq.com/<slug>/<uuid>` → Ashby's public posting API.
     - `jobs.smartrecruiters.com/<Company>/<id>` →
       `api.smartrecruiters.com/v1/companies/<company>/postings/<id>`.
     - `apply.workable.com/j/<code>` → Workable's public widget API.
     - `<tenant>.wd<N>.myworkdayjobs.com/...` → the CXS JSON endpoint the
       page itself calls (see `sources/workday.py`, it already speaks it).
  2. **JSON-LD.** Fetch the page (httpx, browser-like User-Agent — copy
     `_HEADERS` from `app/services/link_resolver.py`), look for
     `<script type="application/ld+json">` with `"@type": "JobPosting"`.
     Gives description, datePosted, salary, employmentType, location in one
     parse. (The extension's `overlay.js readPosting()` already does exactly
     this client-side — port that logic.)
  3. **LLM extraction.** Calls are free: strip the page's HTML to text, send
     to the LLM with "extract the job description and metadata as JSON
     {description, salary, employment_type, ...}; return null if this page
     is not a job posting". Use `app/llm/providers.generation_chat` and wrap
     in `llm_log.stage("enrich_extract")` so every call is logged.
  4. **Browser task fallback** for hosts that block servers (LinkedIn, Dice,
     tracker chains): `browser_tasks.enqueue(db, "resolve_link", {...})` and
     extend `app/services/agent_work._ingest_resolve_link` to run steps 2–3
     on the HTML the extension returns, then update the job.
- On success: `job.description = clean(new_text)`; stamp
  `description_updated_at` (already exists — added Aug 2026) when it grew
  meaningfully; fill any structured fields learned (see 1.4); and if the job
  was `filtered_out` with reason `no_description`/`few_skills`, set
  `status='new'`, clear `filter_reason`/`filter_detail` → the next matching
  pass re-scores it automatically.
- Record every attempt's outcome for the panel in 5.1. No budgets — only
  politeness: cap concurrent requests per host (~4), small delay between
  requests to the same host. The backlog drains over days; that's fine.

**Verify:** `scripts/db_report.sql` section 2 — Adzuna `full%` (≥500 chars)
climbs from 99%-of-500-char-stubs toward real multi-KB descriptions
(watch `avg_len`); LinkedIn `desc%` climbs from 9.8%. Section 4:
`no_description`/`few_skills` counts shrink as rescued jobs re-match.

### 1.3 Kill the tracker chains
**Why:** "resolved" Adzuna links mostly stop at `click.appcast.io/...` — a
redirect tracker, not the employer. It got stored as the "real apply URL"
because the resolver follows HTTP redirects only, and appcast redirects via
JavaScript. Also: 79,705 aggregator links were never resolved at all (budget
was 400/cycle), and the landing-page HTML the resolver downloads is thrown
away after mining board slugs — enrichment (1.2) wants it.

**Build (all in `app/services/link_resolver.py`):**
- Add tracker hosts to `_INTERSTITIAL_PATTERNS` (so they get followed
  through) AND to `_AGGREGATOR_DOMAINS` (so they're never stored as
  `apply_url`): `click.appcast.io`, `appcast.io`, `jsv3.recruitics.com`,
  `recruitics.com`, `click.jobvite.com`, plus whatever section C of
  `scripts/db_samples.sql` shows accumulating.
- Trackers redirect with JS/meta-refresh; `_redirect_from_html` already
  handles that — the miss was purely that appcast wasn't in the pattern list.
- One-time cleanup task: find jobs whose `apply_url` matches a tracker
  domain, null it, re-queue those URLs for resolution.
- Raise `LINK_RESOLVE_MAX_PER_CYCLE` (config) substantially — politeness per
  host is the only limit.
- Return landing HTML to the caller keyed by job URL (already in
  `ResolveStats.landing_html`) and feed it to enrichment instead of dropping
  it after slug-mining.

**Verify:** section C of `db_samples.sql` empties; section 11 of
`db_report.sql` (`still_unresolved`) trends to ~0 over a week; resolved
`apply_url`s are myworkdayjobs/greenhouse/company domains, not appcast.

### 1.4 Structured job details
**Why:** "skills and any other details" as first-class data. Today salary,
required experience, employment type etc. live buried in description prose;
the matcher re-derives them from text every time, and the UI can't show them.

**Build:**
- Migration adding to `jobs`: `salary_min`, `salary_max`, `salary_currency`,
  `employment_type` (full_time/part_time/contract/internship),
  `required_years` (float), `required_skills` (ARRAY), `nice_to_have_skills`
  (ARRAY), `education_required`, `benefits_note`, `language` (ISO code of the
  posting's language). All nullable. (Migrations live in `alembic/versions/`,
  next number, follow 0020's format.)
- One LLM call per new/enriched job — stage `job_details` — with the full
  cleaned description: "return JSON with exactly these fields; null anything
  the posting doesn't state; never guess numbers." Parse defensively (copy
  `matcher._extract_json_object`). Run it right after a job passes the
  keyword filter (so no calls wasted on title-rejects) and re-run when
  enrichment meaningfully grows a description.
- Show on the job card (`app/templates/jobs/partials/job_card.html`) — salary
  pill, employment-type pill, "asks N yrs" pill — and on the app detail page.
- Feed the matcher (see 3.1) and the jobs page filters (salary floor filter).

**Verify:** new jobs carry filled columns (spot-check via psql); a "details
coverage" line fits naturally into `scripts/db_report.sql` section 2.

### 1.5 LinkedIn done properly
**Why:** LinkedIn's guest API returns 10-job pages with no descriptions; a
separate per-job detail call fetches each description. A budget
(`LINKEDIN_MAX_DETAIL_FETCHES=200`) rationed those calls and was spent before
title filtering — most of it on jobs that die at the title gate anyway.
8,800 stored LinkedIn jobs have no description.

**Build:**
- In `app/services/sources/linkedin.py`: apply the title prefilter (import
  `matcher._title_matches_roles` + expanded queries) BEFORE detail fetches;
  then fetch details for **every** surviving job. Raise the cap to a
  politeness ceiling only; raise `LINKEDIN_MAX_PAGES`.
- Backfill: enrichment (1.2) queues browser tasks for the stored 8.8k
  description-less LinkedIn jobs (server IPs get blocked; the extension's
  browser doesn't).
- Harvest (passive capture of LinkedIn's own API responses while the user
  browses — `extension/interceptor.js` → `/api/agent/harvest` →
  `app/services/harvest.py`): **the user must enable the toggle in the
  extension options** — it's built and currently produces 0. Then extend
  `harvest.py`'s alias lists to also capture salary and applicant-count
  fields from Voyager payloads into the 1.4 columns.

**Verify:** `db_report.sql` section 2, linkedin row: `desc%` 9.8 → 90+.
Section 12 (harvest yield) goes nonzero once the toggle is on.

---

## Phase 2 — Find every job

### 2.1 New ATS adapters
**Why:** each ATS we speak = every company hosted on it becomes reachable, and
the board-discovery flywheel (`ats_discovery.py` + sniffer) starts finding
boards for it automatically in job links and career pages.

**Build** — in yield order: **iCIMS** (huge enterprise footprint),
**BambooHR** (`<company>.bamboohr.com/careers` has a JSON API),
**Teamtailor** (public JSON API), **Jobvite**, **Personio**. For each:
- New `app/services/sources/<ats>.py` copying the shape of
  `sources/recruitee.py` (slug-list-driven, returns list-of-dicts with
  url/title/company/location/description/posted_at).
- Config: `<ATS>_COMPANY_SLUGS` in `app/config.py` + `.env.example`.
- Register in `job_fetcher._run_all_adapters`, in `ats_discovery.py`'s
  URL-pattern table (so boards auto-discover), and in
  `runs.py TRIGGERABLE_SOURCES`.
- Tests: copy an existing adapter's test file, fixture a captured API reply.

### 2.2 New sources
- **USAJOBS** — official API, free key, structured JSON.
- **Y Combinator Work at a Startup** — public job feed.
- **hiring.cafe** — aggregates ATS boards with full descriptions.
- **jsearch is dead**: 403 on every run for 20 runs (expired RapidAPI key).
  User refreshes the key, or remove the adapter.
- careerjet/findwork sit disabled awaiting keys — user decision.

### 2.3 Board discovery hygiene + expansion
**Why:** the registry holds gems (auto-discovered `ionq`: 5,460 jobs) next to
junk (`greenhouse/linkedin`, `greenhouse/appcast`, `greenhouse/stepstone` —
slugs "discovered" from non-company pages, some attached to wrong companies).

**Build (in `app/services/company_boards.py` / `ats_discovery.py`):**
- A slug blocklist (linkedin, appcast, stepstone, justjoin, indeed, glassdoor,
  jobs, careers, www...) checked at `record_boards` time.
- Validation before activation: fetch the board once; if the ATS API 404s or
  returns a company name wildly different from the claimed one, store as
  inactive with a note instead of polling it every cycle.
- More seeds: public GitHub lists of known Greenhouse/Lever slugs (the
  SLUG_HARVEST_URLS mechanism in config already parses such lists — add more).

### 2.4 Split the monolithic fetch cycle
**Why:** one 47-minute task fetches everything; fast API sources wait on the
browser tier, and postings arrive hours later than they could.

**Build:** split `fetch_jobs` into group tasks — `fetch_api_sources` (Adzuna,
Jooble, feeds — cheap, run every 1–2h), `fetch_ats_boards` (the registry
poll, every 4–6h), `fetch_browser_tier` (Playwright, own schedule). Each
takes its own lock (`fetch_lock.py` supports per-key locks), writes its own
`fetch_runs` row (add a `group` column), and tail-calls matching. Keep a
combined "run everything" entry point for the manual trigger UI.

### 2.5 Extension harvest beyond LinkedIn
**Why:** the shape-based extractor (`harvest.py`) is deliberately generic —
it finds anything with title+company+id in intercepted JSON. Only the
interceptor's host registration limits it to LinkedIn.

**Build:** in `extension/background.js`, add per-host harvest registrations
(Indeed, Glassdoor, `*.myworkdayjobs.com`) each behind its own permission
toggle in `options.html`; add those hosts' field names to the alias lists in
`harvest.py`. Server side needs nothing else — `/api/agent/harvest` is
host-agnostic.

---

## Phase 3 — Matching intelligence

### 3.1 Match on full data
- Feed the matcher prompt (`matcher._build_match_prompt`) the structured
  fields from 1.4 (salary, required_years, required_skills) as explicit
  lines, and raise the description excerpt from 4,000 chars to the full text.
- Replace the title-word seniority block (`_blocked_by_seniority`) for jobs
  that have `required_years`: a "Senior" title asking 3 years should pass a
  2.4-year candidate to the LLM instead of being auto-dropped.

### 3.2 Two-pass scoring
- Pass 1 (exists): fast model scores everything.
- Pass 2 (new): jobs landing in the 55–85 band get re-scored by the strongest
  configured model (the chain in `llm/providers.py` already knows which);
  store both scores (`llm_score`, new `llm_score_deep`), display the deep one
  when present. The band is where accept/reject actually flips.

### 3.3 Auto re-match on description growth
When enrichment stamps `description_updated_at` on an already-scored job,
also reset it to `new` (unless the user manually overrode its status) so the
sweep re-scores it with the real description.

### 3.4 Match-quality regression harness
Hand-label ~50 jobs (good fit / bad fit) into a fixture table or JSON file;
a task scores them with the current model+prompt and reports agreement.
Run it after any prompt or model change — the model-compare machinery
(`app/services/model_compare.py`) is 80% of this already.

---

## Phase 4 — Documents that improve themselves

### 4.1 Auto-regenerate docs on enrichment
The stale-docs badge exists (apps UI shows "docs predate a fuller
description"). Upgrade it from nudge to action: a scheduled task finds
applications whose current docs predate `description_updated_at`, and
re-queues `generate_docs` for them (versions are kept, so nothing is lost).
Skip applications the user already edited/sent (status beyond `not_applied`).

### 4.2 Self-review loop
In `doc_generator.generate_documents`, after composing the resume context and
cover letter: one critique call ("as a recruiter for THIS job description,
list concrete weaknesses: missing keywords, vague bullets, generic phrasing")
and one revision call applying the critique. Two extra free calls per
document; log stages `doc_critique`/`doc_revise`.

### 4.3 Overlay superpowers (extension)
- **Resume upload into forms**: content scripts CAN set file inputs — fetch
  the current resume PDF via the background worker (it holds the token),
  build a `File` + `DataTransfer`, assign to `input.files`, dispatch
  `change`. Add a "Attach resume" button next to "Fill this form" in
  `extension/overlay.js`.
- **Answer bank**: profile gains a `screening_answers` section (work
  authorization, needs sponsorship?, start date, salary expectation, how did
  you hear about us). Autofill's `FIELD_RULES` learns those patterns and —
  new — handles `<select>` dropdowns (currently only input/textarea).
- **Mark applied from the overlay**: one button calling the existing
  `/apps/{id}/status` endpoint via a new agent-API proxy route.

---

## Phase 5 — Observability parity

*The house pattern (copy it): a history table + a panel on `/runs` that reads
it, plus per-run log lines. See `fetch_runs`/`fetch_history.py` and
`llm_calls`/`llm_log.py` for the two reference implementations.*

### 5.1 Enrichment runs panel — build together WITH 1.2, not after
Table `enrichment_runs` (or columns on `fetch_runs`): attempted, per-method
success counts (ats_api/json_ld/llm/browser), chars gained, jobs re-queued
for matching, failures by host. Panel on `/runs` in the fetch-history style.

### 5.2 Agent events (extension observability)
**Why:** harvest posts, autofill outcomes, and overlay lookups leave no
durable trace; completed browser tasks show blank ingest outcomes on the
panel (bug: `agent_work._ingest_resolve_link` writes a note the panel can't
see, or fails silently — investigate while here); `browser_tasks` rows are
never pruned.
**Build:** `agent_events` table (kind, host, summary JSONB, created_at) +
pruning like `llm_log.prune`; new `POST /api/agent/report` for client-side
events; extension keeps a ring buffer of its last ~50 events in
`chrome.storage` shown on the options page; a "Browser agent" panel showing
harvest yield over time, task success/escalation rates per kind, per-agent
last-seen (today the `agent` blob on the profile only remembers the LAST
agent that polled — store a per-agent map instead).

### 5.3 Funnel dashboard
One page: fetched → filter reasons → matched → docs over time (the data
already exists across `jobs`/`fetch_runs`); per-source
"new jobs per 1k requests" ROI; score distributions per model.

---

## Phase 6 — Housekeeping

- **Backups** (do early, it's an evening): nightly `pg_dump` in a cron
  container or beat task, shipped off-box (B2/S3/private repo). Everything
  else on this list assumes the data survives.
- Archive `filtered_out` jobs older than ~60 days (table is 147k rows and
  growing; keep matched/docs rows forever).
- Language filter: non-English postings (German Arbeitnow jobs show up in
  samples) waste matcher calls — use the 1.4 `language` field to skip or
  down-rank them.

---

## Build order

1.1 → 1.3 → 1.2+5.1 (together) → 1.4 → 1.5 → then Phase 2 rolling, Phase 3,
Phase 4, with 5.2/5.3 and Phase 6 slotted between larger items.

Rationale: matching, documents, and coverage ROI all multiply off clean full
descriptions, so data quality goes first; coverage second; intelligence third
once it has real data to be intelligent about.

## Done so far (Aug 2026)

- Code-review pass: 13 bug fixes (session-poisoning batch losses, profile
  blob lost-updates, lock ownership, partial-commit-on-timeout, etc.).
- Diagnostic SQL reports: `scripts/db_report.sql` (aggregates),
  `scripts/db_samples.sql` (row-level extraction quality).
- Expanded-query title matching (title gate accepts LLM-expanded role
  variants, not just raw target roles).
- Cross-post duplicate guard (near-identical company+title with an existing
  application → filtered as `duplicate`, no second doc generation).
- "Not interested" menu on job cards — user explicitly picks: hide job /
  exclude company / block a chosen title word; choices feed future matching.
- Dead-posting detection (`app/services/liveness.py`): scheduled sweep marks
  closed postings (404/410, explicit closed-banner, ATS redirect-to-board);
  badges in jobs + apps UI. Conservative: ambiguity never closes a job.
- Stale-docs nudge: `description_updated_at` stamp + "docs predate a fuller
  description" notice next to the Rewrite button.
