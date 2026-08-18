-- Diagnostic report: what the pipeline actually produced, and where it leaks.
-- Read-only. Run with:
--   docker compose exec -T postgres psql -U jobapp -d jobapp -f - < scripts/db_report.sql > report.txt
-- Focused on the two priorities: description/data quality per source, and
-- where new jobs really come from (vs sources that refetch the same postings).

\pset pager off
\pset footer off

\echo ''
\echo '=== 1. Jobs by status ==='
SELECT status::text, count(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 2. Per-source data quality (the priority) ==='
\echo '--- desc%=has any description, full%=at least 500 chars, avg/med=chars,'
\echo '--- dated%=has posted_at, loc%=has location, apply%=has real apply_url'
SELECT source,
       count(*) AS jobs,
       round(100.0 * count(*) FILTER (WHERE coalesce(length(description),0) > 0) / count(*), 1) AS "desc%",
       round(100.0 * count(*) FILTER (WHERE coalesce(length(description),0) >= 500) / count(*), 1) AS "full%",
       round(avg(coalesce(length(description),0))) AS avg_len,
       (percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(length(description),0)))::int AS med_len,
       round(100.0 * count(posted_at) / count(*), 1) AS "dated%",
       round(100.0 * count(*) FILTER (WHERE coalesce(location,'') <> '') / count(*), 1) AS "loc%",
       round(100.0 * count(apply_url) / count(*), 1) AS "apply%"
FROM jobs
GROUP BY source
ORDER BY jobs DESC;

\echo ''
\echo '=== 2b. Structured detail coverage (extracted per job, after the filter) ==='
\echo '--- read%=details extracted, pay%=states a salary, yrs%=states required years'
SELECT source,
       count(*) AS jobs,
       round(100.0 * count(details_extracted_at) / count(*), 1) AS "read%",
       round(100.0 * count(coalesce(salary_max, salary_min)) / count(*), 1) AS "pay%",
       round(100.0 * count(required_years) / count(*), 1) AS "yrs%",
       round(100.0 * count(employment_type) / count(*), 1) AS "type%",
       round(100.0 * count(*) FILTER (WHERE coalesce(array_length(required_skills, 1), 0) > 0)
             / count(*), 1) AS "skills%",
       count(*) FILTER (WHERE language IS NOT NULL AND language <> 'en') AS non_english
FROM jobs
GROUP BY source
ORDER BY jobs DESC;

\echo ''
\echo '=== 2c. Enrichment passes (newest first) ==='
\echo '--- via columns are ats_api/json_ld/llm/held-page; chars is what was gained'
SELECT started_at, status, attempted, enriched, failed,
       via_ats_api AS ats, via_json_ld AS ld, via_llm AS llm,
       via_landing_html AS held, queued_browser AS browser,
       chars_gained AS chars, requeued_for_matching AS rematch
FROM enrichment_runs
ORDER BY started_at DESC
LIMIT 20;

\echo ''
\echo '=== 3. Per-source funnel: what each source turned into ==='
SELECT source,
       count(*) AS total,
       count(*) FILTER (WHERE status = 'new') AS new,
       count(*) FILTER (WHERE status = 'matched') AS matched,
       count(*) FILTER (WHERE status = 'docs_generated') AS docs,
       count(*) FILTER (WHERE status = 'filtered_out') AS filtered,
       round(avg(llm_score)::numeric, 1) AS avg_score
FROM jobs
GROUP BY source
ORDER BY matched DESC, total DESC;

\echo ''
\echo '=== 4. Why jobs were filtered out ==='
SELECT filter_reason, count(*)
FROM jobs WHERE status = 'filtered_out'
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 4b. Data-quality filter reasons, by source ==='
SELECT source, filter_reason, count(*)
FROM jobs
WHERE filter_reason IN ('no_description', 'few_skills', 'title_mismatch', 'location')
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 30;

\echo ''
\echo '=== 5. LLM score distribution (scored jobs) ==='
SELECT (floor(llm_score / 10) * 10)::int AS score_decade, count(*)
FROM jobs WHERE llm_score IS NOT NULL
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 6. Which provider/model scored them ==='
SELECT coalesce(matched_by, '(unset)') AS scored_by, count(*)
FROM jobs WHERE llm_score IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 7. LLM calls, last 7 days ==='
SELECT stage, provider, ok, count(*) AS calls, round(avg(duration_ms)) AS avg_ms
FROM llm_calls
WHERE created_at > now() - interval '7 days'
GROUP BY 1, 2, 3
ORDER BY calls DESC
LIMIT 30;

\echo ''
\echo '=== 7b. Recent LLM failures ==='
SELECT created_at::timestamp(0) AS at, stage, provider, left(error, 120) AS error
FROM llm_calls WHERE NOT ok
ORDER BY created_at DESC
LIMIT 15;

\echo ''
\echo '=== 8. Fetch runs, last 10 ==='
SELECT started_at::timestamp(0) AS started, status,
       duration_seconds::int AS secs, fetched, inserted, merged, skipped, stale,
       links_attempted AS l_try, links_resolved AS l_ok, boards_polled AS boards
FROM fetch_runs
ORDER BY started_at DESC
LIMIT 10;

\echo ''
\echo '=== 9. Source contribution across the last 20 runs ==='
SELECT source, sum(fetched) AS fetched, sum(inserted) AS new_jobs,
       sum(merged) AS merged, sum(stale) AS stale,
       max(left(errors[1], 90)) AS sample_error
FROM fetch_source_runs
WHERE run_id IN (SELECT id FROM fetch_runs ORDER BY started_at DESC LIMIT 20)
GROUP BY source
ORDER BY new_jobs DESC, fetched DESC;

\echo ''
\echo '=== 9b. Source status over those runs ==='
SELECT source, status, count(*)
FROM fetch_source_runs
WHERE run_id IN (SELECT id FROM fetch_runs ORDER BY started_at DESC LIMIT 20)
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

\echo ''
\echo '=== 10. Board registry by ATS ==='
SELECT ats, count(*) AS boards, count(*) FILTER (WHERE active) AS active,
       sum(total_job_count) AS jobs_alltime
FROM company_boards
GROUP BY ats
ORDER BY jobs_alltime DESC NULLS LAST;

\echo ''
\echo '=== 10b. Top 15 producing boards ==='
SELECT ats, slug, left(coalesce(company, ''), 24) AS company,
       total_job_count AS jobs, active
FROM company_boards
ORDER BY total_job_count DESC
LIMIT 15;

\echo ''
\echo '=== 11. Aggregator links still unresolved ==='
SELECT count(*) FILTER (WHERE url ~* 'adzuna|jooble|careerjet|indeed\.com') AS aggregator_urls,
       count(*) FILTER (WHERE url ~* 'adzuna|jooble|careerjet|indeed\.com'
                        AND apply_url IS NULL) AS still_unresolved
FROM jobs;

\echo ''
\echo '=== 12. Browser harvest yield, per site (extension) ==='
\echo '--- zero rows means no harvest toggle has ever been ticked'
SELECT source AS harvested_from, count(*) AS jobs,
       count(*) FILTER (WHERE coalesce(length(description), 0) > 0) AS with_desc,
       count(coalesce(salary_max, salary_min)) AS with_pay,
       min(fetched_at)::date AS first, max(fetched_at)::date AS last
FROM jobs WHERE source LIKE '%\_harvest'
GROUP BY source ORDER BY jobs DESC;

\echo ''
\echo '=== 13. Browser agent tasks ==='
SELECT kind, status, count(*)
FROM browser_tasks
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

\echo ''
\echo '=== 14. Applications ==='
SELECT status::text, count(*) FROM applications GROUP BY 1 ORDER BY 2 DESC;
SELECT generation_status, count(*) FROM applications GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== 15. Generated documents ==='
SELECT doc_type::text, count(*) AS versions,
       count(*) FILTER (WHERE is_current) AS current
FROM application_documents
GROUP BY 1;

\echo ''
\echo '=== 16. Jobs per day, last 14 days ==='
SELECT fetched_at::date AS day, count(*) AS fetched,
       count(*) FILTER (WHERE status IN ('matched', 'docs_generated')) AS matched
FROM jobs
WHERE fetched_at > now() - interval '14 days'
GROUP BY 1
ORDER BY 1 DESC;

\echo ''
\echo '=== 17. Sample: recent jobs lost to missing/thin descriptions ==='
SELECT source, left(company, 20) AS company, left(title, 40) AS title,
       coalesce(length(description), 0) AS len, filter_reason
FROM jobs
WHERE filter_reason IN ('no_description', 'few_skills')
ORDER BY fetched_at DESC
LIMIT 20;

\echo ''
\echo '=== 18. Dedupe sanity ==='
SELECT count(*) AS jobs, count(DISTINCT dedupe_hash) AS distinct_hashes FROM jobs;

\echo ''
\echo '=== 19. Outreach + interview corpus counts ==='
SELECT (SELECT count(*) FROM contacts) AS contacts,
       (SELECT count(*) FROM outreach_messages) AS messages,
       (SELECT count(*) FROM outreach_messages WHERE status = 'sent') AS sent,
       (SELECT count(*) FROM interview_reports) AS interview_reports;
