-- Source intake only. PostgreSQL / psql. No application changes or writes.
-- See docs/JOB_SOURCE_AUDIT.md for commands and interpretation.
\set ON_ERROR_STOP on
\pset pager off
\pset null '(null)'
\pset columns 220
\x auto
BEGIN READ ONLY;
SET LOCAL statement_timeout = '45s';
SET LOCAL lock_timeout = '3s';

\echo '=== 1. Schema and actual retained history (not assumed to be 30 days) ==='
SELECT now() AS report_at, current_database() AS database;
SELECT version_num FROM alembic_version;
SELECT 'fetch_runs' AS history, count(*) AS rows, min(started_at) AS oldest,
       max(started_at) AS newest FROM fetch_runs
UNION ALL
SELECT 'agent_events', count(*), min(created_at), max(created_at) FROM agent_events;

\echo '=== 2. Fetch cadence and duration by group, retained last 30 days ==='
SELECT "group", count(*) AS runs, min(started_at) AS oldest, max(started_at) AS latest,
       round(avg(duration_seconds)::numeric, 1) AS avg_seconds,
       round(max(duration_seconds)::numeric, 1) AS max_seconds,
       count(*) FILTER (WHERE duration_seconds > 1800) AS over_lock_ttl,
       count(*) FILTER (WHERE status = 'failed') AS failed,
       count(*) FILTER (WHERE status = 'partial') AS partial,
       sum(fetched) AS fetched, sum(inserted) AS inserted
FROM fetch_runs WHERE started_at >= now() - interval '30 days'
GROUP BY "group" ORDER BY "group";

\echo '=== 3. Every scheduled source: attempts, zero results, and net new jobs ==='
\echo 'Disabled rows include off-group/manual skips; they are NOT failure attempts.'
WITH catalog(source, expected_group) AS (
  VALUES ('adzuna','api'), ('jsearch','api'), ('jooble','api'), ('careerjet','api'),
    ('findwork','api'), ('usajobs','api'), ('hiringcafe','api'), ('ycombinator','api'),
    ('linkedin','api'), ('indeed','api'), ('remotive','api'), ('arbeitnow','api'),
    ('remoteok','api'), ('weworkremotely','api'), ('themuse','api'), ('himalayas','api'),
    ('jobicy','api'), ('hnhiring','api'), ('workingnomads','api'), ('builtin','api'),
    ('jobspresso','api'), ('greenhouse','boards'), ('lever','boards'), ('ashby','boards'),
    ('smartrecruiters','boards'), ('workable','boards'), ('recruitee','boards'),
    ('workday','boards'), ('icims','boards'), ('bamboohr','boards'), ('teamtailor','boards'),
    ('jobvite','boards'), ('personio','boards'), ('wellfound','browser'),
    ('dice','browser'), ('handshake','browser')
), recent AS (
  SELECT s.*, f.started_at, f."group" AS run_group
  FROM fetch_source_runs s JOIN fetch_runs f ON f.id = s.run_id
  WHERE f.started_at >= now() - interval '30 days'
)
SELECT c.source, c.expected_group,
       count(r.id) FILTER (WHERE r.run_group IN (c.expected_group, 'all')) AS relevant_rows,
       count(r.id) FILTER (WHERE r.enabled) AS attempted,
       count(r.id) FILTER (WHERE r.enabled AND r.fetched = 0) AS zero_results,
       count(r.id) FILTER (WHERE r.enabled AND r.status = 'failed') AS failed,
       count(r.id) FILTER (WHERE r.enabled AND r.status = 'partial') AS partial,
       coalesce(sum(r.fetched) FILTER (WHERE r.enabled), 0) AS fetched,
       coalesce(sum(r.inserted), 0) AS inserted,
       coalesce(sum(r.merged), 0) AS enriched,
       coalesce(sum(r.skipped), 0) AS duplicates_or_archived,
       coalesce(sum(r.stale), 0) AS stale,
       coalesce(sum(r.fetched-r.inserted-r.merged-r.skipped-r.stale)
                FILTER (WHERE r.enabled), 0) AS unaccounted,
       coalesce(sum(r.inserted) FILTER (WHERE r.started_at >= now()-interval '7 days'),0) AS new_7d,
       max(r.started_at) FILTER (WHERE r.enabled) AS last_attempt,
       max(r.started_at) FILTER (WHERE r.inserted > 0) AS last_new_jobs
FROM catalog c LEFT JOIN recent r ON r.source = c.source
GROUP BY c.source, c.expected_group ORDER BY inserted, c.source;

\echo '=== 4. Last attempted run per source (ignores off-group disabled rows) ==='
SELECT DISTINCT ON (s.source) s.source, f.started_at, f."group", s.status,
       s.fetched, s.inserted, s.merged, s.skipped, s.stale
FROM fetch_source_runs s JOIN fetch_runs f ON f.id = s.run_id
WHERE s.enabled ORDER BY s.source, f.started_at DESC;

\echo '=== 5. Error examples per source; URLs and common credential fields removed ==='
\echo 'Review free-text output before sharing. No raw request payloads are selected.'
WITH messages AS (
  SELECT s.source, f.started_at,
    left(regexp_replace(regexp_replace(e.message,
      'https?://[^[:space:]<>]+', '[URL removed]', 'gi'),
      '(bearer[[:space:]]+|((api[_-]?key|token|secret|password|app_key|authorization)[[:space:]]*[=:][[:space:]]*))[^[:space:],;]+',
      '[credential removed]', 'gi'), 400) AS error
  FROM fetch_source_runs s JOIN fetch_runs f ON f.id=s.run_id
  CROSS JOIN LATERAL unnest(s.errors) AS e(message)
  WHERE f.started_at >= now()-interval '30 days'
), grouped AS (
  SELECT source, error, count(*) AS occurrences, max(started_at) AS latest
  FROM messages GROUP BY source, error
), ranked AS (
  SELECT *, row_number() OVER (PARTITION BY source ORDER BY latest DESC, occurrences DESC) AS rn
  FROM grouped
)
SELECT source, occurrences, latest, error FROM ranked WHERE rn <= 3 ORDER BY source, rn;

\echo '=== 6. Recent search queries/locations, up to 3 runs per group ==='
WITH ranked AS (
  SELECT *, row_number() OVER (PARTITION BY "group" ORDER BY started_at DESC) AS rn
  FROM fetch_runs
)
SELECT started_at, "group", status, queries, locations, fetched, inserted,
       duration_seconds FROM ranked WHERE rn <= 3 ORDER BY "group", started_at DESC;

\echo '=== 7. Company board coverage (counts are returned rows, not unique new jobs) ==='
SELECT ats, count(*) AS boards,
       count(*) FILTER (WHERE active) AS active,
       count(*) FILTER (WHERE active AND last_fetched_at IS NULL) AS active_never_polled,
       count(*) FILTER (WHERE active AND (last_fetched_at IS NULL OR
                                  last_fetched_at < now()-interval '7 days')) AS active_not_polled_7d,
       count(*) FILTER (WHERE validated_at IS NULL) AS awaiting_probe,
       count(*) FILTER (WHERE NOT active AND inactive_reason IS NULL) AS retired_empty,
       count(*) FILTER (WHERE NOT active AND total_job_count > 0) AS inactive_once_productive,
       max(last_fetched_at) AS last_poll
FROM company_boards GROUP BY ats ORDER BY ats;

\echo '=== 8. Up to 5 neglected active boards per ATS ==='
WITH ranked AS (
  SELECT *, row_number() OVER (PARTITION BY ats ORDER BY last_fetched_at ASC NULLS FIRST, first_seen_at) AS rn
  FROM company_boards
  WHERE active AND (last_fetched_at IS NULL OR last_fetched_at < now()-interval '7 days')
)
SELECT ats, slug, origin, first_seen_at, last_fetched_at, last_job_count, total_job_count
FROM ranked WHERE rn <= 5 ORDER BY ats, rn;

\echo '=== 9. Inactive board reasons and earlier productivity ==='
SELECT ats, origin,
       left(regexp_replace(coalesce(inactive_reason,'empty retirement'),
            'https?://[^[:space:]<>]+','[URL removed]','gi'),180) AS reason,
       count(*) AS boards, count(*) FILTER (WHERE total_job_count > 0) AS once_productive
FROM company_boards WHERE NOT active GROUP BY 1,2,3 ORDER BY boards DESC LIMIT 50;

\echo '=== 10. Browser activity last 7 days (host only) ==='
SELECT kind, host, count(*) AS events, count(*) FILTER (WHERE NOT ok) AS not_ok,
       max(created_at) AS latest
FROM agent_events
WHERE created_at >= now()-interval '7 days'
  AND kind IN ('poll','read','browse','harvest','sweep','task_failed')
GROUP BY kind,host ORDER BY kind, events DESC LIMIT 100;

\echo '=== 10a. Browser challenges, last 21 days (may be shorter due to retention) ==='
SELECT host, summary->>'challenge' AS challenge, ok, count(*) AS events,
       max(created_at) AS latest
FROM agent_events
WHERE kind='browse' AND created_at >= now()-interval '21 days'
GROUP BY 1,2,3 ORDER BY latest DESC LIMIT 60;

\echo '=== 10b. Reader response counters, last 7 days ==='
\echo 'Reports can be repeated snapshots of one page; response sums are diagnostic, not unique jobs.'
SELECT host, count(*) AS reports,
       count(*) FILTER (WHERE summary->>'first'='true') AS first_reports,
       sum(CASE WHEN summary->>'json' ~ '^[0-9]+$' THEN (summary->>'json')::bigint ELSE 0 END) AS json_seen,
       sum(CASE WHEN summary->>'sent' ~ '^[0-9]+$' THEN (summary->>'sent')::bigint ELSE 0 END) AS sent,
       sum(CASE WHEN summary->>'probed' ~ '^[0-9]+$' THEN (summary->>'probed')::bigint ELSE 0 END) AS probed,
       sum(CASE WHEN summary->>'url_no' ~ '^[0-9]+$' THEN (summary->>'url_no')::bigint ELSE 0 END) AS url_rejected
FROM agent_events WHERE kind='read' AND created_at >= now()-interval '7 days'
GROUP BY host ORDER BY reports DESC LIMIT 50;

\echo '=== 11. Browser harvest yield last 7 days; separate from fetch_source_runs ==='
SELECT host, summary->>'source' AS source, summary->>'read_by' AS reader,
       count(*) AS payloads,
       sum(CASE WHEN summary->>'found' ~ '^[0-9]+$' THEN (summary->>'found')::bigint ELSE 0 END) AS found,
       sum(CASE WHEN summary->>'inserted' ~ '^[0-9]+$' THEN (summary->>'inserted')::bigint ELSE 0 END) AS inserted,
       sum(CASE WHEN summary->>'merged' ~ '^[0-9]+$' THEN (summary->>'merged')::bigint ELSE 0 END) AS merged,
       sum(CASE WHEN summary->>'skipped' ~ '^[0-9]+$' THEN (summary->>'skipped')::bigint ELSE 0 END) AS skipped,
       sum(CASE WHEN summary->>'invalid' ~ '^[0-9]+$' THEN (summary->>'invalid')::bigint ELSE 0 END) AS invalid
FROM agent_events WHERE kind='harvest' AND created_at >= now()-interval '7 days'
GROUP BY 1,2,3 ORDER BY inserted, found DESC LIMIT 100;

\echo '=== 12. API sweep coverage and stop reasons (includes linked Tsenta) ==='
SELECT host, summary->>'deep' AS deep, summary->>'stopped' AS stopped,
       count(*) AS sweeps, max(created_at) AS latest,
       sum(CASE WHEN summary->>'pages' ~ '^[0-9]+$' THEN (summary->>'pages')::bigint ELSE 0 END) AS pages,
       sum(CASE WHEN summary->>'rows' ~ '^[0-9]+$' THEN (summary->>'rows')::bigint ELSE 0 END) AS rows,
       sum(CASE WHEN summary->>'inserted' ~ '^[0-9]+$' THEN (summary->>'inserted')::bigint ELSE 0 END) AS inserted,
       sum(CASE WHEN summary->>'capped_slices' ~ '^[0-9]+$' THEN (summary->>'capped_slices')::bigint ELSE 0 END) AS capped_slices
FROM agent_events WHERE kind='sweep' AND created_at >= now()-interval '7 days'
GROUP BY 1,2,3 ORDER BY latest DESC LIMIT 50;

\echo '=== 13. Browser work backlog and recent outcomes ==='
SELECT substring(payload->>'url' from '://([^/?:#]+)') AS host,
       kind, status, count(*) AS tasks, min(created_at) AS oldest, max(created_at) AS latest,
       count(*) FILTER (WHERE status IN ('queued','leased') AND expires_at < now()) AS overdue_expiry
FROM browser_tasks WHERE created_at >= now()-interval '7 days' OR status IN ('queued','leased')
GROUP BY 1,2,3 ORDER BY tasks DESC LIMIT 100;

\echo '=== 14. Saved parser evidence: metadata only, no captured payloads ==='
SELECT host, count(*) AS samples, max(bytes) AS biggest, sum(found) AS found,
       max(created_at) AS latest FROM harvest_samples GROUP BY host ORDER BY latest DESC LIMIT 40;

\echo '=== 15. Jobs still stored, by original source, first ingested in last 30 days ==='
\echo 'Not complete ingestion history: archives/deletions and cross-source merges change this view.'
SELECT source, count(*) AS stored_30d,
       count(*) FILTER (WHERE fetched_at >= now()-interval '7 days') AS stored_7d,
       count(*) FILTER (WHERE fetched_at >= now()-interval '24 hours') AS stored_24h,
       max(fetched_at) AS newest,
       count(*) FILTER (WHERE posted_at IS NULL) AS missing_posted_date,
       count(*) FILTER (WHERE coalesce(btrim(url),'')='') AS missing_url,
       count(*) FILTER (WHERE coalesce(btrim(company),'')='') AS missing_company,
       count(*) FILTER (WHERE status::text='new') AS awaiting_matching,
       count(*) FILTER (WHERE status::text='filtered_out') AS filtered_out,
       count(*) FILTER (WHERE status::text='matched') AS matched_now
FROM jobs WHERE fetched_at >= now()-interval '30 days' GROUP BY source ORDER BY stored_7d;

COMMIT;
\echo '=== Done: source-intake report complete ==='
