# System Architecture Review

Comprehensive review of logic errors, architectural issues, and correctness
problems across the job application automation system.

Findings are grouped by severity. Each one names the file, describes the
problem, and explains the user-visible consequence.

---

## Bugs (confirmed incorrect behavior)

### 1. Dead fallback string in `linked_auth.py` (line 134)

```python
_note_failure(db, row, f"HTTP {response.status_code}: {detail}"
                       or f"HTTP {response.status_code}")
```

The `or` operates on two f-string operands. The left one always produces a
non-empty string (it starts with `"HTTP "`), so the right branch is
**unreachable**. When `detail` is empty the recorded failure says
`"HTTP 401: "` with a trailing colon and space, rather than a clean
`"HTTP 401"`.

**Fix:** `f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"`

---

### 2. `int(keyword_score)` always yields 0 in the browser overlay (`job_context.py:101`)

```python
def _score(job: Job) -> int | None:
    if job.llm_score is not None:
        return int(job.llm_score)
    if job.keyword_score is not None:
        return int(job.keyword_score)   # keyword_score is 0.0-1.0; int(0.85) == 0
    return None
```

`keyword_score` is a ratio in [0.0, 1.0] (set by `matcher.py:374` as
`matched / len(skills_flat)`). Casting with `int()` truncates everything
below 1.0 to 0. Any job that has a keyword score but no LLM score shows
"0" in the extension overlay instead of a meaningful value or no score.

---

### 3. Seniority penalty detail references the wrong result dict after deep scoring (`matcher.py:1126`)

```python
penalty = " (after a 15-point seniority penalty)" if not llm_result.get(
    "seniority_fit", True) else ""
```

When a job gets deep-scored, `score` is updated from `deep_result`
(line 1089), but the detail message still checks `llm_result` (the first
pass). If the two passes disagree on `seniority_fit`, the displayed reason
either omits the penalty note when one was applied, or claims one when it
was not.

---

### 4. Language-filtered job keeps a non-zero `keyword_score` (`matcher.py:1050-1063`)

When a job passes the keyword filter (line 1030 sets `keyword_score`) and
then detail extraction discovers a foreign language, the job is filtered
out with `llm_score = None` but `keyword_score` retains its value. The
early filter-out path (lines 1015-1028) explicitly zeroes `keyword_score`.
This inconsistency, combined with bug 2, means language-filtered jobs show
"0" in the overlay instead of nothing.

---

### 5. Seniority detail message reads raw setting instead of tunable override (`matcher.py:315`)

The detail text reads `max_years = getattr(settings, "JUNIOR_MAX_YEARS", 3.0)`,
but the actual blocking decision at line 113 uses `tunable(profile_data, "junior_max_years")`.
If a user overrides the threshold in their profile (e.g., to 5.0), the
blocking fires at the profile value but the message reports the environment
value. The user sees "under 3 years" when the actual threshold was 5.

---

## Medium-Severity Issues

### 6. Missing GIN index on `jobs.source_urls`

`deduplication.find_existing_job` (line 123) queries `Job.source_urls.any(url)`.
`ArchivedJob` has a GIN index on `source_urls` (migration 0028), but the
`jobs` table does not. Every dedup check against the main jobs table does a
sequential scan on the JSONB/array column. This runs once per job per fetch
cycle, so with hundreds of thousands of jobs it becomes a meaningful
bottleneck.

---

### 7. Final `db.commit()` failure in `fetch_and_save_jobs` loses the entire cycle (`job_fetcher.py:1237`)

Individual jobs are inserted within savepoints for error isolation, but
the outer `db.commit()` is all-or-nothing. If it fails (serialization
error, connection drop), `db.rollback()` at line 1240 discards every job
from a cycle that may have run for 20+ minutes. There is no partial-save
mechanism.

---

### 8. Redis blip can block the next scheduled fetch for up to 1 hour (`fetch_lock.py:49-65`)

When Redis is unreachable, `acquire()` returns True but stores no token.
On `release()`, the missing token means nothing is deleted from Redis. If
Redis comes back with a stale lock still inside its TTL, the next scheduled
run sees it and skips. The TTL is 3600 seconds — so a brief Redis outage
can block fetching for up to one hour after recovery.

---

### 9. `asyncio.run()` inside `_run_all_adapters` crashes in async contexts (`job_fetcher.py:585`)

`asyncio.run(_run_playwright())` raises `RuntimeError` if called from an
existing event loop. The Celery worker path is fine, but a manual fetch
trigger from a FastAPI async handler would fail. This limits how the fetch
can be invoked.

---

### 10. Mid-cycle commits for board backfill are not transactional with job inserts (`job_fetcher.py:675-724`)

`_maybe_backfill_boards` calls `db.commit()` at line 723, and the board
registry update commits at line 916. If the cycle fails after these
commits but before the final job commit, the backfill is marked done while
its discovered boards are lost to the rollback. The two pieces of state
become inconsistent.

---

### 11. Enrichment stamps all jobs after all fetches; a crash between fetch and commit re-fetches the batch (`enrichment.py:1129-1198`)

All target jobs get `enrichment_attempted_at` set after all HTTP fetches
complete, but before `db.commit()`. If the commit fails, the rollback
undoes the stamps and the next pass re-fetches the same URLs — wasting
bandwidth and potentially triggering rate limits at the job boards.

---

### 12. Liveness checks can falsely close jobs on WAF/challenge pages returning HTTP 200 (`liveness.py:88-122`)

A Cloudflare or WAF challenge page served with status 200 could contain
strings like "this job is no longer available" in a generic error template.
The marker check does not distinguish between a real job page and a
challenge page, so a live posting behind a WAF could be marked as closed.

---

### 13. `_resolve_apply_links` loads all known URLs into memory (`job_fetcher.py:638-647`)

`_known_urls` builds a Python `set` of every URL, source_url, and
apply_url from the entire jobs table. With hundreds of thousands of jobs,
each with multiple URLs, this can reach tens of megabytes. A DB-side
existence check would avoid the memory spike.

---

### 14. No retry for transient adapter failures within a fetch cycle (`job_fetcher.py`)

When a source adapter times out or gets a 500 for one role/country combo,
`_run_combos` catches the exception and moves on. There is no retry for
transient network failures. A momentary hiccup during one adapter's call
silently loses that combo's results for the entire cycle, and the resting
mechanism only handles sources that fail every run.

---

## Low-Severity / Design Concerns

### 15. Title matching is overly permissive with single-word overlap (`matcher.py:48-60`)

`_title_matches_roles` passes if ANY single meaningful word overlaps
between the title and a target role. "Engineering Manager" matches target
"Software Engineer" (both contain "engineer"). "Data Analyst" matches
"Data Engineer" (both contain "data"). This means the title gate lets
through many irrelevant jobs that each cost an LLM scoring call.

The 0.7 SequenceMatcher fallback is also quite loose. This is a deliberate
design tradeoff (broad rather than narrow), but it has a real cost in LLM
spend on irrelevant jobs.

---

### 16. Duplicate application check does a full table scan in Python (`deduplication.py:209-223`)

`find_duplicate_application_job` loads ALL (id, company, title) tuples for
jobs with applications, then checks each one in Python with normalization
and SequenceMatcher. This runs once per newly-matched job during the
scoring loop. As the application count grows, this linear scan becomes
increasingly expensive.

---

### 17. Profile is loaded once for the entire batch chain (`matcher.py:1162-1163`)

`match_all_new_jobs` reads the profile once before the loop. Profile
changes (skills, target roles, min score) made mid-batch don't take
effect. With the self-chaining mechanism, batches run back-to-back for
large backlogs — potentially hundreds of jobs scored against a stale
profile.

---

### 18. `_extract_json_object` brace-counting ignores string contents (`matcher.py:584-600`)

The fallback JSON extractor counts raw `{` and `}` characters without
considering whether they are inside JSON string values. A response with
unbalanced braces in a reasoning string (e.g., `"skills {Python matched"`)
would throw off the depth counter. This only matters when the full
`json.loads` fails and the response has surrounding prose.

---

### 19. Profile blob concurrent write risk (`job_fetcher.py:1045-1050, 1222-1234`)

The fetch cycle deep-copies the profile blob, runs for minutes, then
merges only `_FETCH_CYCLE_KEYS` back. This is careful — but if the fetch
lock fails (Redis was down), two overlapping cycles could both do the
refresh-merge-write, and the last writer wins on the shared keys. Mitigated
by the fetch lock under normal operation.

---

### 20. State abbreviations in `_LOCATION_NOISE` collide with English words (`deduplication.py:39-49`)

The noise set includes state abbreviations that are common words: "in"
(Indiana), "or" (Oregon), "me" (Maine), "co" (Colorado), "id" (Idaho).
For country-first formats without commas ("IN - Indianapolis"), these would
be stripped. The fallback at line 102-105 prevents total erasure, but two
legitimately different locations could normalize to the same string.

---

### 21. Harvest `save_harvested_jobs` single retry covers two-way races but not three-way (`harvest.py:886-917`)

When concurrent extension payloads for the same job arrive, the single
`IntegrityError` retry handles the common two-way race. A three-way race
(three payloads from the same browsing session) could still drop a job,
though this is rare in practice.

---

### 22. `match_budget.save` uses two separate Redis commands (`match_budget.py:70-77`)

`save()` calls `client.hset()` and `client.expire()` as separate
operations. If the process dies between them, the key persists with its old
TTL. Harmless in practice but a Redis pipeline would make this atomic.

---

## Data Model & Integrity Issues

### 23. `Contact.application_id` CASCADE contradicts "can outlive the application" intent (`outreach.py:63-66`)

The comment says "Nullable so a contact can outlive the application", but
the FK uses `ondelete="CASCADE"`. CASCADE deletes the contact when the
application is deleted — the opposite of outliving it. Should be
`ondelete="SET NULL"`. As written, deleting an application silently
destroys all its contacts and their message threads.

---

### 24. Missing indexes on the `jobs` table (models/job.py)

The `jobs` table is missing several indexes that its sibling
`archived_jobs` has:

- **`source_urls` GIN index** (finding 6 above) — the hottest dedup path
- **`status` index** — the most filtered column in the system, used in
  matcher, archive, enrichment, liveness, funnel, and job list queries
- **`(source, source_job_id)` composite index** — dedup layer 2; the
  archived_jobs table has this as `ix_archived_jobs_source_job`
- **`url` index** — queried in job_context and multiple service paths;
  archived_jobs has `ix_archived_jobs_url`
- **`fetched_at` index** — used in ORDER BY across 8+ service files

These missing indexes mean most queries against the jobs table do
sequential scans. At scale this is a serious performance problem.

---

### 25. Missing indexes on `applications.job_id` and `application_documents.application_id`

Neither FK column is indexed. The archive service joins applications to
jobs on every pass, and loading an application's documents navigates
`application_documents.application_id`. Without indexes, both require
full table scans.

---

### 26. `Application.job_id` and `ApplicationDocument.application_id` FKs missing ondelete policy (`application.py`)

Both FKs lack an explicit `ondelete` clause, defaulting to RESTRICT.
Every other FK in the system explicitly declares its cascade policy. A
code path that deletes a Job or Application without first checking for
children will get a database error rather than a cascading cleanup.

---

### 27. `generation_status` state machine has no DB-level guard

The `generation_status` transitions (idle -> generating -> done/failed) are
managed purely in application code. No row-level lock or conditional
update prevents two workers from transitioning the same application
simultaneously. `doc_refresh.py` sets `generation_status = "generating"`
without a `WHERE generation_status = 'idle'` guard.

---

### 28. Deduplication race condition on layers 1 and 2 (`deduplication.py:115-138`)

`find_existing_job` performs three separate SELECT queries with no
row-level locking. Between these queries and the caller's INSERT, a
concurrent fetch could insert the same job. Layer 3 (dedupe_hash) is
protected by a UNIQUE constraint, but layers 1 (URL in source_urls) and 2
(source + source_job_id) have no unique constraints. Two concurrent
fetchers could both pass all three checks and both insert duplicates.

---

## Architecture Notes (not bugs, but worth knowing)

- **Auth is middleware-based, not per-route.** This is intentionally
  fail-closed — adding a new router doesn't accidentally expose it. The
  design is sound.

- **Celery late acks with `task_reject_on_worker_lost=True`** means tasks
  are redelivered on worker death. Combined with the Redis lock pattern,
  this prevents most lost-work scenarios. The generation sweeper catches
  the rest.

- **The two-pass scoring (fast model + deep second opinion)** is
  well-designed for its purpose. The deep chain now walks all providers
  instead of just the first, which fixes the "40 consecutive failures"
  problem documented in the comments.

- **Enrichment's multi-method approach** (ATS API, JSON-LD, LLM, browser)
  with the re-scoring hook for description growth is thoughtful. The
  `_worth_rescoring` gate prevents wasted calls on jobs where the new
  description wouldn't change the verdict.

- **The deduplication layers** (URL, source+ID, content hash) with the
  normalization for company suffixes, title abbreviations, and location
  noise are thorough. The cross-post detection at match time catches
  near-misses the hash layer can't.
