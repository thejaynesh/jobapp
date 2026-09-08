-- Is the browser tier actually working right now? Read-only. Run with:
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U jobapp -d jobapp -f - < scripts/db_pulse.sql
--
-- `db_board.sql` is the full six-cause walkthrough for a board that is dry.
-- This is the short version, for the question you ask straight after a deploy:
-- did anything change. Four sections, and they chain the same way — a board
-- with no visits in section 2 cannot possibly show jobs in section 3, so read
-- them in order and stop at the first one that is wrong.
--
-- Nothing here is a job description or a payload. Safe to paste.

\pset pager off
\timing off

\echo '=== 1. Jobs landed, last 24h ======================================'
SELECT source, count(*) AS jobs, max(fetched_at) AS newest
FROM jobs
WHERE fetched_at > now() - interval '24 hours'
GROUP BY source
ORDER BY jobs DESC;

\echo ''
\echo '=== 2. Who is getting visited (last 6h) ==========================='
-- The gate. A board starved of visits cannot contribute whatever else is
-- fixed, and a host piling up `queued` rows is one crowding everything else
-- out — which is what jooble was doing with 308 of them.
SELECT substring(payload->>'url' from '://([^/]+)') AS host,
       status,
       count(*)        AS tasks,
       max(created_at) AS newest
FROM browser_tasks
WHERE created_at > now() - interval '6 hours'
  AND payload->>'url' IS NOT NULL
GROUP BY 1, 2
ORDER BY 3 DESC;

\echo ''
\echo '=== 3. What the reader made of what it saw (last 6h) =============='
-- `jobs_found` is the number the reader work is judged on. `read_by` says
-- whether a learned recipe or the generic shape walker did it, and `unusable`
-- should sit at zero now that a posting another request inserted first is
-- merged rather than dropped.
SELECT summary->>'source'                              AS source,
       summary->>'read_by'                             AS read_by,
       count(*)                                        AS payloads,
       sum(coalesce((summary->>'found')::int, 0))      AS jobs_found,
       sum(coalesce((summary->>'inserted')::int, 0))   AS inserted,
       sum(coalesce((summary->>'merged')::int, 0))     AS enriched,
       sum(coalesce((summary->>'invalid')::int, 0))    AS unusable
FROM agent_events
WHERE kind = 'harvest' AND created_at > now() - interval '6 hours'
GROUP BY 1, 2
ORDER BY jobs_found DESC;

\echo ''
\echo '=== 4. Evidence kept per host ====================================='
-- Nothing can be learned about a board whose payloads never reached the store.
-- Dice and LinkedIn had no rows here at all, which is what the probe-budget
-- split and the displacing keep-rule are for: a host appearing with a large
-- `biggest` is a board that can now be diagnosed by `replay_samples.py`.
SELECT host, count(*) AS samples, max(bytes) AS biggest,
       sum(found) AS jobs_the_walker_saw, max(created_at) AS newest
FROM harvest_samples
GROUP BY host
ORDER BY newest DESC
LIMIT 15;
