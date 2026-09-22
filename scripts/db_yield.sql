-- Is each kind of work this system does actually paying for itself?
-- Read-only. Run with:
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U jobapp -d jobapp -f - < scripts/db_yield.sql > yield.txt
--
-- The other scripts here answer "why is this broken" (db_board, db_pulse,
-- db_learn) or "what did the pipeline produce" (db_report). This one asks a
-- different question, and it is the one that decides where effort goes:
--
--     for each thing we spend requests, LLM calls or wall-clock on,
--     how often does it change the answer?
--
-- Every section is a ratio with a denominator, because a numerator alone
-- cannot tell you whether to stop. "Enrichment gained 4.2 million characters"
-- sounds like success; "enrichment attempted 38,000 jobs and 31,000 were
-- unchanged" is the same run described usefully.
--
-- Three things are already self-correcting, and are NOT re-measured here
-- because a query that duplicates a live feedback loop invites you to act on
-- it twice:
--
--   * enrichment per host  — `enrichment_runs.host_outcomes` records
--     attempts and successes per host, and `for_server` skips a host whose
--     rate is bad over a fortnight. Section 2 looks at per *source* instead,
--     which nothing acts on yet.
--   * discovered boards    — `company_board.consecutive_empty` retires a board
--     after `ATS_BOARD_MAX_EMPTY_CYCLES`. Section 7 asks the opposite
--     question: which retired ones deserve another look.
--   * failing sources      — `SOURCE_REST_AFTER_FAILURES` rests a source that
--     errors repeatedly. Section 1 is about sources that *succeed* and
--     contribute nothing, which is invisible to that.
--
-- Nothing here selects a description, a document or a payload. Safe to paste.

\pset pager off
\pset footer off
\timing off

\echo ''
\echo '################ EFFICIENCY: what work is not paying off ################'

\echo ''
\echo '=== 1. Fetch: which sources return volume but contribute nothing new ==='
\echo '--- inserted/fetched is the only number that matters. A source at 0% is'
\echo '--- either fully deduplicated by others (drop it) or broken (check errors).'
SELECT source,
       count(*)                                   AS runs,
       sum(fetched)                               AS fetched,
       sum(inserted)                              AS inserted,
       sum(merged)                                AS merged,
       round(100.0 * sum(inserted) / NULLIF(sum(fetched), 0), 1) AS pct_new,
       round(100.0 * sum(merged)   / NULLIF(sum(fetched), 0), 1) AS pct_merged,
       count(*) FILTER (WHERE status <> 'ok')     AS bad_runs
FROM fetch_source_runs r
JOIN fetch_runs f ON f.id = r.run_id
WHERE f.started_at > now() - interval '30 days'
GROUP BY source
HAVING sum(fetched) > 0
ORDER BY sum(inserted) ASC;

\echo ''
\echo '=== 2. Enrichment: which sources arrive complete already ==='
\echo '--- THE QUESTION THIS SCRIPT WAS WRITTEN FOR. A source whose postings'
\echo '--- already carry full descriptions costs a request per job to learn'
\echo '--- nothing. `grew` counts jobs whose description actually improved'
\echo '--- after we went back for it (description_updated_at is only stamped on'
\echo '--- a meaningful change). Low pct_grew on high attempts = stop enriching'
\echo '--- that source.'
SELECT source,
       count(*)                                                     AS jobs,
       count(*) FILTER (WHERE enrichment_attempted_at IS NOT NULL)  AS attempted,
       count(*) FILTER (WHERE description_updated_at IS NOT NULL)   AS grew,
       round(100.0 * count(*) FILTER (WHERE description_updated_at IS NOT NULL)
             / NULLIF(count(*) FILTER (WHERE enrichment_attempted_at IS NOT NULL), 0), 1)
                                                                    AS pct_grew,
       round(avg(length(description)) FILTER (WHERE description IS NOT NULL))
                                                                    AS avg_chars,
       count(*) FILTER (WHERE description IS NULL
                          OR length(description) < 1500)            AS still_thin
FROM jobs
GROUP BY source
HAVING count(*) FILTER (WHERE enrichment_attempted_at IS NOT NULL) > 50
ORDER BY pct_grew ASC NULLS FIRST;

\echo ''
\echo '=== 3. Enrichment: which extraction path earns its cost ==='
\echo '--- via_llm is the expensive one. If json_ld or ats_api covers most of'
\echo '--- the volume, the LLM path is a fallback and should stay one; if the'
\echo '--- LLM path dominates, that is the bill worth attacking.'
SELECT sum(attempted)                AS attempted,
       sum(enriched)                 AS enriched,
       sum(unchanged)                AS unchanged,
       sum(failed)                   AS failed,
       round(100.0 * sum(unchanged) / NULLIF(sum(attempted), 0), 1) AS pct_wasted,
       sum(via_ats_api)              AS via_ats_api,
       sum(via_json_ld)              AS via_json_ld,
       sum(via_landing_html)         AS via_landing_html,
       sum(via_llm)                  AS via_llm,
       sum(queued_browser)           AS queued_browser,
       sum(chars_gained)             AS chars_gained,
       round(sum(chars_gained)::numeric / NULLIF(sum(enriched), 0)) AS chars_per_win
FROM enrichment_runs
WHERE started_at > now() - interval '30 days';

\echo ''
\echo '=== 4. The second opinion: does the deep pass ever change the verdict ==='
\echo '--- DEEP_MATCH_BAND_LOW..HIGH decides which jobs get a second, paid'
\echo '--- scoring call. It is worth its money only when it moves a job across'
\echo '--- min_match_score. `flipped` counts the ones where pass 1 and pass 2'
\echo '--- disagree about accept/reject; if that is a small fraction, narrow'
\echo '--- the band and most of those calls stop.'
SELECT count(*)                                                       AS deep_scored,
       round(avg(llm_score))                                          AS avg_first,
       round(avg(llm_score_deep))                                      AS avg_deep,
       round(avg(abs(llm_score_deep - llm_score)), 1)                  AS avg_abs_change,
       count(*) FILTER (WHERE (llm_score >= 70) <> (llm_score_deep >= 70)) AS flipped_at_70,
       round(100.0 * count(*) FILTER (WHERE (llm_score >= 70) <> (llm_score_deep >= 70))
             / NULLIF(count(*), 0), 1)                                 AS pct_flipped
FROM jobs
WHERE llm_score IS NOT NULL AND llm_score_deep IS NOT NULL;

\echo ''
\echo '--- and the same question by first-pass score, to size the band honestly.'
\echo '--- Buckets where nothing ever flips are buckets to stop paying for.'
SELECT width_bucket(llm_score, 0, 100, 10) * 10 - 10 AS score_from,
       count(*)                                       AS deep_scored,
       count(*) FILTER (WHERE (llm_score >= 70) <> (llm_score_deep >= 70)) AS flipped,
       round(100.0 * count(*) FILTER (WHERE (llm_score >= 70) <> (llm_score_deep >= 70))
             / NULLIF(count(*), 0), 1)                AS pct_flipped
FROM jobs
WHERE llm_score IS NOT NULL AND llm_score_deep IS NOT NULL
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 5. Re-scoring: is anything actually being re-judged ==='
\echo '--- After the §1.1 fix a job is only requeued when its description grew'
\echo '--- since the score it carries. More than one score row per job with the'
\echo '--- same description_chars means work that changed nothing.'
SELECT scores_per_job,
       count(*) AS jobs
FROM (SELECT job_id, count(*) AS scores_per_job
      FROM job_scores GROUP BY job_id) t
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '--- score rows that were written against an identical description length'
\echo '--- as the previous one for the same job: re-scoring that learned nothing.'
SELECT count(*) AS redundant_score_rows
FROM (SELECT job_id, description_chars,
             lag(description_chars) OVER (PARTITION BY job_id ORDER BY created_at) AS prev
      FROM job_scores) t
WHERE prev IS NOT NULL AND description_chars = prev;

\echo ''
\echo '=== 6. Liveness: which sources postings actually close ==='
\echo '--- LIVENESS_RECHECK_DAYS re-checks every matched posting on a clock. A'
\echo '--- source whose jobs are never found closed is a source not worth'
\echo '--- re-checking; one with a high rate deserves a shorter interval.'
SELECT source,
       count(*) FILTER (WHERE liveness_checked_at IS NOT NULL) AS checked,
       count(*) FILTER (WHERE closed_at IS NOT NULL)           AS found_closed,
       round(100.0 * count(*) FILTER (WHERE closed_at IS NOT NULL)
             / NULLIF(count(*) FILTER (WHERE liveness_checked_at IS NOT NULL), 0), 1)
                                                               AS pct_closed
FROM jobs
GROUP BY source
HAVING count(*) FILTER (WHERE liveness_checked_at IS NOT NULL) > 20
ORDER BY pct_closed ASC;

\echo ''
\echo '################ YIELD: where more jobs would come from ################'

\echo ''
\echo '=== 7. Boards retired as empty — which deserve another look ==='
\echo '--- consecutive_empty retires a board automatically. But a board that'
\echo '--- once produced and then went quiet is a different case from one that'
\echo '--- never produced at all: the first may just have had no openings.'
SELECT ats,
       count(*)                                          AS boards,
       count(*) FILTER (WHERE active)                    AS active,
       count(*) FILTER (WHERE NOT active
                          AND total_job_count > 0)       AS retired_but_once_gave,
       count(*) FILTER (WHERE NOT active
                          AND total_job_count = 0)       AS never_gave_anything,
       sum(total_job_count)                              AS total_jobs
FROM company_boards
GROUP BY ats ORDER BY total_jobs DESC NULLS LAST;

\echo ''
\echo '--- the individual boards worth reviving: retired, but productive before.'
SELECT ats, slug, company, total_job_count, consecutive_empty,
       inactive_reason, last_fetched_at::date
FROM company_boards
WHERE NOT active AND total_job_count > 0
ORDER BY total_job_count DESC LIMIT 25;

\echo ''
\echo '=== 8. Where the title gate is rejecting whole sources ==='
\echo '--- A source whose jobs are almost all title_mismatch is not useless —'
\echo '--- it is aimed wrong. Either its queries need changing or its postings'
\echo '--- are for a different market. Distinguishes "bad source" from'
\echo '--- "bad search".'
SELECT source,
       count(*)                                                        AS jobs,
       count(*) FILTER (WHERE filter_reason = 'title_mismatch')         AS title_mismatch,
       round(100.0 * count(*) FILTER (WHERE filter_reason = 'title_mismatch')
             / NULLIF(count(*), 0), 1)                                 AS pct_title_mismatch,
       count(*) FILTER (WHERE status = 'matched')                       AS matched,
       round(100.0 * count(*) FILTER (WHERE status = 'matched')
             / NULLIF(count(*), 0), 1)                                  AS pct_matched
FROM jobs
GROUP BY source
HAVING count(*) > 100
ORDER BY pct_matched ASC;

\echo ''
\echo '=== 9. Sources that have stopped contributing ==='
\echo '--- Compares the last week against the four before it. A source falling'
\echo '--- to zero is usually an expired key or a changed page, and it is'
\echo '--- invisible on a total that other sources hold up.'
WITH windows AS (
  SELECT r.source,
         sum(r.inserted) FILTER (WHERE f.started_at > now() - interval '7 days')  AS last_7d,
         sum(r.inserted) FILTER (WHERE f.started_at BETWEEN now() - interval '35 days'
                                                        AND now() - interval '7 days') AS prior_28d
  FROM fetch_source_runs r
  JOIN fetch_runs f ON f.id = r.run_id
  WHERE f.started_at > now() - interval '35 days'
  GROUP BY r.source
)
SELECT source, last_7d, prior_28d,
       round(prior_28d / 4.0, 1) AS prior_weekly_avg
FROM windows
WHERE coalesce(prior_28d, 0) > 0
ORDER BY (coalesce(last_7d, 0)::numeric / NULLIF(prior_28d / 4.0, 0)) ASC NULLS FIRST;

\echo ''
\echo '=== 10. Link resolution: does following redirects reveal new boards ==='
\echo '--- RESOLVE_APPLY_LINKS spends LINK_RESOLVE_MAX_PER_CYCLE requests a run'
\echo '--- to turn aggregator redirects into employer URLs. Its payoff is'
\echo '--- boards_discovered; if that is flat while links_resolved is large,'
\echo '--- the budget is buying apply URLs the user already had.'
SELECT sum(links_attempted) AS attempted,
       sum(links_resolved)  AS resolved,
       sum(links_failed)    AS failed,
       round(100.0 * sum(links_resolved) / NULLIF(sum(links_attempted), 0), 1) AS pct_resolved,
       sum(boards_discovered) AS boards_discovered,
       sum(boards_sniffed)    AS boards_sniffed,
       round(sum(links_resolved)::numeric
             / NULLIF(sum(boards_discovered), 0)) AS links_per_board_found
FROM fetch_runs
WHERE started_at > now() - interval '30 days';

\echo ''
\echo '=== 11. Structured data completeness per source ==='
\echo '--- What we can actually filter on. A source with jobs but no salary,'
\echo '--- no period and no posted date is a source you cannot search.'
SELECT source,
       count(*)                                                       AS jobs,
       round(100.0 * count(*) FILTER (WHERE posted_at IS NOT NULL)
             / count(*), 1)                                           AS pct_dated,
       round(100.0 * count(*) FILTER (WHERE coalesce(salary_max, salary_min) IS NOT NULL)
             / count(*), 1)                                           AS pct_any_pay,
       round(100.0 * count(*) FILTER (WHERE salary_annual_min IS NOT NULL)
             / count(*), 1)                                           AS pct_comparable_pay,
       round(100.0 * count(*) FILTER (WHERE required_years IS NOT NULL)
             / count(*), 1)                                           AS pct_years,
       round(100.0 * count(*) FILTER (WHERE apply_url IS NOT NULL)
             / count(*), 1)                                           AS pct_apply_url
FROM jobs
GROUP BY source
HAVING count(*) > 100
ORDER BY jobs DESC;

\echo ''
\echo '=== 12. Archive: is anything being re-fetched after being retired ==='
\echo '--- The tombstone exists so an archived posting is never re-inserted and'
\echo '--- re-scored. A row here means a live job sharing a dedupe_hash with an'
\echo '--- archived one, which would mean the guard is being missed.'
SELECT count(*) AS live_jobs_matching_an_archived_hash
FROM jobs j
WHERE EXISTS (SELECT 1 FROM archived_jobs a WHERE a.dedupe_hash = j.dedupe_hash);

\echo ''
\echo '=== 13. What the matcher spends its verdicts on ==='
\echo '--- DESCRIPTION_DEPENDENT_REASONS are the verdicts enrichment exists to'
\echo '--- revisit. A large no_description or few_skills population with full'
\echo '--- descriptions now means jobs waiting on a re-score that is not coming.'
SELECT coalesce(filter_reason, '(not filtered)') AS filter_reason,
       count(*)                                  AS jobs,
       count(*) FILTER (WHERE description IS NOT NULL
                          AND length(description) >= 1500) AS now_has_full_text
FROM jobs
WHERE status = 'filtered_out' OR filter_reason IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== Done. Read sections 1, 2 and 4 first — those are where the money is.'
