# Roadmap

Ground rules: LLM/API calls are free — never trade quality for call volume.
No security work. The only metric is how good the system is. Priorities:
**(1) full, correct, structured job data; (2) find every job;** everything
else serves those two.

## Phase 1 — Perfect job data

- **1.1 Canonical description cleaning.** One `clean(text)` in a new
  `app/services/descriptions.py`, used by every save path: double-unescape
  (Greenhouse ships `&lt;div&gt;`), strip HTML to readable text keeping
  bullets, reject block-page junk as no-description. Backfill the ~50k
  stored HTML-soup rows. Fix adapter bugs: himalayas company="name",
  hnhiring company/location swap, remoteok block pages, dice empty
  descriptions, wellfound off.
- **1.2 Enrichment engine** (`app/services/enrichment.py` + task, scheduled
  and tail-called after each fetch). Ladder per thin job: ATS API by URL
  pattern → JSON-LD JobPosting → LLM extraction from raw page text →
  browser task for blocked hosts. Rescued jobs (filtered for
  no_description/few_skills) return to `new` and re-match automatically.
  Politeness limits only, no budgets.
- **1.3 Kill tracker chains.** appcast/recruitics/jobvite click URLs are
  interstitials: follow through, never store as apply_url, hand landing
  HTML to enrichment. Re-resolve the stored tracker apply_urls and drain
  the 79k backlog.
- **1.4 Structured job details.** New jobs columns: salary_min/max/currency,
  employment_type, required_years, required_skills[], nice_to_have[],
  education_required, benefits_note, language. One LLM `job_details` call
  per new/enriched job fills them; shown in UI and fed to the matcher.
- **1.5 LinkedIn done properly.** Details for every title-passing job
  (prefilter first, then fetch); backfill 8.8k description-less rows via
  browser tasks; harvest toggle ON (user action); capture Voyager extras
  (salary, applicant counts) into the structured columns.

## Phase 2 — Find every job

- **2.1 New ATS adapters**, yield order: iCIMS, BambooHR, Teamtailor,
  Jobvite, Personio. Each compounds via sniffer auto-discovery.
- **2.2 New sources**: USAJOBS, YC Work at a Startup, hiring.cafe; fix or
  kill jsearch (403 for 20 runs); decide careerjet/findwork keys.
- **2.3 Board hygiene + expansion**: blocklist junk slugs (linkedin,
  appcast, stepstone, justjoin…), validate sniffed boards before
  activation, seed from public Greenhouse/Lever slug lists on GitHub.
- **2.4 Split the monolithic fetch cycle** into per-source-group tasks on
  their own schedules; fast sources fetch more often, slow tiers stop
  gating them.
- **2.5 Extension harvest beyond LinkedIn**: Indeed, Glassdoor, Workday
  response interception.

## Phase 3 — Matching intelligence

- **3.1 Match on full data**: structured details in the prompt, full
  description (no 4k excerpt), seniority from extracted required_years
  instead of title words alone.
- **3.2 Two-pass scoring**: fast model scores all; strong model re-scores
  the 55–85 band where decisions flip.
- **3.3 Auto re-match on description growth** (the stamp re-queues scoring,
  not just the docs nudge).
- **3.4 Match-quality regression harness**: ~50 hand-labeled jobs, every
  model/prompt change scored against them via the compare machinery.

## Phase 4 — Documents that improve themselves

- **4.1 Auto-regenerate docs** when a scored application's description got
  meaningfully fuller (versions kept; stale-docs badge becomes trigger).
- **4.2 Self-review loop**: generate → critique against the JD → revise.
- **4.3 Overlay superpowers**: resume/cover-letter upload into file inputs,
  screening-question answer bank + <select> support, one-click mark-applied.

## Phase 5 — Observability parity

- **5.1 Enrichment runs panel** (built WITH 1.2): attempted / method /
  chars gained / re-matched / failures per host.
- **5.2 agent_events + /api/agent/report**: durable harvest/autofill/task
  history, browser-agent panel, options-page event log, fix blank
  resolve_link ingest outcomes, per-agent last-seen, browser_tasks pruning.
- **5.3 Funnel dashboard**: fetched → filter reasons → matched → docs over
  time; per-source new-jobs-per-1k-requests; score distributions per model.

## Phase 6 — Housekeeping

Nightly pg_dump off-box · archive filtered_out rows older than ~60 days ·
language filter for non-English postings.

**Build order:** 1.1 → 1.3 → 1.2+5.1 → 1.4 → 1.5 → 2.x rolling → 3.x → 4.x,
with 5.2/5.3 and Phase 6 slotted between larger items.

## Done so far

- Code-review bug fixes (13), diagnostic SQL reports (scripts/).
- Expanded-query title matching; cross-post duplicate application guard;
  "Not interested" menu (company/title-word blocking, user-chosen);
  dead-posting liveness sweep with badges; stale-docs nudge.
