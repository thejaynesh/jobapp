# System Architecture Review — Consolidated Findings

Every finding below was verified against the current source tree. Each
includes what is wrong, why it matters, and how to fix it.

---

## Critical — Resource waste or data loss

### 1. Infinite re-scoring loop burns LLM tokens every 30 minutes

**Files:** `enrichment.py:769-855`, `tasks/enrich.py`

`requeue_settled_verdicts()` runs on every enrichment pass. It selects
`filtered_out` jobs with `filter_reason` in `{"low_score", "few_skills",
"seniority"}` whose description is >= 1500 chars, resets them to
`status = new`, and triggers `match_jobs.delay()`. The matcher re-scores
them, the LLM rejects them again as `low_score`, and 30 minutes later the
same function picks them up and resets them again.

The function never checks whether the description grew *since the last
score*. `JobScore.description_chars` records how many characters the LLM
saw, and `job.description_updated_at` records when the description last
grew. Neither is consulted. Any job scored on its full description and
rejected will be re-scored indefinitely.

**Impact:** Infinite paid LLM calls on the same rejected jobs every 30
minutes, crowding out genuinely new postings in the matching queue.

**Fix:** Compare `len(job.description)` against the most recent
`JobScore.description_chars` for that job, or check that
`description_updated_at` is newer than the last score's `created_at`.
Only requeue jobs whose description actually grew since they were last
evaluated.

---

### 2. Fetch group locks are defeated by the global lock (`tasks/fetch.py:39`)

**File:** `tasks/fetch.py:37-51`

The architecture splits fetching into three groups (`api`, `boards`,
`browser`) with separate lock keys so fast API adapters don't wait behind
slow Playwright jobs. But line 39 forces every group to also acquire the
global `LOCK_KEY`:

```python
keys = [LOCK_KEY] if group in (None, "all") else [GROUP_LOCK_KEYS[group], LOCK_KEY]
```

When the slow `browser` tier runs and holds `LOCK_KEY`, the hourly `api`
tier tries to acquire it, fails, and skips. The group separation is
completely negated.

**Impact:** Fast API sources are blocked by slow browser fetches, defeating
the entire purpose of the three-tier split.

**Fix:** Individual groups should only acquire their own
`GROUP_LOCK_KEYS[group]`. Only an `"all"` / manual run should hold the
global `LOCK_KEY`. Add a check against the global key (non-blocking) so a
group run yields to a manual full run, but not to other groups.

---

### 3. Seniority prefilter bypassed for non-junior candidates (`matcher.py:112-118`)

**File:** `matcher.py:92-128`

```python
total_years = _total_years(profile_data.get("experience", []))
if total_years >= tunable(profile_data, "junior_max_years"):
    return False          # <-- exits immediately for anyone with 3+ years
required = getattr(job, "required_years", None)
if isinstance(required, (int, float)):
    return float(required) > total_years + SENIORITY_YEARS_TOLERANCE
```

The `junior_max_years` guard (default 3.0) returns `False` for anyone
above the junior threshold, and the `required_years` check below it is
never reached. A candidate with 4 years of experience sees a job requiring
15 years pass through the prefilter and go to the LLM.

**Impact:** Wasted LLM calls on jobs wildly out of range for non-junior
candidates. The LLM will almost certainly reject them, but each costs a
scoring call.

**Fix:** Move the explicit `required_years` check above the
`junior_max_years` check. The years tolerance still applies, but
`15 > 4 + 1.5` is correctly caught locally.

---

### 4. Document generation truncates descriptions to 2000-2500 chars (`doc_generator.py`)

**File:** `doc_generator.py:244, 412, 797, 914`

The matcher was updated to feed up to 24,000 characters to the LLM for
scoring. But the document generator still hardcodes severe truncation:

- `tailor_bullet_points`: `job_description[:2000]`
- `tailor_summary`: `job_description[:2500]`
- `generate_cover_letter`: `job_description[:2500]`
- `extract_job_insights`: `job_description[:4000]`

In modern job listings, the first 2000 characters are typically company
intro and culture copy. The actual tech stack, responsibilities, and
required qualifications are in the second half.

**Impact:** Resumes and cover letters are tailored against the marketing
preamble of the posting, not against the technical requirements. The
enrichment pipeline fetches full descriptions specifically so the system
can use them — but document generation ignores that work.

**Fix:** Raise the limits to at least 15,000 characters, or pass the
pre-extracted `required_skills` and `nice_to_have_skills` directly into
the generation prompts.

---

### 5. Salary has no period — hourly rates break filtering and display

**Files:** `job_details.py:48-51`, `models/job.py:207-217`, `routers/jobs.py:231-233`

The extraction prompt says "if it quotes an hourly rate, give the hourly
number", but there is no `salary_period` column. A $65/hr contract role
is stored as `salary_min: 65.0`. The consequences:

1. `salary_label` displays `"$65"` instead of `"$65/hr"`
2. The matcher passes `"Stated salary: $65"` to the LLM alongside the
   candidate's `"Minimum salary: $130,000"`, making the LLM think the
   role pays $65/year
3. The salary filter in `routers/jobs.py` runs
   `coalesce(salary_max, salary_min) >= floor` — a $65/hr role (~$135k/yr)
   is hidden when filtering for $100k+

**Fix:** Add a `salary_period` column (`hourly`, `annual`, `monthly`) to
the extraction schema and the `Job` model. Annualize rates in query filters
and append the period in formatting.

---

### 6. Final `db.commit()` failure loses the entire fetch cycle (`job_fetcher.py:1237`)

Individual jobs are inserted with savepoints, but the outer `db.commit()`
is all-or-nothing. If it fails (connection drop, serialization error), all
jobs from a 20+ minute cycle are discarded.

**Fix:** Commit in smaller batches (e.g., every N jobs) rather than holding
the entire cycle in one transaction.

---

## High — Incorrect behavior visible to the user

### 7. Export-control regex drops valid commercial tech jobs (`eligibility.py:80-81`)

The pattern `r"export[\s-]control(?:led|s)?\b"` fires on any sentence
containing "export control". Standard compliance boilerplate at companies
like Google, Apple, Datadog, and Intel — "This position is subject to U.S.
export control regulations" — triggers this and causes the job to be
filtered as "Restricted to US citizens."

Under US EAR, foreign nationals are hireable for commercial software, and
"US Person" includes green card holders and asylees. The regex does not
require an actual restriction like "must be a US citizen due to export
controls."

**Impact:** Valid commercial software jobs at major employers silently
dropped.

**Fix:** Require explicit restriction language alongside export-control
mentions: e.g.,
`r"(?:requires?|limited to|must be)\s+.*?(?:u\.?s\.?\s+citizen|clearance).*?export[\s-]control"`.
Or downgrade "export control" from a blocking restriction to an advisory
sponsorship note.

---

### 8. Sponsorship detection triggers on non-visa "sponsor" (`eligibility.py:91, 238-247`)

`_SPONSORSHIP_TRIGGER = re.compile(r"sponsor(?:s|ed|ing|ship)?\b", re.I)`

Any sentence containing "sponsor" is scanned:
- "We sponsor attendance at PyCon and tech conferences" — matched by
  `_SPONSORSHIP_POSITIVE_RE` → flagged as positive visa sponsorship
- "The executive sponsor will oversee delivery" — no positive keyword →
  flagged as *negative* visa sponsorship

The function does not check whether the sentence is about immigration.

**Impact:** Incorrect visa sponsorship indicators displayed on job cards.

**Fix:** Require immigration context in the sentence: the word "sponsor"
must appear near "visa", "work authorization", "H-1B", "green card",
"immigration", "permanent resident", or similar.

---

### 9. Browser overlay ignores deep score and truncates keyword score to 0 (`job_context.py:97-102`)

```python
def _score(job: Job) -> int | None:
    if job.llm_score is not None:
        return int(job.llm_score)
    if job.keyword_score is not None:
        return int(job.keyword_score)  # 0.0-1.0 → int = 0
    return None
```

Two bugs in one function:
1. It ignores `llm_score_deep` entirely. For borderline jobs that got a
   second opinion, the overlay shows the first-pass score, not the score
   that actually decided the job's fate.
2. `keyword_score` is a ratio in [0, 1). `int(0.85)` is 0. Jobs with only
   a keyword score show "0" in the extension.

**Fix:** Use `job.effective_score` which already prefers deep score over
first pass and is the canonical property for this.

---

### 10. Seniority penalty detail checks the wrong result after deep scoring (`matcher.py:1126`)

After a deep score, the `score` variable is updated from `deep_result`,
but the detail message still checks `llm_result.get("seniority_fit")`.
If the two passes disagree on seniority fit, the user sees a wrong
explanation.

**Fix:** Check whichever result determined the final score:
`deep_result if deep_result else llm_result`.

---

### 11. Language-filtered jobs keep a non-zero keyword_score (`matcher.py:1050-1063`)

When detail extraction discovers a foreign language after the keyword
filter already set `keyword_score`, the job is filtered with
`llm_score = None` but `keyword_score` retains its value. The early
filter path (line 1017) zeros it; this path doesn't. Combined with bug 9,
these jobs show "0" in the overlay.

**Fix:** Set `job.keyword_score = 0.0` on the language filter path, same
as the early filter path.

---

### 12. Dead fallback string in `linked_auth.py` (line 134)

```python
_note_failure(db, row, f"HTTP {response.status_code}: {detail}"
                       or f"HTTP {response.status_code}")
```

The `or` operates on two f-strings. The left is always non-empty
(`"HTTP 401: "`), so the right is unreachable. When `detail` is empty
the failure is recorded with a dangling `: `.

**Fix:** `f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"`

---

### 13. Outreach follow-up rollback discards the whole batch (`outreach.py:1003-1031`)

In `draft_due_follow_ups`, if drafting fails for one contact, `db.rollback()`
undoes `follow_up_due_at = None` for that contact AND discards all previous
uncommitted drafts in the batch. The failed contact retries every beat tick
forever; the successfully drafted messages are lost.

**Fix:** Use `db.begin_nested()` per contact, so a single failure rolls
back only its own savepoint.

---

### 14. Seniority detail reads raw setting, not tunable override (`matcher.py:315`)

The detail message reads `getattr(settings, "JUNIOR_MAX_YEARS", 3.0)` but
the blocking decision uses `tunable(profile_data, "junior_max_years")`. A
user who set a custom threshold sees the wrong number in the explanation.

**Fix:** Read the tunable value for the message, same as the decision.

---

### 15. Region keyword matching admits wrong countries (`locations.py:233`)

**File:** `locations.py:79, 233`

```python
# line 233
if any(kw in text_lower for kw in cfg["keywords"]):
    return True
```

The region matcher uses substring containment (`kw in text_lower`) rather
than word-boundary matching. The Europe region's keyword list includes
`"austria"` (line 79), which is a substring of `"australia"`. Any job
located in Sydney, Melbourne, or anywhere in Australia passes
`_region_matches("europe", ...)` because `"austria" in "sydney, australia"`
is `True`.

The abbreviation check on line 236 already uses `\b` word boundaries
correctly — the keyword check does not.

**Impact:** Australian jobs silently classified as European. A user
targeting Europe gets Australian results; a user excluding non-European
locations would still see them admitted.

**Fix:** Use word-boundary matching for keywords:
`re.search(rf"\b{kw}\b", text_lower)` instead of `kw in text_lower`.

---

## Medium — Performance, missing indexes, concurrency

### 16. Missing indexes on the `jobs` table

The `archived_jobs` table has indexes for all its queried columns. The
`jobs` table — queried far more heavily — is missing:

| Index | Where it's needed |
|-------|------------------|
| GIN on `source_urls` | `deduplication.find_existing_job` — once per fetched job |
| `status` | matcher, archive, enrichment, liveness, funnel, job list |
| `(source, source_job_id)` | dedup layer 2 |
| `url` | job_context, multiple service paths |
| `fetched_at` | ORDER BY in 8+ service files |

**Fix:** Add an Alembic migration creating these indexes. Copy the pattern
from the archived_jobs migration (0028).

---

### 17. Missing indexes on `applications.job_id` and `application_documents.application_id`

Neither FK column is indexed. The archive service joins applications to
jobs on every pass; loading an application's documents navigates
`application_documents.application_id`. Both require full table scans.

**Fix:** Add indexes on both FK columns.

---

### 18. `Contact.application_id` CASCADE contradicts stated intent (`outreach.py:63-66`)

Comment: "Nullable so a contact can outlive the application." FK:
`ondelete="CASCADE"`. CASCADE deletes contacts when the application is
deleted — the opposite of outliving.

**Fix:** Change to `ondelete="SET NULL"`.

---

### 19. `Application.job_id` and `ApplicationDocument.application_id` missing ondelete

Both FKs lack an explicit `ondelete`, defaulting to RESTRICT. Every other
FK in the system declares its policy. A deletion of a Job or Application
without pre-checking children raises a database error.

**Fix:** Add explicit `ondelete="CASCADE"` (or `SET NULL` if orphan
documents should survive).

---

### 20. `generation_status` state machine has no DB-level guard

Transitions (idle → generating → done/failed) are managed in application
code only. `doc_refresh.py` sets `generation_status = "generating"` without
a `WHERE generation_status = 'idle'` guard. Two workers can transition the
same application simultaneously.

**Fix:** Use a conditional UPDATE (`WHERE generation_status IN ('idle',
'failed')`) and check the affected row count before proceeding.

---

### 21. Duplicate application check does O(n) scan in Python (`deduplication.py:209-223`)

`find_duplicate_application_job` loads all (id, company, title) tuples for
every job with an application, then normalizes and compares each in Python.
Runs once per matched job in the scoring loop.

**Fix:** Add an `ILIKE` filter on the first word of the normalized company
in SQL to reduce the candidate set before it reaches Python.

---

### 22. Redis blip blocks the next fetch for up to 1 hour (`fetch_lock.py:49-65`)

When Redis is down, `acquire()` returns True but stores no token. On
recovery, the stale lock persists for its full TTL (3600s).

**Fix:** On `release()` with no token, attempt a plain `DEL` rather than
silently returning. Or reduce the TTL and accept a shorter worst-case.

---

### 23. `_resolve_apply_links` loads all known URLs into memory (`job_fetcher.py:638-647`)

Builds a Python `set` of every URL from the entire jobs table. At scale
this is tens of megabytes.

**Fix:** Use a DB-side `EXISTS` check per URL instead of a Python set.

---

### 24. Deduplication race on layers 1 and 2 (`deduplication.py:115-138`)

Three SELECT queries with no row-level locking. Layer 3 (dedupe_hash) has
a UNIQUE constraint; layers 1 (URL in array) and 2 (source + source_job_id)
do not. Concurrent fetchers can both pass and both insert.

**Fix:** Add a unique constraint on `(source, source_job_id)` where
`source_job_id IS NOT NULL`. For layer 1, the GIN index + UNIQUE won't
work on arrays, so rely on the savepoint retry pattern.

---

### 25. All Celery tasks share a single queue (`celery_app.py`)

**File:** `celery_app.py:6-50`

All 16 task modules — including slow work like `browse` (Playwright,
seconds per page), `fetch` (network I/O), `match` and `descriptions`
(LLM calls) — route to the single default `celery` queue. There is no
`task_routes`, no `task_queues`, and no per-task `queue=` argument.

With `worker_prefetch_multiplier=1` (line 27), each worker pulls one task
at a time. A burst of LLM scoring tasks blocks fast, latency-sensitive
work: `liveness` probes, `backup`, `prune_llm_log`, and the beat
health-check task.

**Impact:** Slow tasks starve fast tasks. A scoring pass of 200 jobs
blocks liveness checks for the duration. The three-tier fetch split (§2)
would still run into this even after the lock fix.

**Fix:** Define at least two queues — `default` for fast/short tasks and
`heavy` for LLM, browser, and fetch tasks. Route tasks via
`task_routes` in the Celery config.

---

### 26. Eager `selectin` on `Job.scores` loads scores everywhere (`models/job.py:163-166`)

**File:** `models/job.py:163-166`

```python
scores: Mapped[list["JobScore"]] = relationship(
    "JobScore", cascade="all, delete-orphan", lazy="selectin", ...
)
```

The comment says this is for the list page ("fifty queries per page"), but
`selectin` applies globally. Every service, task, and background job that
touches a Job object fires a second SELECT to load all associated
`JobScore` rows — even when scores are irrelevant (fetching,
deduplication, archiving, enrichment).

**Impact:** At scale (100k jobs, 2-3 scores each), background bulk-loading
pulls hundreds of thousands of score rows it never reads, increasing
memory pressure and database load.

**Fix:** Change to the default `lazy="select"` (lazy load) and use
`selectinload(Job.scores)` explicitly in the queries that need it (the
job list endpoint, the overlay, and the score-history view).

---

## Low — Correctness nits, dead code, portability

### 27. Single-word title matching is too permissive (`matcher.py:48-60`)

`_title_matches_roles` matches on ANY single-word overlap. "Civil
Engineer" passes for target "Software Engineer" because of "engineer".
These then fail the skill check as `few_skills`, and because `few_skills`
is in `DESCRIPTION_DEPENDENT_REASONS`, enrichment wastes time trying to
scrape and re-score them.

**Fix:** Require at least two overlapping words for multi-word titles, or
require the overlap word to not be a generic noun ("engineer", "manager",
"specialist", "analyst") unless the full phrase matches.

---

### 28. Windows crash: Unix-only `fcntl` import (`doc_generator.py:3`)

`import fcntl` at module top level. On Windows,
`ModuleNotFoundError: No module named 'fcntl'`.

**Fix:** Wrap in `try...except ImportError` with a no-op fallback for
non-Unix platforms.

---

### 29. Dead code: `research_company` Celery task never called (`tasks/interview.py:18-45`)

Defined as a Celery task but never invoked anywhere in the codebase. The
UI does company research synchronously inline.

**Fix:** Remove the dead task, or wire it up if async research is wanted.

---

### 30. Test DB URL derivation can mangle the username (`tests/conftest.py:12`)

```python
settings.DATABASE_URL.replace("/jobapp", "/jobapp_test")
```

`str.replace` is global. `postgresql://jobapp:jobapp@host/jobapp` contains
`/jobapp` in the userinfo, so the fallback renames the role too.

**Fix:** Use `sqlalchemy.engine.make_url(...).set(database="jobapp_test")`.

---

### 31. Profile loaded once for entire batch chain (`matcher.py:1162-1163`)

Profile changes (skills, target roles, min score) made mid-batch don't
take effect. With self-chaining, hundreds of jobs can be scored against a
stale profile.

**Fix:** Reload the profile at the start of each chained batch, not just
the first.

---

### 32. `_extract_json_object` brace-counting ignores string contents (`matcher.py:584-600`)

The fallback JSON extractor counts raw `{`/`}` without considering string
interiors. Only matters when `json.loads` fails and the response has
surrounding prose with unbalanced braces in strings.

**Fix:** Use a proper JSON-extraction regex or skip characters inside
quoted strings during the depth count.

---

### 33. Mid-cycle commits not transactional with job inserts (`job_fetcher.py:675-724`)

Board backfill and registry updates commit mid-cycle. If the final job
commit fails, the backfill is marked done but its discovered boards are
lost.

**Fix:** Move board state commits to the same transaction as the job
inserts, or accept the inconsistency with a comment documenting it.

---

## Infrastructure drift — config that has diverged from the code

### 34. Dev Docker Compose mounts nonexistent nginx config (`docker-compose.yml:67`)

**File:** `docker-compose.yml:62-67`

The dev compose defines an `nginx` service mounting `./nginx/nginx.conf`.
No `nginx/` directory exists in the repository. The production compose
(`docker-compose.prod.yml`) correctly uses Caddy, with `caddy/Caddyfile`
present. The dev compose was never updated when the reverse proxy was
switched.

**Fix:** Either remove the nginx service from `docker-compose.yml` (if
Caddy is used in dev too) or add the missing `nginx/nginx.conf`.

---

### 35. Four tunable settings missing from `.env.example`

**Files:** `celery_app.py:75, 82, 156`, `llm/providers.py:115`,
`.env.example`

The code reads `FETCH_LINKED_INTERVAL_HOURS`,
`FETCH_LINKED_DEEP_INTERVAL_HOURS`, `BROWSE_TOPUP_INTERVAL_MINUTES`, and
`GEMINI_BASE_URL` from settings (with silent defaults in `config.py`).
None appear in `.env.example`. A deployer reading that file as the
canonical variable list has no way to discover or tune these schedules, or
override the Gemini endpoint for a self-hosted proxy.

**Fix:** Add the four variables to `.env.example` with their default
values and a brief comment.

---

## Architecture — sound design, confirmed working

These areas were reviewed and are correctly implemented:

- **Auth middleware** is fail-closed. New routers are protected by default.
  HMAC-signed session cookies, constant-time comparisons, placeholder
  secret detection, login throttle.

- **Celery late acks** with `task_reject_on_worker_lost=True` ensures
  at-least-once delivery. The generation sweeper catches anything that
  falls through.

- **Two-pass scoring** (fast model + deep second opinion) with provider
  chain walking is well-designed. The deep chain walks all providers, fixing
  the "40 consecutive failures" problem.

- **Deduplication layers** (URL, source+ID, content hash) with
  normalization are thorough. Cross-post detection at match time catches
  near-misses.

- **Enrichment multi-method pipeline** (ATS API, JSON-LD, LLM, browser)
  with re-scoring hooks is well-architected. The `_worth_rescoring` gate
  correctly prevents wasted calls — except for the missing
  "already scored on this description" check (finding 1).

- **Concurrency management** — savepoints for per-job isolation,
  profile blob refresh-merge to avoid lost updates, Redis distributed
  locks with TTL and compare-and-delete — is carefully implemented.

- **LLM concurrency gate** correctly serializes single-slot providers
  with a Redis lock, poll-based waiting, and fail-open on Redis outage.
