# Job source volume audit — 2026-09-22

## Scope and handoff

Only increase useful job intake from existing sources, especially broken or zero-yield sources. User requested code review and database diagnostic commands before implementation. Do not change production behavior yet. Update this document as findings are established so a session interruption does not lose the investigation.

Status: production reports received; two targeted intake batches implemented and locally verified. Original audit checkout: `514fdf6`; implementation starts from `e679515`. Production deployment and measurement remain pending. See Git history for the implementation commit and the checkpoints below for current evidence; the original findings table records the original issues and must be read together with these checkpoints.

## Checkpoint: review fixes (2026-09-22, later)

Addressed in code on `claude/gallant-ptolemy-epbp0s`, with tests; not yet measured in production:

- **S02** JSearch window is a tunable (default `3days`), as is pages per search.
- **S05** Errors from a source that still returned jobs are kept; the source is recorded as partial. Warnings are stored separately.
- **S06** Scheduled group runs rest failing sources (a `manual` flag replaces reading `only` as manual); the browser tier checks resting too.
- **S07** The re-probe clock counts, per source, rests since its last real call, instead of `count(fetch_runs) % N`.
- **S13** Celery no longer re-delivers multi-hour fetches: fetch tasks opt out of late acks and the visibility timeout is 2h.
- Per-source durations are recorded on each run (settings page "Time" column, run log) — the input needed to reduce the ~4h board runtime.
- Also outside this audit's list: US cities sharing foreign names were rejected by the location filter; blank-company postings collapsed in dedupe; board jobs are now filed under the registry's company name; Greenhouse/Lever/Ashby honour the settings-page max age.

Still open: S08 (other sources' paging), S09 (uniform rate-limit handling), S10/S11 (commit-accurate counters, persisted drop counts), S12 (requisition-aware dedupe), JSearch 403 (account-side).

## Evidence already established

### Production baseline received (2026-09-22 15:26 UTC)

Reports completed successfully at schema 0043, host checkout e679515. Container image revision is not established merely by host Git HEAD. Inputs: `source-intake-db.txt`, `source-intake-settings.txt`, `source-intake-version.txt` (user-provided; do not commit raw reports).

- **First implementation priority: board coverage.** 2,680 active boards have never been polled: Greenhouse 1,102, Workday 1,126, SmartRecruiters 190, Ashby 132, BambooHR 130. Use bounded rotation within existing request caps; avoid simply multiplying requests on overloaded workers.
- API: 91 retained runs, average 11,065.9 seconds; boards: 107 runs, average 14,883 seconds. All exceeded the 1,800-second lock TTL. Recent API run intervals overlap their recorded durations. Board poll timestamps reach Sep 22 although latest completed board history starts Sep 20; do not conclude scheduling stopped from completed-history timestamps alone.
- JSearch failed all 91 attempts with 403. A present key does not establish subscription/quota validity; paging changes cannot repair this.
- HiringCafe server failed all 91 attempts (405 historically, now 403), but browser recipe intake inserted 386 jobs in 7 days. Preserve that working path.
- iCIMS, Jobvite, Teamtailor failed all 107 attempts; error messages show listing HTML without JobPosting JSON-LD. Jobvite has 0 active boards out of 76; Teamtailor 0/19. Their validators assume the same structured-data format as their parsers, so a parser gap can incorrectly reject a real board. Fixing selection alone must not be represented as repairing these adapters.
- YC failed all 91 attempts despite returning substantial HTML. Requires actual HTML/embedded-data investigation, not more polling.
- Built In and Jobspresso have only 3 observed empty runs, insufficient to establish a long-standing outage.
- Careerjet, Findwork, USAJOBS lack credentials. Server Handshake has no cookie, but browser Handshake inserted 1,442 jobs/7d. LinkedIn browser hosts include empty payloads while a separate null-host intake inserted 644; don't treat all LinkedIn intake as dead. LinkedIn browsing is explicitly paused in runtime settings; preserve that setting.
- API sources currently producing include Adzuna and LinkedIn. Preserve matching criteria; this task remains source intake only despite many stored rows being filtered.

First implementation checkpoint: 116 focused checks passed. Follow-up batch and expanded checks are recorded below. No deployment performed. Production reports supersede provisional priorities below.

Implemented locally:

- Reserve one quarter of each capped ATS selection for never/oldest-polled active boards. Remaining slots retain yield ranking; configured boards still take priority. Caps stay unchanged. Workday's 30-board cap now has 7 rotating slots instead of seeds occupying its entire budget.
- Treat a present registry (even an empty one) as authoritative; do not append retired/rejected legacy slugs or prepend seeds already imported into the registry.
- Renew owned fetch-group locks every minute during a cycle. Renewal compares the ownership token; stopping/crashing the process still leaves the existing TTL fallback. Redis outages or process suspension can still lose ownership; this is logged, not a claim of perfect exclusion during outages.
- Added observed non-JSON-LD formats for YC's `data-page` jobPostings and Jobvite/Teamtailor listing cards. Same-host posting URLs are required; missing descriptions/dates remain missing for enrichment to resolve.
- Updated Jobvite/Teamtailor validation to recognize the same card formats. Old exact JSON-LD-only rejections are re-probed once within the existing validation batch limit. New rejections use a distinct reason; no migration or blanket reactivation is needed.

Public evidence read Sep 22: [YC role page](https://www.ycombinator.com/jobs/role/software-engineer), [Virtasant Teamtailor](https://virtasant.teamtailor.com/jobs), [Tyler Technologies Jobvite](https://jobs.jobvite.com/tylertech/search). All three returned HTTP 200 to a plain HTTP client; saved HTML was replayed locally. Teamtailor/Jobvite had posting links, and YC had embedded listing data. Raw HTML is in ignored `.venv-test/evidence/`, not committed. This proves parser behavior against these responses, not access from the production server or coverage of every tenant.

Still pending after both batches: JSearch account/access diagnosis, HiringCafe server endpoint (retain working browser intake), source cooldown/retry defects, partial-error telemetry, other source pagination and deeper board coverage, DB commit accounting, runtime reduction, and false-dedupe sample analysis. Do not claim these are repaired.

Verification completed:

- 94 checks across the new intake regression module and existing ATS discovery, validation, and adapter tests, plus 22 existing fetch lock/task/group/state checks. Tests imported actual application modules with mocked provider requests; board selection/revalidation tests used an isolated SQLite board table. They ran from copies in ignored `.venv-test/checks` to avoid the repository's globally required PostgreSQL fixture. No full PostgreSQL integration suite or real Redis renewal test was run; Redis interactions were mocked.
- Captured public HTML replay through `jobs_from_listing()` recovered 41 distinct YC posting URLs, 16 Teamtailor URLs, and 50 Jobvite URLs. Both corrected ATS probes accepted their observed listing cards. These are extracted listings, not guaranteed new/relevant production inserts.
- `git diff --check` passed. Test packages are confined to ignored `.venv-test`; raw user reports remain untracked.

After a normal deployment (no new migration):

1. Trigger a boards fetch and targeted runs for YC, Built In, Jobspresso, Himalayas and The Muse using the existing Runs page. The next registry validation automatically rechecks old Jobvite/Teamtailor/iCIMS format rejections within its cap.
2. Re-run `scripts/db_source_intake.sql` after completed runs. Expect previously unpolled boards to acquire `last_fetched_at`; restored sources should show nonzero fetched counts when accessible. Judge improvement by inserted jobs and relevance, not fetched totals alone.
3. Verify a long fetch still holds its group lease after 30 minutes and releases it at completion. Runtime reduction remains outstanding; renewal prevents expiry-driven overlap, not slow work itself.
4. JSearch requires checking access/subscription/quota in the provider account and then a source-only retry. Do not paste API keys into the conversation. Missing optional provider credentials are setup gaps, not parser bugs.

- Fetch adapters run in `api`, `boards`, and `browser` groups (`app/services/job_fetcher.py`). Sources outside a run's group are recorded as disabled; do not treat these rows as actual source outages.
- `fetch_runs` and `fetch_source_runs` preserve fetched/inserted/merged/skipped/stale counts and errors. Only 200 runs are retained globally (`app/services/fetch_history.py`); a requested 30-day window may contain much less history.
- Browser-agent intake has separate evidence in `agent_events`, `browser_tasks`, and `harvest_samples`. Source health must include both intake paths.
- Existing general-purpose SQL reports cover many unrelated matching/enrichment features. Prepare a narrower source-volume report with actual coverage, group-aware source summaries, errors, board coverage, and browser yield.

## Next steps

1. [x] Review scheduler, source adapters, board selection/retirement, browser intake, and deduplication.
2. [x] Append confirmed behaviors and separately labeled impact hypotheses with code references.
3. [x] Save read-only diagnostic SQL and exact production commands; check model/migration references.
4. [x] Read the user's reports and rank fixes by recoverable new jobs.
5. [x] Complete the first targeted patch and focused regression checks; record deployment verification steps and remaining work.
6. [ ] Deploy and measure new-job yield; continue source-specific fixes listed above using the new evidence.

## Findings and proposed improvements

Priorities are provisional until production data arrives. "Confirmed" means the behavior is visible in this checkout; it does not mean its production impact has been measured. The target is additional distinct, relevant openings, not inflated fetched/duplicate counts.

| ID | Priority | Finding and evidence | Improvement | Data needed |
| --- | --- | --- | --- | --- |
| S01 | High | **Confirmed coverage gap.** Workday always sends `offset: 0`, a page size of 20, and only `queries[:5]`; up to 20 detail requests are separate from listing coverage. `app/services/sources/workday.py:23`, `:90`. | Add bounded pagination using response totals, rotate queries when the budget is exhausted, and separate listing coverage from description budget. | Workday yield, actual queries, board counts, and later a selected response's total/result count. |
| S02 | High | **Confirmed narrow search.** JSearch uses `date_posted: today` and defaults to one page; the fetcher passes no override. `app/services/sources/jsearch.py:20`, `:37`; `app/services/job_fetcher.py:266`. Healthy credentials can still yield zero or miss jobs after downtime. | Configurable overlapping date window and bounded pagination, balanced against the user's API quota. | Credentials-present flag, errors, new-job yield, recent queries. Do not assume the key works just because it exists. |
| S03 | High | **Confirmed selection bias.** `board_slugs()` orders by latest count, cumulative returned count, recent sighting, then slug; it has no oldest-polled rotation. `build_ats_slugs()` puts configured and seed slugs ahead of registry rows under a fixed cap. `app/services/company_boards.py:165`; `app/services/ats_discovery.py:302`. If capacity is full, productive incumbents can permanently exclude untried boards. Repeated duplicates also increase cumulative board yield. | Reserve capacity for new/overdue boards, rank on useful recent contribution, track actual polls. Retain explicit configured priorities. | Active-but-never-polled and stale polling counts, per-ATS caps, examples in report sections 7–8. |
| S04 | High | **Confirmed registry bypass.** Even with registry results present, `build_ats_slugs()` appends legacy `discovered` slugs without checking active/validated/rejected state. `app/services/ats_discovery.py:327`. When capacity remains, rejected or retired legacy entries can consume requests again. | Use the registry as the authority when enabled; keep legacy fallback only when the registry is unavailable. Validate deliberate configured/seed exceptions explicitly. | Inactive board reasons and legacy/registry membership comparison if needed. |
| S05 | High | **Confirmed hidden partial failures.** `merge_into_stats()` discards captured warnings/errors whenever a source returned any jobs. Most adapters log errors and return partial/empty lists rather than raise. Thus one working board/page can hide failures on the others. `app/services/source_diagnostics.py:61`. | Persist partial failures with per-request/per-board outcomes; distinguish warning from failed listing request and zero matches. | Source errors plus board coverage. Existing reports cannot reconstruct warnings already discarded; targeted worker logs may be needed next. |
| S06 | Medium | **Confirmed cooldown bypass.** Scheduled groups populate `only`, but `_skip()` applies resting only when `only is None`. Scheduled API/board runs therefore act like explicit manual bypasses. Browser branches do not call `_skip()` for resting at all. `app/services/job_fetcher.py:233`, `:558`, `:873`. | Carry an explicit manual-override flag separately from group membership; apply appropriate cooldowns to scheduled paths. | Repeated 401/403/429 failures and attempt frequency. This saves requests; it does not repair invalid credentials. |
| S07 | Medium | **Confirmed retry clock defect.** `resting_sources()` uses retained `count(fetch_runs) % retry_every`; pruning keeps the count at 200. At default retry interval 10 the probe condition stays true once retention fills. `app/services/fetch_history.py:27`, `:130`, `:227`. This is separate from S06. | Per-source next-probe timestamps or another monotonic retry clock unaffected by retention. | Retained run count and settings. |
| S08 | Medium | **Confirmed first-response limits.** Jooble and Careerjet make one listing request; Findwork does not follow a next-page link; hiring.cafe requests page 0/100; Himalayas requests limit 100 without offset and repeats that feed for each role. `app/services/sources/jooble.py:13`, `careerjet.py:31`, `findwork.py:13`, `hiringcafe.py:46`, `himalayas.py:71`. | For sources that actually work, verify current response contracts and add bounded paging/cursors; fetch a shared feed once per cycle. Do not increase limits blindly. | Source yield/errors first; then page totals or sanitized selected response shapes. Endpoint support has not been live-verified. |
| S09 | Medium | **Confirmed uneven rate-limit handling.** JSearch uses `SourceUnavailable` to stop the source; Adzuna/Jooble/Careerjet/Findwork swallow HTTP errors, and callers continue remaining role/location combinations. `app/services/sources/adzuna.py:60`, `jooble.py:24`, `careerjet.py`, `findwork.py:24`; fetcher loops. | Consistent 401/402/403/429 classification, per-source request budgets, bounded retry/backoff and Retry-After support. | Repeated HTTP failures, quota availability, and request counts if logs provide them. |
| S10 | High if observed | **Confirmed data-loss/reporting risk.** Chunk/final commit exceptions roll back jobs but leave inserted/merged counters incremented; a later history write can claim jobs that never committed. Top-level adapter errors return zero counts before history is recorded; task exceptions also return zero-shaped success data. `app/services/job_fetcher.py:1001`, `:1259`, `:1296`, `:1326`; `app/tasks/fetch.py:96`. All adapters finish before job inserts start, so worker interruption during fetching loses the in-memory results so far. | Record run start and failure durably, account only committed batches, checkpoint completed sources, surface failed tasks. | Duration/gaps and unaccounted counts; if suspicious, redacted worker errors. Absence from fetch history cannot prove no task ran. |
| S11 | Medium | **Confirmed telemetry omissions.** Dropped counts are computed but not stored in FetchRun/FetchSourceRun; `boards_polled` is actually active registry size, not selected/attempted boards. History is capped at 200 runs shared across groups. `app/services/fetch_history.py:78`, `:108`, `:125`; `app/models/fetch_run.py`. | Persist dropped outcomes/reasons and real attempted-board counts; retain time-based or per-group history. Label skipped/out-of-group separately from disabled/missing credentials. | Report coverage and fetched minus accounted outcomes. This difference is a clue, not proof of exactly how many rows were dropped. |
| S12 | High if observed | **Confirmed identity limitation; impact unmeasured.** Live and archive dedupe ultimately match normalized company/title/location without requisition identity or a time bound. A new distinct requisition with the same normalized triple merges into an existing row or is suppressed by an old tombstone, even with a different source ID/URL. `app/services/deduplication.py:108`, `:145`, `:185`; `app/models/job.py:78` makes the hash unique. | Investigate sample pairs first; design requisition-aware identity/repost rules before changing dedupe or archive policy. Preserve true cross-source deduplication. | High duplicate/archive share, then small public posting-ID/date examples. Aggregate reports cannot prove false deduplication, and raw discarded candidates are not retained. |
| S13 | Medium | **Confirmed lease-duration risk.** Fetch locks expire after 1,800 seconds and are not renewed; fetch tasks have no task-specific runtime limit here. A slow run can outlive its lock and overlap a later request, increasing duplicate requests and races. `app/services/fetch_lock.py:34`, `:54`; `app/tasks/fetch.py:63`. Group runs also share profile discovery/cache fields despite the fetcher comment claiming cycles are serialized. | Renew owned locks during bounded work; update shared profile fields atomically/under proper coordination. | Group durations over 1,800 seconds, worker concurrency, overlapping runs. No production overlap has been established. |
| S14 | Investigate | **Existing alternate paths must be diagnosed separately.** Indeed RSS is disabled by default with a code comment describing it as retired; browser-agent collection is separate. Handshake requires a session cookie on the server path. Browser hosts can be paused, rate-rested or challenge-backed-off; linked Tsenta sweeps record stop reasons separately. `app/config.py:473`; `app/services/browse_plan.py:346`, `:408`; `app/tasks/fetch.py:148`. | Identify offline agent vs expired login vs paused host vs extraction failure before touching selectors. Use successful authorized browser/linked intake where available. Do not simply re-enable a default-off source. | Browser events, task backlog, sample metadata, sweep stop reasons, worker configuration. No claim about current external endpoint behavior is made from comments alone. |

## Read-only data to collect now

Files prepared:

- `scripts/db_source_intake.sql`: 15 focused sections covering retained history, source attempts/yield/errors, search queries, board coverage, browser collection, linked sweeps, sample metadata, and stored jobs.
- `scripts/source_intake_settings.py`: worker settings allowlist, credential **presence booleans only**, configured board counts, and the profile's effective intake overrides. Does not call providers or modify the database.

On the Linux production host, from `/opt/jobapp` (the deployment path in this repository), place these two new files at those relative paths first. They currently exist only in the local workspace; no commit, push, pull, deployment, or container rebuild has been done. The commands stream them into existing containers, so no image rebuild is needed.

```bash
cd /opt/jobapp
git rev-parse --short HEAD > source-intake-version.txt
docker compose -f docker-compose.prod.yml ps >> source-intake-version.txt

docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -' \
  < scripts/db_source_intake.sql > source-intake-db.txt 2>&1

docker compose -f docker-compose.prod.yml exec -T worker python - \
  < scripts/source_intake_settings.py > source-intake-settings.txt 2>&1
```

Return `source-intake-db.txt`, `source-intake-settings.txt`, and `source-intake-version.txt`. The SQL uses a read-only transaction, a 45-second per-statement timeout and a 3-second lock timeout. If it stops on a missing table/column or timeout, return the partial output and error; do not run migrations just to satisfy this report. The file ends with a completion marker when everything ran.

No database dump, full profile, resume, raw browser payload, API key, cookie, or `.env` file is needed. URLs and common credential fields in source errors are redacted by the query; review free-text errors before sharing. Search terms and locations are intentionally included because they affect coverage. Runtime configuration needs the worker snapshot because DB rows alone cannot show missing environment credentials or disabled schedules.

## How to act on the reports

1. Partition sources into never attempted/missing configuration, authentication or rate failure, true empty search, partial coverage, repeated duplicates/stale results, and browser-only successes.
2. Prioritize sources with previously demonstrated yield that stopped, then healthy but capped sources and unpolled company boards.
3. Make one targeted change at a time; run that source using the existing manual source selection, with the user's normal matching preferences intact.
4. Compare new distinct jobs per attempted run/day, board coverage, failures, elapsed time, and request cost against a comparable baseline. Different groups and browser paths need separate denominators. A higher fetched count alone is not improvement.
5. If a parser is implicated, request only a small relevant sanitized sample or replay saved samples locally before changing extraction. Do not request all captured traffic up front.

## Original audit validation and limits (before reports arrived)

Schema references were checked against current SQLAlchemy models and migration 0032 for `harvest_samples`. No live database query execution or provider testing has occurred. The system Python lacks the project's SQLAlchemy/pytest/PostgreSQL driver dependencies, so this audit does not claim a full application test run or PostgreSQL execution validation.

Completed local checks:

- Python diagnostic compiles; every allowlisted setting exists in the current configuration model.
- SQL source catalog covers all 36 scheduled source identifiers, including currently disabled sources.
- Executed extracted functions with bounded in-memory inputs (imports/dependencies substituted, no network/DB): S04 appends a legacy slug excluded from active registry selection; S06 bypasses resting with a non-null source selection; S07 always permits probing at retained count 200/default interval 10; S05 discards captured errors when count is positive. These are focused reproductions, not integration tests.
- `git diff --check` reported no tracked whitespace errors; all three additions were untracked at review time. Application code remains unchanged.

Remaining evidence gaps: actual production credential validity, provider response formats/page totals, DB query runtime, per-board partial errors hidden by the current collector, false dedupe sample pairs, and actual worker run overlap. Resolve only the relevant gaps after reading the reports.

## Resume instruction

### Follow-up intake batch (implemented locally)

Public responses captured on 2026-09-22 confirm additional adapter gaps:

- Jobspresso `/feed/` has zero items; `/?feed=job_feed` has 10 job items with company/location namespaces and RFC2822 dates. Switch feeds and preserve those fields.
- iCIMS search is an iframe wrapper. The same search with `in_iframe=1` contains `iCIMS_JobCardItem` cards. Fetch and validate the readable inner listing; retry legacy false rejections once.
- Built In search HTML contains `data-id="job-card"` cards with titles, companies and locations, but the adapter accepts JSON-LD only.
- The Muse supports page 0; the current loop starts at 1 and skips it.
- Himalayas browse API caps pages at 20, ignoring the requested 100. Its documented search endpoint returns different jobs on pages 1 and 2. Use bounded role searches instead of repeatedly filtering the same first browse page.
- Workday searches only the first five expanded roles and always offset 0. Add bounded pagination and cover the ten current expanded roles without increasing the detail-request cap.

Evidence files are local ignored captures under `.venv-test/evidence/`; they are not deployment artifacts. These findings do not establish increased production inserts.

Implemented all six changes above. Jobspresso preserves employer, geographic restrictions, stable numeric ID and ISO-formatted posting dates. Built In and iCIMS card readers require same-host posting URLs and leave missing descriptions/dates for enrichment. iCIMS validation uses the same host resolution and inner-page URL as fetching. Legacy iCIMS format rejections now join Jobvite/Teamtailor's bounded one-time retry.

Workday now searches up to 10 distinct roles, trying every first page before deeper pages, with at most 20 listing requests per tenant and 3 pages per role. Its existing 20-detail-request cap remains. Himalayas uses at most 3 search pages per role; empty or repeated pages stop early, and a later failure preserves earlier results. The Muse includes page 0 within its existing two-page/category budget. These request caps increase coverage but can increase runtime; compare completed-run duration and new distinct inserts before increasing them further. iCIMS and Built In currently still read the first listing page; Jobspresso reads the feed's 10 available entries. They are not full historical backfills.

Verification: 130 focused tests passed across both intake regression modules, ATS discovery/validation/adapters, and existing Workday/Himalayas/The Muse tests. New cases exercise request limits, late-page failures, short/repeated pages, role coverage, stable identities, geographic restrictions, foreign-link rejection and one-time registry recovery. As above, this uses mocked HTTP and an isolated SQLite board table, not full PostgreSQL integration. The earlier 22 fetch-lock/task checks remain recorded separately.

Captured-response replays through the real adapters recovered 25 unique Built In jobs matching Software Engineer (all with company/location), 20 unique iCIMS jobs with locations, 10 Jobspresso entries with company/location, and 23 unique Software Engineer results across two Himalayas search pages. These 78 extracted entries are not a production insert count and include roles/geographies that normal matching may reject. The iCIMS probe accepted the captured inner page. `git diff --check` passed.

Primary public format references: [Jobspresso jobs feed](https://jobspresso.co/?feed=job_feed), [Built In search](https://builtin.com/jobs?search=software%20engineer), [iCIMS inner listing](https://hotjobs-teksynap.icims.com/jobs/search?ss=1&searchRelation=keyword_all&in_iframe=1), [Himalayas API documentation](https://himalayas.app/api), [The Muse page zero](https://www.themuse.com/api/public/jobs?category=Software%20Engineering&page=0). Public format/access checks were made from this workstation; production connectivity remains to be measured.

Read this file first. The reports are now supplied in the workspace. Preserve the source-intake-only scope and do not repeat the baseline request. Do not assume local configuration matches production. Raw user reports should remain untracked.
