-- Why a login-only board is contributing nothing. Read-only. Run with:
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U jobapp -d jobapp -f - < scripts/db_board.sql > board.txt
--
-- The boards behind a login — LinkedIn, JobRight, Handshake, Hiring Cafe,
-- Tsenta — have no public API, so everything about them arrives through the
-- browser. That makes "this board gave us nothing" a question with six
-- completely different answers, and from the outside they are identical: the
-- job count does not move.
--
--   1. nothing was ever queued        — the board is not in the crawl rotation,
--                                       or the queue is full of other work
--   2. it was queued and never run    — no agent polled, or it cannot run the
--                                       kind, or the tasks expired unread
--   3. it was opened and blocked      — a sign-in wall or a challenge rendered
--                                       instead of the board
--   4. it was opened and not read     — the harvest checkbox for that site is
--                                       off, so no reader was registered
--   5. it was read and nothing matched — the reader saw JSON and none of it was
--                                       job-shaped
--   6. it worked and the jobs are dups — the board genuinely has nothing new
--
-- Sections A–F below are those six, in that order. Read them in order and stop
-- at the first one that is wrong: they are a chain, and a break early on makes
-- everything after it look broken too.
--
-- Nothing here is a job description or a payload. Safe to paste.

\pset pager off
\timing off

\echo '=================================================================='
\echo 'A. What each browser-side source has actually contributed'
\echo '=================================================================='
-- The bottom line. `newest` is the one to read: a source with thousands of
-- rows and a newest of three weeks ago stopped working three weeks ago.
SELECT source,
       count(*)                                        AS jobs,
       count(*) FILTER (WHERE description IS NOT NULL) AS with_description,
       count(*) FILTER (WHERE status = 'matched')      AS matched,
       max(fetched_at)                                 AS newest
FROM jobs
WHERE source LIKE '%harvest%' OR source IN ('linkedin', 'handshake')
GROUP BY source
ORDER BY jobs DESC;

\echo ''
\echo '=================================================================='
\echo 'B. Was anything queued for it, and did it run? (last 7 days)'
\echo '=================================================================='
-- Section 1 and 2 together. No rows at all for a host means it is not being
-- queued; rows stuck at `queued` or `expired` mean nothing ran them.
SELECT substring(payload->>'url' from '://([^/]+)') AS host,
       status,
       count(*)                                     AS tasks,
       max(created_at)                              AS newest,
       max(attempts)                                AS most_attempts
FROM browser_tasks
WHERE created_at > now() - interval '7 days'
  AND payload->>'url' IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 2;

\echo ''
\echo '-- Why the failures failed, in their own words --------------------'
SELECT substring(payload->>'url' from '://([^/]+)') AS host,
       left(coalesce(error, ''), 120)               AS error,
       count(*)                                     AS times,
       max(created_at)                              AS newest
FROM browser_tasks
WHERE created_at > now() - interval '7 days'
  AND status IN ('failed', 'expired')
GROUP BY 1, 2
ORDER BY times DESC
LIMIT 20;

\echo ''
\echo '=================================================================='
\echo 'C. What the visits saw (last 7 days)'
\echo '=================================================================='
-- Section 3. `signed_out` is a sign-in wall; `challenged` is a bot check that
-- never cleared. Either one means the board never rendered, and every number
-- after this point is describing the login page rather than the board.
--
-- `no_scroll` is the other half: a visit that rendered and never moved. On an
-- infinite-scroll board the scroll *is* the pagination, so zero means one
-- screenful, however healthy the visit looks otherwise.
SELECT host,
       count(*)                                                   AS visits,
       count(*) FILTER (WHERE NOT ok)                             AS not_ok,
       count(*) FILTER (WHERE summary->>'signed_in' = 'false')    AS signed_out,
       count(*) FILTER (WHERE summary->>'challenge' IN ('timeout','skipped'))
                                                                  AS challenged,
       count(*) FILTER (WHERE coalesce((summary->>'scrolled_px')::int, 0) = 0)
                                                                  AS no_scroll,
       count(*) FILTER (WHERE summary->>'searched_ok' = 'false')  AS search_box_missing,
       round(avg(coalesce((summary->>'batches')::int, 0)), 1)     AS avg_batches,
       max(created_at)                                            AS newest
FROM agent_events
WHERE kind = 'browse' AND created_at > now() - interval '7 days'
GROUP BY host
ORDER BY visits DESC;

\echo ''
\echo '-- Which element the scroll actually moved ------------------------'
-- "(nothing scrolled)" is the diagnosis: the page has a scroller and we did
-- not find it. Anything else names what moved.
SELECT host,
       coalesce(summary->>'scroll_target', '?') AS scroll_target,
       count(*)                                 AS visits
FROM agent_events
WHERE kind = 'browse' AND created_at > now() - interval '7 days'
GROUP BY 1, 2
ORDER BY host, visits DESC;

\echo ''
\echo '=================================================================='
\echo 'D. Which sites the reader is registered on'
\echo '=================================================================='
-- Section 4, and the one that is invisible from the server otherwise. A host
-- that is missing here has its harvest checkbox switched off in the extension,
-- and no amount of opening its pages will ever read one.
SELECT agent->>'agent_id'                       AS agent,
       agent->>'at'                             AS last_polled,
       agent->'harvest_sites'                   AS reading,
       agent->'kinds'                           AS can_run
FROM profiles,
     LATERAL jsonb_each(coalesce(data->'agents', '{}'::jsonb)) AS a(name, agent);

\echo ''
\echo '=================================================================='
\echo 'E. What the reader looked at, and what it forwarded (last 7 days)'
\echo '=================================================================='
-- Section 5. `json_seen` is the denominator nothing else has: a host with
-- zero saw no JSON at all (a server-rendered board, or a reader that was not
-- registered), and a host with thousands and `sent` of zero saw plenty and
-- recognised none of it. Opposite problems, same silence in the job count.
SELECT host,
       count(*)                                                  AS pages,
       sum(coalesce((summary->>'json')::int, 0))                 AS json_seen,
       sum(coalesce((summary->>'sent')::int, 0))                 AS forwarded,
       sum(coalesce((summary->>'url_no')::int, 0))               AS url_rejected,
       sum(coalesce((summary->>'shape_no')::int, 0))             AS shape_rejected,
       max(created_at)                                           AS newest
FROM agent_events
WHERE kind = 'read' AND created_at > now() - interval '7 days'
GROUP BY host
ORDER BY json_seen DESC;

\echo ''
\echo '-- And what came of the payloads that were forwarded --------------'
SELECT summary->>'source'                                  AS source,
       summary->>'read_by'                                 AS read_by,
       count(*)                                            AS payloads,
       sum(coalesce((summary->>'found')::int, 0))          AS jobs_found,
       sum(coalesce((summary->>'inserted')::int, 0))       AS inserted,
       sum(coalesce((summary->>'merged')::int, 0))         AS enriched,
       sum(coalesce((summary->>'skipped')::int, 0))        AS already_had,
       sum(coalesce((summary->>'invalid')::int, 0))        AS unusable,
       max(created_at)                                     AS newest
FROM agent_events
WHERE kind = 'harvest' AND created_at > now() - interval '7 days'
GROUP BY 1, 2
ORDER BY jobs_found DESC;

\echo ''
\echo '=================================================================='
\echo 'F. What we have learned about walking and reading each board'
\echo '=================================================================='
-- A board with no crawl recipe is being walked by the generic heuristics,
-- which is fine until it is not. A retired one is the interesting case: it
-- means visits under it kept landing on page one.
SELECT host, mode, active, tries, wins,
       left(coalesce(note, ''), 70) AS note, updated_at
FROM crawl_recipes
ORDER BY updated_at DESC
LIMIT 20;

\echo ''
SELECT host, active, tries, wins, left(coalesce(note, ''), 70) AS note, updated_at
FROM harvest_recipes
ORDER BY updated_at DESC
LIMIT 20;
