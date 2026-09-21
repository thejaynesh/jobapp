-- Has a guessed job id already merged two different postings into one row?
--
-- `source_job_id` for every board read through `base.jobs_from_listing`
-- (iCIMS, Jobvite, Teamtailor, Y Combinator) used to be "the longest number
-- anywhere in the posting URL", which is the posting id only until a date
-- segment or a tracking parameter has more digits. Two postings that collide
-- on that value are not stored as two jobs: `find_existing_job` layer 2
-- matches on `(source, source_job_id)`, returns the first row, and treats the
-- second posting as another sighting of it — appending its URL and counting it
-- as `merged`. The cycle reports success.
--
-- The extraction is fixed going forward (the digits a path segment starts
-- with, last segment wins). This answers what the old rule already did.
--
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U jobapp -d jobapp -f - < scripts/job_id_collisions.sql

\echo '== ids shared by more than one stored job =========================='
-- Rows here are collisions that still produced two rows anyway, via a
-- different dedupe layer. A floor, not a total: the ones layer 2 absorbed
-- cleanly left no second row to count.
SELECT source,
       source_job_id,
       count(*) AS rows_sharing_the_id,
       array_agg(DISTINCT company) AS companies,
       array_agg(url ORDER BY fetched_at) AS urls
FROM jobs
WHERE source_job_id IS NOT NULL
GROUP BY source, source_job_id
HAVING count(*) > 1
ORDER BY count(*) DESC, source
LIMIT 40;

\echo ''
\echo '== the merges that left one row: several URLs, one job ============='
-- A posting absorbed by layer 2 shows up as an extra entry in `source_urls`
-- whose path disagrees with the row's own `url`. Not proof on its own — a
-- genuine cross-post looks the same — but on the four boards above, two URLs
-- from the *same* host is the shape a collision leaves and a cross-post does
-- not.
SELECT source, source_job_id, company, title,
       url, array_length(source_urls, 1) AS url_count, source_urls
FROM jobs
WHERE source IN ('icims', 'jobvite', 'teamtailor', 'ycombinator')
  AND array_length(source_urls, 1) > 1
ORDER BY array_length(source_urls, 1) DESC
LIMIT 40;

\echo ''
\echo '== how much of each board depends on the guess at all ============='
-- Boards publishing `identifier` in their JobPosting block now use it, so
-- these counts should fall for new rows.
SELECT source,
       count(*) AS jobs,
       count(source_job_id) AS with_an_id,
       count(*) FILTER (WHERE source_job_id ~ '^[0-9]+$') AS purely_numeric_id
FROM jobs
WHERE source IN ('icims', 'jobvite', 'teamtailor', 'ycombinator')
GROUP BY source
ORDER BY source;
