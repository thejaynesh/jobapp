# Job source volume audit — 2026-09-22

## Scope and handoff

Only increase useful job intake from existing sources, especially broken or zero-yield sources. User requested code review and database diagnostic commands before implementation. Do not change production behavior yet. Update this document as findings are established so a session interruption does not lose the investigation.

Status: initial code review complete; awaiting production reports before implementation. Reviewed checkout: `514fdf6`. No production database access or live provider tests performed. Local code is evidence of behavior, not proof of which sources are failing in production. Only this audit and diagnostic scripts have been added.

## Evidence already established

- Fetch adapters run in `api`, `boards`, and `browser` groups (`app/services/job_fetcher.py`). Sources outside a run's group are recorded as disabled; do not treat these rows as actual source outages.
- `fetch_runs` and `fetch_source_runs` preserve fetched/inserted/merged/skipped/stale counts and errors. Only 200 runs are retained globally (`app/services/fetch_history.py`); a requested 30-day window may contain much less history.
- Browser-agent intake has separate evidence in `agent_events`, `browser_tasks`, and `harvest_samples`. Source health must include both intake paths.
- Existing general-purpose SQL reports cover many unrelated matching/enrichment features. Prepare a narrower source-volume report with actual coverage, group-aware source summaries, errors, board coverage, and browser yield.

## Next steps

1. [x] Review scheduler, source adapters, board selection/retirement, browser intake, and deduplication.
2. [x] Append confirmed behaviors and separately labeled impact hypotheses with code references.
3. [x] Save read-only diagnostic SQL and exact production commands; check model/migration references.
4. [ ] Await the user's report, rank sources by recoverable new jobs, and then implement targeted fixes with regression checks.

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

## Validation and limits

Schema references were checked against current SQLAlchemy models and migration 0032 for `harvest_samples`. No live database query execution or provider testing has occurred. The system Python lacks the project's SQLAlchemy/pytest/PostgreSQL driver dependencies, so this audit does not claim a full application test run or PostgreSQL execution validation.

Completed local checks:

- Python diagnostic compiles; every allowlisted setting exists in the current configuration model.
- SQL source catalog covers all 36 scheduled source identifiers, including currently disabled sources.
- Executed extracted functions with bounded in-memory inputs (imports/dependencies substituted, no network/DB): S04 appends a legacy slug excluded from active registry selection; S06 bypasses resting with a non-null source selection; S07 always permits probing at retained count 200/default interval 10; S05 discards captured errors when count is positive. These are focused reproductions, not integration tests.
- `git diff --check` reported no tracked whitespace errors; all three additions were untracked at review time. Application code remains unchanged.

Remaining evidence gaps: actual production credential validity, provider response formats/page totals, DB query runtime, per-board partial errors hidden by the current collector, false dedupe sample pairs, and actual worker run overlap. Resolve only the relevant gaps after reading the reports.

## Resume instruction

Read this file first, then `scripts/db_source_intake.sql` once created. Preserve the source-intake-only scope. Ask for the report if not supplied; do not assume local configuration matches production.
