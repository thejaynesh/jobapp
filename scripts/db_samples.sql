-- Row-level samples: actual extracted values, so extraction quality can be
-- judged by reading them — not just counted. Read-only. Run with:
--   docker compose exec -T postgres psql -U jobapp -d jobapp -f - < scripts/db_samples.sql > samples.txt
--
-- Companion to db_report.sql (the aggregates). This one answers: are the
-- URLs real apply pages, are descriptions clean text or HTML soup, do
-- locations/titles parse sanely, did link resolution land somewhere useful.

\pset pager off
\pset footer off

\echo ''
\echo '=== A. Three most recent jobs per source (full extracted values) ==='
\x on
SELECT source,
       title,
       company,
       location,
       is_remote,
       experience_level,
       posted_at::timestamp(0) AS posted_at,
       fetched_at::timestamp(0) AS fetched_at,
       status::text AS status,
       filter_reason,
       llm_score,
       left(url, 160) AS url,
       left(coalesce(apply_url, ''), 160) AS apply_url,
       coalesce(length(description), 0) AS desc_len,
       left(regexp_replace(coalesce(description, ''), '\s+', ' ', 'g'), 500) AS desc_first_500
FROM (
    SELECT *, row_number() OVER (PARTITION BY source ORDER BY fetched_at DESC) AS rn
    FROM jobs
) ranked
WHERE rn <= 3
ORDER BY source, rn;
\x off

\echo ''
\echo '=== B. Link resolution: what the aggregator URL became ==='
SELECT source,
       left(url, 90) AS original_url,
       left(apply_url, 90) AS resolved_apply_url
FROM jobs
WHERE apply_url IS NOT NULL AND apply_url <> url
ORDER BY fetched_at DESC
LIMIT 20;

\echo ''
\echo '=== C. Suspicious URLs (not http, or landed back on an aggregator) ==='
SELECT source, left(url, 80) AS url, left(coalesce(apply_url, ''), 80) AS apply_url
FROM jobs
WHERE url !~* '^https?://'
   OR apply_url ~* 'adzuna|jooble|careerjet|indeed\.com|linkedin\.com|glassdoor'
ORDER BY fetched_at DESC
LIMIT 15;

\echo ''
\echo '=== D. Descriptions that look like markup, not text ==='
SELECT count(*) AS html_ish_total FROM jobs
WHERE description ~ '<(p|div|ul|li|br|span|strong|b|h[1-6])[ >/]'
   OR description ~ '&(amp|lt|gt|nbsp|#\d+);';
SELECT source, count(*) AS html_ish
FROM jobs
WHERE description ~ '<(p|div|ul|li|br|span|strong|b|h[1-6])[ >/]'
   OR description ~ '&(amp|lt|gt|nbsp|#\d+);'
GROUP BY source ORDER BY 2 DESC;
\x on
SELECT source, left(company, 30) AS company, left(title, 50) AS title,
       left(regexp_replace(coalesce(description, ''), '\s+', ' ', 'g'), 400) AS desc_sample
FROM jobs
WHERE description ~ '<(p|div|ul|li|br|span|strong|b|h[1-6])[ >/]'
   OR description ~ '&(amp|lt|gt|nbsp|#\d+);'
ORDER BY fetched_at DESC
LIMIT 5;
\x off

\echo ''
\echo '=== E. Location strings as they arrive (top 30 by frequency) ==='
SELECT coalesce(nullif(location, ''), '(empty)') AS location, count(*)
FROM jobs
GROUP BY 1 ORDER BY 2 DESC
LIMIT 30;

\echo ''
\echo '=== F. Titles that look wrong (very short, very long, or no letters) ==='
SELECT source, left(title, 90) AS title, length(title) AS len
FROM jobs
WHERE length(title) < 5 OR length(title) > 90 OR title !~ '[a-zA-Z]'
ORDER BY fetched_at DESC
LIMIT 15;

\echo ''
\echo '=== G. What the matcher extracted on recent matches ==='
\x on
SELECT left(title, 60) AS title, company, llm_score, matched_by,
       matched_skills, missing_skills,
       left(llm_reasoning, 300) AS reasoning
FROM jobs
WHERE status IN ('matched', 'docs_generated') AND llm_score IS NOT NULL
ORDER BY fetched_at DESC
LIMIT 6;
\x off

\echo ''
\echo '=== H. Recent filtered jobs: reason vs what the row actually holds ==='
\x on
SELECT source, left(title, 60) AS title, filter_reason,
       left(filter_detail, 200) AS filter_detail,
       coalesce(length(description), 0) AS desc_len,
       left(regexp_replace(coalesce(description, ''), '\s+', ' ', 'g'), 240) AS desc_sample
FROM jobs
WHERE status = 'filtered_out' AND filter_reason IS NOT NULL
ORDER BY fetched_at DESC
LIMIT 8;
\x off

\echo ''
\echo '=== I. Browser agent: last 10 tasks with payload and outcome ==='
\x on
SELECT kind, status, attempts,
       created_at::timestamp(0) AS created,
       left(coalesce(payload->>'url', payload::text), 120) AS asked_for,
       left(coalesce(result->>'landed_on', result->>'via', ''), 120) AS outcome,
       left(coalesce(result->'ingest'->>'stored', ''), 20) AS ingested,
       left(coalesce(error, ''), 160) AS error
FROM browser_tasks
ORDER BY created_at DESC
LIMIT 10;
\x off

\echo ''
\echo '=== J. Harvested jobs (extension), most recent 5 ==='
\x on
SELECT title, company, location,
       left(url, 120) AS url,
       coalesce(length(description), 0) AS desc_len,
       left(regexp_replace(coalesce(description, ''), '\s+', ' ', 'g'), 300) AS desc_sample
FROM jobs
WHERE source = 'linkedin_harvest'
ORDER BY fetched_at DESC
LIMIT 5;
\x off

\echo ''
\echo '=== K. Discovered/sniffed boards: do the slugs look like companies? ==='
SELECT ats, slug, left(coalesce(company, ''), 30) AS company, origin,
       left(coalesce(source_host, ''), 40) AS learned_from,
       total_job_count AS jobs, active
FROM company_boards
ORDER BY last_seen_at DESC
LIMIT 25;

\echo ''
\echo '=== L. One full LLM matching exchange (prompt tail + reply), most recent ==='
\x on
SELECT created_at::timestamp(0) AS at, stage, provider, model, ok, finish_reason,
       right(regexp_replace(messages::text, '\s+', ' ', 'g'), 600) AS prompt_tail,
       left(regexp_replace(coalesce(response, ''), '\s+', ' ', 'g'), 500) AS response_head
FROM llm_calls
WHERE stage = 'match'
ORDER BY created_at DESC
LIMIT 2;
\x off
