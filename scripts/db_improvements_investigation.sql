-- Diagnostic queries for system improvements outlined in docs/IMPROVING.md
-- Run with:
--   docker compose exec -T postgres psql -U jobapp -d jobapp -f - < scripts/db_improvements_investigation.sql > improvements_output.txt

\pset pager off
\pset footer off

\echo ''
\echo '=== IMPROVEMENT 0: SALARY PERIOD DEFECT ==='
\echo '--- Identifying jobs that likely state hourly/daily/monthly rates but are stored in annual salary columns'
\echo '--- We look for small numbers in salary_min'
SELECT id, company, title, salary_min, salary_max, salary_currency
FROM jobs
WHERE salary_min IS NOT NULL AND salary_min < 10000
ORDER BY salary_min DESC
LIMIT 20;

\echo ''
\echo '=== IMPROVEMENT 1 & 2: DATA COMPLETENESS ==='
\echo '--- Jobs that reached "matched" status but are missing critical fields'
SELECT source,
       count(*) AS matched_jobs,
       count(*) FILTER (WHERE salary_min IS NULL) AS missing_salary,
       count(*) FILTER (WHERE required_years IS NULL) AS missing_years,
       count(*) FILTER (WHERE coalesce(array_length(required_skills, 1), 0) = 0) AS missing_skills
FROM jobs
WHERE status = 'matched'
GROUP BY source
ORDER BY matched_jobs DESC;

\echo ''
\echo '=== IMPROVEMENT 6 & 8: RECALL PANEL / COMPANY DISCOVERY ==='
\echo '--- Top companies by matched jobs to serve as the initial Recall Panel (40-60 companies)'
SELECT company,
       count(*) AS jobs_count,
       count(*) FILTER (WHERE status = 'matched') AS matched_count
FROM jobs
GROUP BY company
HAVING count(*) FILTER (WHERE status = 'matched') > 0
ORDER BY matched_count DESC, jobs_count DESC
LIMIT 60;

\echo ''
\echo '=== IMPROVEMENT 7: BOARD RECONCILIATION ==='
\echo '--- Comparing what ATS boards report (last_job_count) vs what we hold in the jobs table for that company'
SELECT b.ats,
       b.slug,
       b.company,
       b.last_job_count AS offered,
       (SELECT count(*) FROM jobs j WHERE j.company = b.company) AS held,
       b.last_job_count - (SELECT count(*) FROM jobs j WHERE j.company = b.company) AS gap
FROM company_boards b
WHERE b.last_job_count > 0
ORDER BY gap DESC
LIMIT 50;

\echo ''
\echo '=== IMPROVEMENT 10: PER-SOURCE PAGINATION CEILINGS ==='
\echo '--- Examining sources that might be hitting hardcoded pagination ceilings (large stable volumes)'
SELECT source,
       count(*) AS total_jobs,
       count(DISTINCT date_trunc('day', fetched_at)) AS days_active,
       round(count(*) * 1.0 / NULLIF(count(DISTINCT date_trunc('day', fetched_at)), 0), 1) AS avg_jobs_per_day
FROM jobs
GROUP BY source
ORDER BY avg_jobs_per_day DESC;
