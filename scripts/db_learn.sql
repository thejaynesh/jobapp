-- Why "Work it out" and "Learn" are producing rejections. Read-only. Run with:
--   docker compose exec -T postgres psql -U jobapp -d jobapp -f - < scripts/db_learn.sql > learn.txt
--
-- Both learners fail the same way from the outside: a button press, a
-- rejection, and a one-line reason with no way to tell whose fault it was.
-- There are four candidates and the reason alone distinguishes none of them:
--
--   1. the evidence never reached the model  — the extension collected nothing
--      worth sending, so the model was asked to name a control it was never
--      shown
--   2. the model answered badly              — wrong mode, missing key, prose
--      instead of JSON
--   3. the model answered well and we refused it — validation is stricter than
--      it needs to be, which is the failure that looks exactly like (2)
--   4. it validated and then did nothing     — retired later by note_outcome
--
-- So this dumps the evidence, the proposal and the reply side by side. Section
-- C is the one that matters: the controls in it are everything the model had
-- to work with, and a rejection is only the model's fault if the answer was
-- there to be found.
--
-- Nothing here is a job description or a payload — it is navigation controls,
-- recipes and model replies. Safe to paste.

\pset pager off
\pset footer off

\echo ''
\echo '=== A. Crawl recipes: every proposal, and what became of it ==='
\x on
SELECT host,
       status,
       recipe ->> 'mode' AS mode,
       coalesce(recipe ->> 'selector', '') AS selector,
       coalesce(recipe ->> 'page_param', '') AS page_param,
       coalesce(recipe ->> 'scroll_passes', '') AS scroll_passes,
       coalesce(recipe ->> 'max_pages', '') AS max_pages,
       coalesce(recipe ->> 'page_size', '') AS page_size,
       coalesce(recipe ->> 'page_base', '') AS page_base,
       tries,
       best_pages,
       coalesce(model, '') AS model,
       -- The rejection reason, in full. This is the sentence the panel shows.
       coalesce(note, '') AS note,
       recipe::text AS full_recipe,
       created_at::timestamp(0) AS created_at
FROM crawl_recipes
ORDER BY created_at DESC
LIMIT 40;
\x off

\echo ''
\echo '=== B. Which rejection reason, how often ==='
-- Grouped on the leading phrase, because the reasons interpolate a selector or
-- a label and would otherwise never repeat.
SELECT count(*) AS n,
       left(coalesce(note, '(none)'), 70) AS reason_starts_with,
       string_agg(DISTINCT host, ', ') AS hosts
FROM crawl_recipes
WHERE status = 'rejected'
GROUP BY 2
ORDER BY n DESC;

\echo ''
\echo '=== C. What the page actually offered (the model saw exactly this) ==='
\echo '--- C1. one row per stored sample: how much evidence there was ---'
SELECT host,
       jsonb_array_length(coalesce(evidence -> 'controls', '[]'::jsonb)) AS controls,
       coalesce(evidence #>> '{scroll,passes}', '') AS passes,
       coalesce(evidence #>> '{scroll,batches}', '') AS batches,
       coalesce(evidence #>> '{scroll,doc_height}', '') AS doc_height,
       pages_reached,
       batches AS visit_batches,
       coalesce(evidence -> 'query', '{}'::jsonb)::text AS query_params,
       created_at::timestamp(0) AS created_at
FROM crawl_samples
ORDER BY host, created_at DESC;

\echo ''
\echo '--- C2. every control, one row each. A host with no rows here could'
\echo '        only ever have been answered "scroll". ---'
SELECT s.host,
       c ->> 'tag' AS tag,
       left(coalesce(c ->> 'text', ''), 30) AS text,
       left(coalesce(c ->> 'aria', ''), 40) AS aria_label,
       left(coalesce(c ->> 'title', ''), 30) AS title,
       coalesce(c ->> 'rel', '') AS rel,
       coalesce(c ->> 'role', '') AS role,
       left(coalesce(c ->> 'cls', ''), 50) AS class,
       left(coalesce(c ->> 'id', ''), 30) AS id,
       left(coalesce(c ->> 'testid', ''), 40) AS testid,
       coalesce(c ->> 'disabled', '') AS disabled,
       left(coalesce(c ->> 'href', ''), 60) AS href
FROM crawl_samples s
CROSS JOIN LATERAL jsonb_array_elements(
    coalesce(s.evidence -> 'controls', '[]'::jsonb)
) AS c
WHERE s.created_at = (
    SELECT max(created_at) FROM crawl_samples s2 WHERE s2.host = s.host
)
ORDER BY s.host, tag, text;

\echo ''
\echo '--- C3. the newest sample per host, verbatim. What the prompt contained. ---'
\x on
SELECT DISTINCT ON (host)
       host,
       left(coalesce(source_url, ''), 200) AS source_url,
       coalesce(note, '') AS note,
       jsonb_pretty(evidence) AS evidence
FROM crawl_samples
ORDER BY host, created_at DESC;
\x off

\echo ''
\echo '=== D. Harvest recipes: the other learner, same questions ==='
\x on
SELECT host,
       status,
       jobs_found,
       samples_tried,
       coalesce(model, '') AS model,
       coalesce(note, '') AS note,
       jsonb_pretty(recipe) AS recipe,
       created_at::timestamp(0) AS created_at
FROM harvest_recipes
ORDER BY created_at DESC
LIMIT 25;
\x off

\echo ''
\echo '=== E. Harvest samples: shape only, not the job data ==='
-- The key names at each level are what a recipe is written against, and they
-- are the whole question when a payload "cannot be read". The values are job
-- listings and are left out on purpose.
SELECT host,
       count(*) AS samples,
       sum(found) AS jobs_found,
       max(bytes) AS biggest_bytes,
       string_agg(DISTINCT coalesce(note, ''), ' | ') AS notes
FROM harvest_samples
GROUP BY host
ORDER BY samples DESC;

\echo ''
\echo '--- E1. top-level keys of the newest payload per host ---'
SELECT DISTINCT ON (host)
       host,
       coalesce(note, '') AS note,
       found,
       CASE jsonb_typeof(payload)
           WHEN 'object' THEN
               (SELECT string_agg(k, ', ' ORDER BY k)
                FROM jsonb_object_keys(payload) AS k)
           ELSE jsonb_typeof(payload)
       END AS top_level_keys
FROM harvest_samples
ORDER BY host, created_at DESC;

\echo ''
\echo '=== F. What the model actually replied ==='
-- Rows stamped with the role that made them. Anything older than that change
-- says "unknown" and is matched on its prompt instead.
\x on
SELECT created_at::timestamp(0) AS at,
       stage,
       provider,
       model,
       ok,
       coalesce(finish_reason, '') AS finish_reason,
       coalesce(error, '') AS error,
       completion_tokens,
       -- Truncated because a refusal or a wall of prose is diagnosable from
       -- its first 1500 characters, and a good JSON answer is far shorter.
       left(coalesce(response, ''), 1500) AS response
FROM llm_calls
WHERE stage = 'learn'
   OR messages::text LIKE '%how a job board shows its second page%'
   OR messages::text LIKE '%extraction recipe for job postings%'
ORDER BY created_at DESC
LIMIT 20;
\x off

\echo ''
\echo '=== G. Did the boards these recipes cover actually get anywhere ==='
-- The empirical half. A recipe can validate against a snapshot and still never
-- advance a page, and only the visits can say so.
SELECT host,
       count(*) AS visits,
       sum(CASE WHEN (summary ->> 'pages_done')::int > 1 THEN 1 ELSE 0 END)
           AS got_past_page_one,
       max((summary ->> 'pages_done')::int) AS best_pages,
       max((summary ->> 'batches')::int) AS best_batches,
       -- A string, not a flag: 'timeout', 'skipped', 'passed'.
       string_agg(DISTINCT nullif(summary ->> 'challenge', ''), ', ')
           AS challenges,
       sum(CASE WHEN summary ->> 'rate_limited' = 'true' THEN 1 ELSE 0 END)
           AS rate_limited
FROM agent_events
WHERE kind = 'browse'
  AND created_at > now() - interval '14 days'
  AND summary ? 'pages_done'
GROUP BY host
ORDER BY visits DESC
LIMIT 30;

\echo ''
\echo '=== H. What the reader saw per site (needs the new extension build) ==='
SELECT host,
       sum(CASE WHEN summary ->> 'first' = 'true' THEN 1 ELSE 0 END) AS pages,
       sum(coalesce((summary ->> 'json')::int, 0))    AS json_seen,
       sum(coalesce((summary ->> 'sent')::int, 0))    AS forwarded,
       sum(coalesce((summary ->> 'probed')::int, 0))  AS probed,
       sum(coalesce((summary ->> 'url_no')::int, 0))  AS rejected_on_url
FROM agent_events
WHERE kind = 'read'
  AND created_at > now() - interval '14 days'
GROUP BY host
ORDER BY json_seen DESC
LIMIT 30;
