-- What the collection bugs fixed in 0d97902 left behind in the data.
--
-- The code reviewed there (0bfca19..c250fec) reached production on
-- 2026-10-08 and ran until the fixes deployed. The fixes stop new damage;
-- they do not undo what is already stored. This counts each kind, each beside
-- the same count for an equally long stretch before the window where that
-- means something, and says whether it repairs itself or needs a repair.
--
-- Read-only: one READ ONLY transaction, rolled back at the end.
--
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U jobapp -d jobapp -v ON_ERROR_STOP=1 -f - \
--     < scripts/db_review_damage.sql > review-damage.txt
--
-- The window starts at the deploy of 3eb95aa unless told otherwise:
--     ... psql -U jobapp -d jobapp -v since='2026-10-08 13:00+00' ...
--
-- Fetch history keeps the most recent 200 runs, so sections built on it
-- (3, 6, 7) cover only that much of the window; section 0 prints how far back it reaches.

\pset pager off
\pset footer off

\if :{?since}
\else
\set since '2026-10-08 13:00+00'
\endif

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5min';

-- `before_ts` starts a stretch as long as the window, ending where it begins:
-- the baseline each "during" figure is read against.
SELECT :'since'::timestamptz AS since_ts,
       :'since'::timestamptz - (now() - :'since'::timestamptz) AS before_ts,
       -- link_resolver._INTERSTITIAL_PATTERNS, joined. Keep the two in step.
       'adzuna\.[a-z.]+/(?:land|jobs)/ad/|adzuna\.[a-z.]+/details/|jooble\.org/(?:away|jdp)/'
       '|careerjet\.[a-z.]+/jobad/|\.careerjet\.[a-z.]+/|indeed\.com/(?:rc/clk|pagead/clk|applystart)'
       '|(?:^|//|\.)appcast\.io/|(?:^|//|\.)recruitics\.com/|click\.jobvite\.com/'
       '|(?:^|//|\.)clickcast\.jobs/|(?:^|//|\.)jobs2web\.com/|/redirect\?|/out\?url=' AS interstitial,
       -- posting_identity.AUTHORITATIVE_SOURCES and job_fetcher.FULL_FEED_BOARDS.
       '{greenhouse,lever,ashby,smartrecruiters,workable,recruitee,workday,icims,bamboohr,'
       'teamtailor,jobvite,personio,oracle,successfactors,phenom,eightfold,jibe,rippling,'
       'pinpoint,jazzhr,taleo,paylocity,avature,amazon,apple,tiktok,usajobs}' AS authoritative,
       '{greenhouse,lever,ashby,recruitee,pinpoint,paylocity,bamboohr,personio,workable,avature}' AS full_feed
\gset

\echo ''
\echo '=== 0. Window ==='
SELECT :'since_ts'::timestamptz AS window_from, now() AS window_to,
       :'before_ts'::timestamptz AS baseline_from,
       (SELECT min(started_at) FROM fetch_runs) AS fetch_history_reaches_back_to;


\echo ''
\echo '=== 1. Aggregator redirect links that were never followed ==='
\echo '--- The durable batch stored these before link resolution ran, so the resolver'
\echo '--- treated them as known and skipped them. Unresolved means no apply_url yet.'
\echo '--- The browser fallback may resolve some later, but that does not merge rows.'
SELECT j.source,
       count(*) FILTER (WHERE j.fetched_at < :'since_ts') AS before_jobs,
       round(100.0 * count(*) FILTER (WHERE j.fetched_at < :'since_ts' AND j.apply_url IS NULL)
             / nullif(count(*) FILTER (WHERE j.fetched_at < :'since_ts'), 0), 1) AS "before_unresolved%",
       count(*) FILTER (WHERE j.fetched_at >= :'since_ts') AS during_jobs,
       round(100.0 * count(*) FILTER (WHERE j.fetched_at >= :'since_ts' AND j.apply_url IS NULL)
             / nullif(count(*) FILTER (WHERE j.fetched_at >= :'since_ts'), 0), 1) AS "during_unresolved%"
FROM jobs j
WHERE j.fetched_at >= :'before_ts' AND j.url ~* :'interstitial'
GROUP BY j.source
ORDER BY during_jobs DESC;

\echo ''
\echo '--- 1b. Likely stored twice: a redirect job that shares its address (resolved) or'
\echo '---     its company/title/location fingerprint (unresolved) with an employer-board job.'
\echo '---     The fingerprint is a candidate, not proof; compare the samples below.'
-- Fingerprints gathered once and hash-joined: `dedupe_hash` has no index, and a
-- per-row lookup would read the whole table once per redirect job.
WITH board_hashes AS (
    SELECT DISTINCT dedupe_hash FROM jobs WHERE source = ANY (:'authoritative'::text[])
), redirect AS (
    SELECT j.id, j.apply_url, j.dedupe_hash, j.fetched_at >= :'since_ts' AS during
    FROM jobs j
    WHERE j.fetched_at >= :'before_ts' AND j.url ~* :'interstitial'
)
SELECT r.during, count(*) AS redirect_jobs,
       count(*) FILTER (WHERE r.apply_url IS NOT NULL AND EXISTS (
           SELECT 1 FROM jobs b WHERE b.id <> r.id AND b.source = ANY (:'authoritative'::text[])
             AND b.source_urls @> ARRAY[r.apply_url]::varchar[])) AS same_address,
       count(*) FILTER (WHERE r.apply_url IS NULL
                        AND r.dedupe_hash IN (SELECT dedupe_hash FROM board_hashes)) AS same_fingerprint
FROM redirect r
GROUP BY r.during
ORDER BY r.during;

SELECT DISTINCT ON (j.fetched_at, j.id)
       j.source, j.title, j.company, j.url AS redirect_url, b.source AS board, b.url AS board_url
FROM jobs j
JOIN jobs b ON b.dedupe_hash = j.dedupe_hash AND b.source = ANY (:'authoritative'::text[])
WHERE j.fetched_at >= :'since_ts' AND j.url ~* :'interstitial' AND j.apply_url IS NULL
ORDER BY j.fetched_at DESC, j.id
LIMIT 10;
\echo '--- Repair: needed. Merging a confirmed pair needs the application code (sources, addresses,'
\echo '---         listings, applications); not a hand-written UPDATE.'


\echo ''
\echo '=== 2. Rows stored twice because a chunk lookup went stale ==='
\echo '--- The same URL on two jobs, or an archived posting brought back as new.'
SELECT d.latest >= :'since_ts' AS second_copy_during_window, count(*) AS urls, sum(d.rows) AS rows
FROM (SELECT url, count(*) AS rows, max(fetched_at) AS latest FROM jobs GROUP BY url HAVING count(*) > 1) d
GROUP BY 1
ORDER BY 1;

SELECT j.fetched_at >= :'since_ts' AS during, count(*) AS archived_posting_stored_again
FROM jobs j
WHERE j.fetched_at >= :'before_ts'
  AND EXISTS (SELECT 1 FROM archived_jobs a WHERE a.url = j.url AND a.archived_at < j.fetched_at)
GROUP BY 1
ORDER BY 1;
\echo '--- Repair: needed for the duplicate URLs (merge into the older row); an archived posting'
\echo '---         brought back is a new row to score again, and can be archived again.'


\echo ''
\echo '=== 3. Sources rested on runs that were not failures ==='
\echo '--- "failed" was recorded when anything was dropped and nothing new stored, even if the'
\echo '--- rest were skipped as known. Ten in a row rests a source. corrected_streak is the'
\echo '--- streak with those runs counted as partial.'
WITH runs AS (
    SELECT r.source, r.status, f.started_at,
           r.status = 'failed' AND r.dropped > 0
             AND r.inserted + r.merged + r.skipped + r.stale > 0 AS misclassified,
           row_number() OVER (PARTITION BY r.source ORDER BY f.started_at DESC) AS n
    FROM fetch_source_runs r JOIN fetch_runs f ON f.id = r.run_id
    WHERE r.status NOT IN ('disabled', 'skipped')
), breaks AS (
    SELECT source,
           min(n) FILTER (WHERE status <> 'failed') AS first_ok,
           min(n) FILTER (WHERE status <> 'failed' OR misclassified) AS first_ok_corrected
    FROM runs GROUP BY source
)
SELECT runs.source,
       count(*) FILTER (WHERE runs.misclassified AND runs.started_at >= :'since_ts') AS misclassified_runs,
       count(*) FILTER (WHERE runs.n < coalesce(b.first_ok, 2147483647)) AS current_streak,
       count(*) FILTER (WHERE runs.n < coalesce(b.first_ok_corrected, 2147483647)) AS corrected_streak
FROM runs JOIN breaks b USING (source)
GROUP BY runs.source
HAVING count(*) FILTER (WHERE runs.misclassified) > 0
ORDER BY current_streak DESC, misclassified_runs DESC;
\echo '--- Repair: self-heals. A rested source is probed every 10 runs, and the probe is now'
\echo '---         classified correctly. Relabelling the misclassified rows partial ends it now.'


\echo ''
\echo '=== 4. Jobs sent back through extraction and matching on every board sighting ==='
\echo '--- A board re-asserting a derived value (remote=False) over the extracted one reset the'
\echo '--- job to new each cycle. Jobs with three or more calls of one stage in the period.'
SELECT stage,
       count(DISTINCT job_id) FILTER (WHERE created_at < :'since_ts' AND per_job >= 3) AS before_jobs_3plus,
       count(DISTINCT job_id) FILTER (WHERE created_at >= :'since_ts' AND per_job >= 3) AS during_jobs_3plus,
       count(*) FILTER (WHERE created_at < :'since_ts') AS before_calls,
       count(*) FILTER (WHERE created_at >= :'since_ts') AS during_calls
FROM (
    SELECT stage, job_id, created_at,
           count(*) OVER (PARTITION BY stage, job_id, created_at >= :'since_ts') AS per_job
    FROM llm_calls
    WHERE created_at >= :'before_ts' AND job_id IS NOT NULL
) c
GROUP BY stage
ORDER BY during_calls DESC;
\echo '--- Repair: self-heals. Values a listing repeats no longer overwrite; each affected job'
\echo '---         is extracted and scored once more. The calls already made are spent.'


\echo ''
\echo '=== 5. Closures a board listing undid ==='
\echo '--- Any board sighting reopened any closure. Liveness re-checks a job within'
\echo '--- LIVENESS_RECHECK_DAYS (3 by default) and closes it again, and leaves no trace to'
\echo '--- count here. A browser closure is never re-checked: these are open again although'
\echo '--- the browser last found the posting gone.'
WITH latest AS (
    -- Each job's most recent browser outcome, from the tasks: there are far fewer of
    -- them than open jobs, and nothing indexes the job id inside the payload.
    SELECT DISTINCT ON (t.payload->>'job_id')
           (t.payload->>'job_id')::uuid AS job_id, t.completed_at, t.result
    FROM browser_tasks t
    WHERE t.completed_at IS NOT NULL AND t.result ? 'ingest'
      AND t.payload->>'job_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    ORDER BY t.payload->>'job_id', t.completed_at DESC
)
SELECT count(*) AS browser_closed_now_open,
       count(*) FILTER (WHERE j.last_seen_at > l.completed_at) AS seen_on_a_board_since
FROM latest l
JOIN jobs j ON j.id = l.job_id
WHERE j.closed_at IS NULL AND l.result->'ingest'->>'closed' = 'true';
\echo '--- Repair: needed. Close each again with the note the browser left.'


\echo ''
\echo '=== 6. Postings that could not be stored, still queued for replay ==='
\echo '--- Kept for good and replayed every cycle; their runs could not be pruned.'
SELECT source, error_category, count(*) AS batches,
       sum(jsonb_array_length(payload)) AS postings, min(observed_at) AS oldest
FROM fetch_board_runs
WHERE jsonb_typeof(payload) = 'array' AND jsonb_array_length(payload) > 0
GROUP BY source, error_category
ORDER BY postings DESC;
SELECT count(*) AS fetch_runs_kept, 200 AS retention FROM fetch_runs;
\echo '--- Repair: self-heals. Rejected postings are dropped on the next replay, others after'
\echo '---         three attempts; pruning resumes once their batches are empty.'


\echo ''
\echo '=== 7. Board positions lost to a failed resumed pass ==='
\echo '--- A board read that failed outright right after a pass that saved a position;'
\echo '--- the position was erased and the next pass started from the first page.'
WITH runs AS (
    SELECT source, board, status, cursor, observed_at, error_category,
           lead(cursor) OVER (PARTITION BY source, board ORDER BY observed_at DESC) AS previous_cursor
    FROM fetch_board_runs
    WHERE board <> ''
)
SELECT source, count(*) AS positions_lost, count(DISTINCT board) AS boards
FROM runs
WHERE observed_at >= :'since_ts' AND status = 'failed' AND cursor IS NULL AND previous_cursor IS NOT NULL
  AND coalesce(error_category, '') <> 'ingestion'
GROUP BY source
ORDER BY positions_lost DESC;
\echo '--- Repair: self-heals. Those boards re-read their first pages once; nothing is wrong'
\echo '---         with what is stored.'


\echo ''
\echo '=== 8. Company names replaced by the board registry''s name ==='
\echo '--- Every board posting took the registry name, over one the reader actually read.'
\echo '--- Countable only for listings with a revision from before the window; ones first'
\echo '--- seen during it kept no earlier name to compare.'
WITH latest AS (
    SELECT DISTINCT ON (listing_key) listing_key, observed_at, snapshot->>'company' AS company
    FROM listing_revisions ORDER BY listing_key, observed_at DESC
), earlier AS (
    SELECT DISTINCT ON (listing_key) listing_key, snapshot->>'company' AS company
    FROM listing_revisions WHERE observed_at < :'since_ts'
    ORDER BY listing_key, observed_at DESC
)
SELECT s.source, count(*) AS renamed, count(DISTINCT s.board) AS boards,
       (array_agg(e.company || ' -> ' || l.company))[1:3] AS examples
FROM latest l
JOIN earlier e USING (listing_key)
JOIN source_listings s ON s.identity_key = l.listing_key
JOIN company_boards b ON b.ats = s.source AND b.slug = s.board
WHERE l.observed_at >= :'since_ts'
  AND l.company = b.company
  AND e.company IS DISTINCT FROM l.company
  AND lower(e.company) <> lower(s.board)
GROUP BY s.source
ORDER BY renamed DESC;
\echo '--- Repair: self-heals. The next sighting differs from the stored registry name and'
\echo '---         restores the name the reader reads (and rescores that job once).'


\echo ''
\echo '=== 9. Employers re-watched by saving Today''s priorities ==='
\echo '--- Every save re-watched every pinned employer and reset its research. Nothing'
\echo '--- recorded which had been paused: check this list, and pause any again on /companies.'
SELECT c.name, c.watched, c.research_status, c.last_researched_at, c.created_at
FROM companies c
WHERE c.identity_source = 'user' AND c.watched
  AND EXISTS (
      SELECT 1
      FROM profiles p, jsonb_array_elements_text(coalesce(p.data->'plan_pins', '[]'::jsonb)) pin
      WHERE lower(regexp_replace(btrim(pin), '\s+', ' ', 'g')) IN (
          SELECT lower(regexp_replace(btrim(alias), '\s+', ' ', 'g'))
          FROM jsonb_array_elements_text(c.aliases || to_jsonb(c.name)) alias))
ORDER BY c.name;
\echo '--- Repair: by hand, only you know which were paused.'


\echo ''
\echo '=== 10. Board rows without an id, closed as if unlisted ==='
\echo '--- Closed after every full read of their board and reopened by the next sighting;'
\echo '--- under 0d97902 alone the reopening stopped, so these stay closed until 4bd9f08.'
SELECT s.source, count(DISTINCT j.id) AS closed_now
FROM jobs j
JOIN source_listings s ON s.job_id = j.id
WHERE j.closed_at IS NOT NULL
  AND j.closed_note = 'no longer listed on its board'
  AND s.external_id IS NULL
  AND s.source = ANY (:'full_feed'::text[])
GROUP BY s.source
ORDER BY closed_now DESC;
\echo '--- Repair: self-heals once 4bd9f08 is deployed (the next sighting reopens them).'


\echo ''
\echo '=== Not counted ==='
\echo '--- Watched employers ranked behind unlinked boards, the conversation lock held over'
\echo '--- generation and the follow-ups list missing older messages changed what was shown or'
\echo '--- how long things waited, not what is stored. A posting dropped on every sighting for'
\echo '--- an identity-key clash shows only as a dropped count in section 6 and fetch history.'

ROLLBACK;
