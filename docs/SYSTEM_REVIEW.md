# Consolidated system review — September 2026

Five independent reviews of this codebase, merged, de-duplicated, and checked
against the code. Roughly 95 raw claims came in; 42 survive as real findings,
11 did not reproduce, and two arrived with fixes that would have made things
worse. Those corrections are in §6 rather than quietly dropped, because a review
that is wrong about severity is worse than one that is silent — and a review
that recommends a harmful fix is worse than both.

`docs/IMPROVING.md` remains the design pass — what this system could reach for.
This is the defect pass: what it currently gets wrong.

## How to read this

Every finding says **what breaks**, **why it matters**, and **how to fix it**.
Each carries a verification marker:

| | meaning |
|---|---|
| **REPRODUCED** | ran it and watched it fail; a transcript or measurement is quoted |
| **CONFIRMED** | read the code path end to end and the defect is unambiguous |
| **LATENT** | the defect is real but no live code path reaches it yet |
| **UNMEASURED** | the mechanism is certain; how often it fires needs production data, and the query to find out is given |

Measurements are from Postgres 16 at schema `head`. The suite is 3,243 tests
and passes (two `-n auto` runs; one flake, see §5.7).

---

## 1. Priority 0 — burning money, and one defeated architecture

### 1.1 Jobs rejected on a full description are re-scored forever · **REPRODUCED**

> **Fixed.** `_worth_rescoring` now refuses a job whose description has not
> grown since the `JobScore` it carries, and `requeue_settled_verdicts`
> excludes those rows in SQL so the pass stops re-reading them. Re-run of the
> reproduction: one catch-up pass, then `requeued=0` forever.

**What breaks.** `enrichment.requeue_settled_verdicts` selects on
`(status = filtered_out, filter_reason ∈ DESCRIPTION_DEPENDENT_REASONS,
len(description) ≥ 1500)` and resets those rows to `new`. The matcher scores
them, a genuinely mediocre job lands below `min_match_score`, and it is filed
back as `filtered_out` / `low_score` — which is **the same set of conditions the
query selects on**. Nothing anywhere records that this job was already scored on
this description.

Reproduced with one job holding a stable 3,500-character posting and a matcher
stubbed only at the LLM boundary — real `match_job`, real
`evaluate_keyword_filter`, real status writes:

```
start: status=filtered_out reason=low_score chars=3500

cycle 1: requeued=1 -> status=new | llm_calls_spent=1 -> back to filtered_out/low_score
cycle 2: requeued=1 -> status=new | llm_calls_spent=1 -> back to filtered_out/low_score
cycle 3: requeued=1 -> status=new | llm_calls_spent=1 -> back to filtered_out/low_score
cycle 4: requeued=1 -> status=new | llm_calls_spent=1 -> back to filtered_out/low_score
cycle 5: requeued=1 -> status=new | llm_calls_spent=1 -> back to filtered_out/low_score

job_scores rows accumulated for this one job: 5
description never changed; description_updated_at = None
```

**Why it matters.** `requeue_settled_verdicts(limit=RESCORE_MAX_PER_RUN=1000)`
runs at the top of *every* enrichment pass. Passes come from a 30-minute beat,
from a tail-call on every fetch cycle, and from self-chaining up to
`ENRICH_MAX_CHAINED_PASSES = 50`. The eligible population is not small — the
function's own docstring measures it at 39,702 `few_skills` plus 18,472
`low_score` rows sitting on full descriptions. That set never shrinks; it
circulates. Each lap is one scoring call per job, plus a second-opinion call for
anything landing in the 55–85 deep band, plus a `job_scores` insert and a prune.

Two costs, and the second is worse than the bill. Paid LLM calls on verdicts
that cannot change, and a permanently non-empty `new` queue that competes with
genuinely fresh postings for a matcher that processes 25 jobs a batch. The speed
lane is the product's whole thesis, and this is what starves it.

**How to fix.** The guard needs no migration and no new logic — the predicate
already exists. `score_history._trigger` computes exactly "did the description
grow since the last recorded verdict":

```python
grew_at = job.description_updated_at
if grew_at is not None and previous.created_at is not None:
    if grew_at > previous.created_at:
        return "description_grew"
```

Lift that into `_worth_rescoring`: refuse to requeue when a `JobScore` already
exists and the description has not been updated since it was written. Jobs whose
text genuinely grows still come back — which is the feature — and jobs that were
fairly judged stay judged. `JobScore.description_chars` gives a belt-and-braces
second test if you want one.

This also subsumes a separate finding about the same function: its comment
argues that leaving the query unordered guarantees progress. It does not, but
that hardly matters once the set is allowed to drain.

---

### 1.2 The three-way fetch split is cancelled by a shared lock · **CONFIRMED**

> **Fixed.** A combined run now takes every group key; a group run takes only
> its own. `fetch_state()` reads all of them so the runs page and the manual
> trigger still see a scheduled group as running.

**What breaks.** `app/tasks/fetch.py:39`:

```python
keys = [LOCK_KEY] if group in (None, "all") else [GROUP_LOCK_KEYS[group], LOCK_KEY]
```

Every group run acquires its own key **and** the global `LOCK_KEY`, and a
failure on either one skips the run entirely.

**Why it matters.** The module docstring states the goal: "The whole pipeline
used to be a single 47-minute task, which meant a source that could refresh
hourly ran on the schedule of the slowest thing beside it: Adzuna waited behind
a Chromium launch." Because all three groups contend on `LOCK_KEY`, they still
do. `fetch-browser-tier` holds it for the length of a Playwright run;
`fetch-api-sources` fires on its two-hour beat, takes `jobapp:fetch:api`, fails
on `jobapp:fetch:running`, releases, and logs "another fetch holds
jobapp:fetch:running; skipping". The per-group keys are decoration — they never
block anything the global key doesn't already block.

The comment explains why the global key is there: so a manual "fetch everything"
cannot overlap a scheduled group. That invariant is worth keeping; taking the
global key in group runs is just the wrong way to keep it.

**How to fix.** Invert it. An "all" run takes every group key; a group run takes
only its own.

```python
keys = (list(GROUP_LOCK_KEYS.values())
        if group in (None, "all")
        else [GROUP_LOCK_KEYS[group]])
```

Same guarantee — a group run blocks "all", and "all" blocks every group — but
two different groups no longer exclude each other. Keep writing `LOCK_KEY` as a
non-blocking presence marker if `/runs` reads it for its "fetch running"
indicator, or derive that indicator from the group keys.

---

### 1.3 Two of the three dedupe layers, and the whole overlay lookup, scan the table · **REPRODUCED**

> **Fixed.** Migration 0038 adds the GIN and btree indexes; both `.any()`
> call sites became `.contains()` so GIN can serve them. The real
> `find_existing_job` path measures 3.27 ms per posting, down from ~82 ms,
> on a Bitmap Index Scan. See the migration for the `&&` caveat.

**What breaks.** `jobs` carries twelve indexes and none of them covers the
queries that run most often.

`deduplication.find_existing_job`, once per fetched posting — measured at 120k
rows, on a miss (the case that matters, since misses are what a fetch is for):

| Layer | Predicate | Index | Measured |
|---|---|---|---|
| 1 | `url = ANY(source_urls)` | none | **49.6 ms**, 120,000 rows scanned |
| 2 | `source = ? AND source_job_id = ?` | none usable | **32.5 ms**, 120,000 rows scanned |
| 3 | `dedupe_hash = ?` | unique btree | ~0.05 ms |

Layer 2 appears covered by `ix_jobs_source`, but `source` has about twenty
distinct values so the planner correctly ignores it.

`job_context.find_job`, once per job page the extension overlay renders, runs
**three** unindexed queries in sequence — `Job.url.in_(variants)`, then
`Job.apply_url.in_(variants)`, then `Job.source_urls.overlap(variants)`.

**And the index that exists for this has never been used.** Migration 0028 adds
a GIN index on `archived_jobs.source_urls` with a comment explaining exactly why
("a btree cannot answer" it, "runs once per fetched posting"). But
`was_archived` queries with `.any(url)`, which SQLAlchemy emits as `= ANY`, and
**GIN cannot answer `= ANY`** — only `@>` and `&&`. With the GIN index present:

```
source_urls @> ARRAY['…']::varchar[]   →  0.065 ms   (Bitmap Index Scan)
'…' = ANY(source_urls)                 → 48.283 ms   (Seq Scan, 120,000 rows removed)
```

**Why it matters.** At the ~300k rows this codebase measures elsewhere, a new
posting pays roughly 200 ms before it is written; a cycle taking in two thousand
postings spends minutes deciding "have we seen this?". The overlay pays three
scans on every page view, which is the one latency the user feels directly.

**How to fix.** Index *and* query shape — either alone does nothing.

```python
# deduplication.py, both call sites
.filter(Job.source_urls.contains([url]))          # emits @>
.filter(ArchivedJob.source_urls.contains([url]))
```

```python
# new migration
op.create_index("ix_jobs_source_urls", "jobs", ["source_urls"],
                postgresql_using="gin")
op.create_index("ix_jobs_source_job", "jobs", ["source", "source_job_id"])
op.create_index("ix_jobs_url", "jobs", ["url"])
op.create_index("ix_jobs_apply_url", "jobs", ["apply_url"])
```

`find_job`'s `overlap()` already emits `&&`, so the GIN index fixes that call
site with no code change. Verified after: layer 2 drops to 0.056 ms, layer 1 to
0.065 ms.

`job_fetcher._known_urls` (line 638) answers the same question by pulling every
URL on the table into a Python set once per cycle. Defensible as a bulk test,
but it is a second implementation of the dedupe rule with its own semantics, and
tens of megabytes held for the length of a cycle in each of two worker
processes. Fold it into the indexed lookup once one exists.

---

### 1.4 Every deploy fails, and deploys three times · **REPRODUCED**

> **Fixed.** `restart caddy`, plus `set -e` and a build/migrate/up ordering so
> the migration runs with the new image before any worker serves traffic.

**What breaks.** `.github/workflows/deploy.yml` ends each of its three scripts
with `docker compose -f docker-compose.prod.yml restart nginx`. There is no
`nginx` service in the prod compose file — the proxy is `caddy`:

```
$ docker compose -f docker-compose.prod.yml restart nginx
no such service: nginx
EXIT=1
```

**Why it matters.** The step fails *after* a successful build and migration, so
`continue-on-error` fires → sleep 120 → full rebuild and `alembic upgrade head`
again → fails again → sleep 240 → a third rebuild, this time without
`continue-on-error`, and the workflow ends red. Every push to `main` costs three
image builds, three migration runs, about seven minutes, and a red check that
says nothing about whether the deploy worked. The retry ladder was built for
intermittent SSH timeouts and now fires on a certainty.

**How to fix.** `restart caddy`, or delete the line — `up -d --build` already
restarts what changed, and the Caddyfile is a read-only bind mount.

---

## 2. Priority 1 — wrong answers about jobs

### 2.1 The eligibility scanner blocks jobs on bare mentions · **REPRODUCED**

**What breaks.** Four `_RESTRICTION_PATTERNS` match a phrase with no requirement
language around it:

```python
(re.compile(r"(?:top[\s-]secret|ts/sci)\b", re.I),      "Security clearance required"),
(re.compile(r"\bsecret\s+clearance\b", re.I),           "Security clearance required"),
(re.compile(r"\bu\.?s\.?\s+person(?:s)?\b", re.I),      "ITAR / US Person requirement"),
(re.compile(r"export[\s-]control(?:led|s)?\b", re.I),   "Export-control restriction"),
```

A hit sets `filter_reason = "restricted"` and the job leaves the list. Three
guards exist — EEO boilerplate, negation, cased acronyms — and none of them asks
whether the sentence is about the *reader* or about the *company*. Run against
realistic text:

| Posting text | Verdict |
|---|---|
| "Acme builds software that helps manufacturers manage export control and trade compliance at scale." | **blocked** — Export-control restriction |
| "Our TS/SCI-cleared customers rely on us. This role is fully remote and open to all." | **blocked** — Security clearance required |
| "Acme collects personal data about U.S. persons and processes it under CCPA." | **blocked** — ITAR / US Person requirement |

**Why it matters.** This is the *blocking* tier, with the irreversible
consequence. The first row is every posting at a trade-compliance or GRC vendor;
the second is every posting at a security company selling to government. Whole
employers vanish silently. And standard commercial boilerplate — "this position
is subject to U.S. export control regulations" — appears at Intel, Qualcomm,
Cisco and Apple on roles that are lawfully open to non-citizens, since EAR
"US Person" includes permanent residents and asylees.

Product principle 3 is technically satisfied — evidence is shown — by quoting a
sentence about the customer base.

**How to fix.** Make these patterns look like the other eleven, which already
model it correctly (`must (?:be|hold|possess|have) .{0,40}?clearance` requires
the obligation). Require a requirement verb in the same sentence:

```python
_REQUIREMENT_NEAR = re.compile(
    r"\b(?:must|required?|requires|restricted|limited to|eligib|"
    r"you will need|candidates? must)\b", re.I)
```

and gate the four bare-mention patterns on it. Or demote them to the advisory
tier, where a wrong reading costs a badge rather than the job. Blocking should be
the tier that demands the most evidence, not the least. `eligibility` is a pure
module with its own test file, so each of the rows above is a one-line test.

---

### 2.2 Sponsorship badges are context-blind and direction-blind · **REPRODUCED**

Two separate defects in the same advisory read.

**Any sentence containing "sponsor" is treated as an immigration statement.**
`_SPONSORSHIP_TRIGGER = re.compile(r"sponsor(?:s|ed|ing|ship)?\b", re.I)` has no
immigration context requirement, so:

* "We sponsor attendance at PyCon and regional tech conferences" → matches
  `_SPONSORSHIP_POSITIVE_RE` on "supports"/"provides" → badged **sponsorship
  available**.
* "The executive sponsor will oversee delivery" → no positive keyword →
  badged **will not sponsor**.

**Direction is read from the whole sentence, not the sponsorship clause:**

| Posting text | Recorded | Correct |
|---|---|---|
| "Although we cannot offer relocation assistance, visa sponsorship is available for this role." | negative | positive |
| "Sponsorship is provided at no cost to the candidate." | negative | positive |

The first negation belongs to a different clause. The second is the literal
words "no cost" — the module's comment defends word boundaries because "as bare
substrings, 'no' matches 'now'", and the boundary is there; `no cost` is a
whole-word `no`.

**Why it matters.** Advisory-only, so no score changes and no job is lost. But
the badge appears on the job list, the detail page, the apply queue and the
extension overlay — four places where the product asserts, in the employer's
name, something the employer did not say. Under principle 4 ("never fabricate")
that is worse than showing nothing, and it is being asserted about the one topic
principle 4a says to handle with maximum care.

**How to fix.** Two narrow changes.
1. Require immigration context in the sentence before treating it as a
   sponsorship statement: `visa|work authorisation|work authorization|h-1b|
   h1b|green card|permanent resident|immigration|opt|cpt|tn visa`.
2. Classify on the clause containing the `sponsor` token, not the sentence —
   split on `,` `;` ` but ` ` although ` ` however ` — and test the positive
   pattern before the negative one when both match.

---

### 2.3 The resume writer never sees the job's requirements · **CONFIRMED**

**What breaks.** `matcher.MATCH_DESCRIPTION_CHARS` defaults to 24,000 and the
docstring explains why it was raised: 4,000 "routinely cut off
mid-requirements — so the model was scoring seniority and skill fit against the
marketing half of the posting". The document generator never got that fix:

| Call site | Ceiling |
|---|---|
| `doc_generator.py:412` — tailor bullet points | `job_description[:2000]` |
| `doc_generator.py:797` — tailor summary | `job_description[:2500]` |
| `doc_generator.py:914` — cover letter | `job_description[:2500]` |
| `doc_generator.py:244` — extract job insights | `job_description[:4000]` |
| `self_review.py:~154` — review the draft | `job_description[:6000]` |

**Why it matters.** 2,000 characters is about 300 words, and in a modern
corporate posting that is the company intro, the mission statement and the
culture paragraph. Requirements, tech stack and responsibilities live in the
lower half. So the model rewriting the user's resume bullets to match a job is
doing it **without having read what the job asks for** — and the self-review
pass that is supposed to catch that is reading a different, also-truncated
excerpt. This is the product's core promise ("tailored resume and cover letter
per role") running on the wrong half of the input.

**How to fix.** Two steps, and the second matters more than the first.

1. Raise the ceilings toward `MATCH_DESCRIPTION_CHARS`, through one shared
   helper rather than five literals.
2. Pass the structured facts that already exist. `job_details` extracts
   `required_skills`, `nice_to_have_skills`, `required_years` and
   `education_required` into columns precisely so downstream consumers stop
   re-deriving them from prose. `matcher._stated_facts` already renders them as
   explicit lines; give the generator the same block. A prompt that opens with
   "Required skills: Python, Kubernetes, Terraform" beats any amount of raw
   text.

---

### 2.4 The seniority prefilter never checks the stated number for non-juniors · **CONFIRMED**

**What breaks.** `matcher._blocked_by_seniority`:

```python
total_years = _total_years(profile_data.get("experience", []))
if total_years >= tunable(profile_data, "junior_max_years"):
    return False                      # ← every non-junior exits here

required = getattr(job, "required_years", None)
if isinstance(required, (int, float)) and not isinstance(required, bool):
    return float(required) > total_years + SENIORITY_YEARS_TOLERANCE
```

**Why it matters.** The docstring's thesis is "the number wins" — a title word is
a guess, a stated `required_years` is a fact. But the numeric branch is
unreachable for anyone above `junior_max_years` (default 3). A candidate with
four years is never spared a job that explicitly asks for fifteen: it passes the
prefilter, costs a scoring call, and the LLM rejects it because the prompt tells
it to. The cheap deterministic check that exists to avoid that call is skipped
for exactly the candidates who have outgrown the junior heuristic.

**How to fix.** Hoist the numeric check above the junior gate, so the stated
number is consulted whenever the posting states one and the title heuristic
stays the junior-only fallback it was written as:

```python
required = getattr(job, "required_years", None)
if isinstance(required, (int, float)) and not isinstance(required, bool):
    return float(required) > total_years + SENIORITY_YEARS_TOLERANCE

if total_years >= tunable(profile_data, "junior_max_years"):
    return False
# title heuristic below, unchanged
```

Worth stating the consequence: the `filter_senior_titles` toggle then also
governs a numeric check. That reads as correct given the docstring, but it is a
behaviour change for senior profiles and the tunable's help text should say so.

---

### 2.5 The region filter admits jobs from the wrong continent · **REPRODUCED**

**What breaks.** `locations._region_matches` tests unbounded substring keywords
plus case-sensitive two-letter US state codes — and **US state codes collide
with ISO-3166 country codes.** Verified against `prefs = {"regions": ["usa"]}`:

| Location text | Matches `usa` via |
|---|---|
| `Toronto, CA` / `Vancouver, CA` | `CA` — California / Canada |
| `Berlin, DE` / `Munich, DE` | `DE` — Delaware / Germany |
| `Bengaluru, IN` | `IN` — Indiana / India |
| `Tel Aviv, IL` | `IL` — Illinois / Israel |
| `Valletta, MT` | `MT` — Montana / Malta |
| `Panama City, PA` | `PA` — Pennsylvania / Panama |
| `Jerusalem, Israel` | substring `usa` in **Jer-usa-lem** |
| `South America` | substring `america` |

`location_allowed` tests the user's own regions first and returns `True` on the
first hit, so every one of these passes as "matches your preferences", costs a
scoring call, and lands in the list.

**Why it matters.** Wasted scoring calls, and a location filter that quietly
does not filter. Note `deduplication.normalize_location` solves the adjacent
problem correctly — it strips the same tokens by name and falls back when
nothing survives — so the care exists in the codebase, just not here.

**How to fix.** Two contained changes.
1. Word-boundary the multi-character keywords, or at minimum the three-letter
   ones (`usa`, `u.s.`).
2. Check the *other* regions before the user's own and return `False` on a
   match. `Toronto, CA` then loses to `canada`'s explicit `toronto` keyword, and
   most of the table above resolves for free. Accept a bare state code only when
   something else in the string already says United States.

---

### 2.6 Salary loses its period, so the filter hides the best-paying jobs · **CONFIRMED**

Already the first item in `docs/IMPROVING.md` §0 and independently re-raised
here; confirmed still live, so it belongs on this list.

`job_details._SYSTEM_PROMPT` asks the model for "the annual figure when the
posting gives one; if it quotes an hourly rate, give the hourly number", and the
schema has no `salary_period`. So a $65/hr contract role stores
`salary_min = 65.0`. Consequences, all live:

* `Job.salary_label` renders "$65" rather than "$65/hr".
* `matcher._stated_facts` writes "Stated salary: $65" into the same prompt as
  "Minimum salary: $130,000", inviting the model to conclude the job pays $65 a
  year.
* `routers/jobs.py:232` filters `coalesce(salary_max, salary_min) >= floor`, so
  a $100k floor hides a $65/hr posting worth about $135k — and admits a
  posting stating €100,000 against a floor the user meant in dollars.

The build plan in `IMPROVING.md` §0 is sound and unchanged: add `salary_period`
and derived `salary_annual_*` columns, ask the model to transcribe rather than
convert, treat the period as part of the band in `enrich_from`, and read the
annual columns in the filter and the prompt.

---

### 2.7 Dedupe layer 2 rests on a guessed id that can collide · **CONFIRMED (mechanism) / UNMEASURED (incidence)**

**What breaks.** For every board read through `base.jobs_from_listing` — icims,
jobvite, teamtailor, ycombinator — `source_job_id` is not the board's identifier.
It is a guess:

```python
# app/services/sources/base.py:175
def _listing_job_id(url: str) -> str | None:
    """The longest number in a posting URL — every ATS puts its id in there."""
    numbers = re.findall(r"\d{3,}", url or "")
    return max(numbers, key=len) if numbers else None
```

"The longest number in the URL" is the posting id only when nothing else in the
URL is longer. A date segment, a tracking parameter or a tenant id wins whenever
it has more digits:

```
    20250131  <-  https://acme.example.com/careers/20250131/1234
    20250131  <-  https://acme.example.com/careers/20250131/5678
   987654321  <-  https://apply.example.com/acme/j/A1B2C3?utm_campaign=987654321
   987654321  <-  https://apply.example.com/acme/j/D4E5F6?utm_campaign=987654321
```

**Why it matters.** Two distinct postings that collide on this value are not
stored as two jobs. `find_existing_job` layer 2 matches on
`(source, source_job_id)` and returns the first row, so the second posting is
treated as *another sighting of the first*: its URL is appended to
`source_urls`, its description merged if it happens to be longer, and the job
itself is never stored. It is counted as `merged` or `skipped`, so the cycle
reports success. Under "find every job" that is the most expensive failure
available, and it is completely silent.

**How much this is actually happening is a data question, not a code question.**
The mechanism is certain; the incidence depends on the URL shapes the four live
boards emit, and this review had no production data. One query answers it:

```sql
SELECT source, source_job_id, count(*), array_agg(url)
FROM jobs
WHERE source_job_id IS NOT NULL
GROUP BY 1, 2 HAVING count(*) > 1
LIMIT 20;
```

Rows mean collisions already got through — and each one is a job that was
merged into an unrelated posting. Note the query can only show collisions that
produced two rows *anyway* (via a different dedupe layer); the ones layer 2
absorbed cleanly left no second row to count, so this is a floor, not a total.

**How to fix.** Prefer the board's own identifier where the structured data
carries one (`identifier`, `@id`, or the final path segment) and fall back to
the heuristic only when there is nothing better. Where the fallback is used,
qualify it — the URL path without the query string, or `host + path` — so a
tracking parameter cannot become the id. A `None` is safe: the code already
guards `if source_job_id:` and skips layer 2 entirely.

**What not to do.** One review proposed a `UNIQUE (source, source_job_id)`
constraint here. That would convert a silent merge into a hard `IntegrityError`,
the per-row savepoint would roll the insert back, and the posting would be
counted as `dropped` — turning a lost job into a lost job *plus* a broken
insert path. The constraint is only safe once the query above returns nothing,
and by then the underlying bug is already fixed.

---

## 3. Priority 2 — infrastructure drift

### 3.1 Two uvicorn workers race the migration, and the loser serves 503 forever · **CONFIRMED**

> **Fixed.** The migration runs under a Postgres advisory lock. Four concurrent
> workers against an empty database: 1/4 came up able to serve before, 4/4 after.

Prod runs `uvicorn app.main:app --workers 2`, and `app/main.py:105` runs
`subprocess.run(["alembic", "upgrade", "head"])` **inside the lifespan** — which
executes once per worker process. When there is anything to apply, one commits
and the other fails on an object that now exists, sets the module-global
`_migration_failure`, and the middleware then answers every request that process
receives with

```
503 — The database schema is not up to date, so the application is refusing
      to serve against it.
```

Both workers share the listening socket, so roughly half of all requests 503
after a deploy that looked successful, and `_migration_failure` is only cleared
by another lifespan, so it does not heal. §1.4 makes it worse by running the
migration three more times during the restart.

**Fix.** Take the write out of the request path. The deploy script already runs
`alembic upgrade head` as its own step, so the lifespan should *verify* instead:
compare `alembic_version` against `ScriptDirectory.get_current_head()` and set
`_migration_failure` on a mismatch. Same guarantee, no write, no race. If it
must stay, wrap it in a Postgres advisory lock.

### 3.2 Nothing runs the tests · **CONFIRMED**

> **Fixed.** `.github/workflows/test.yml` runs the suite on every push and pull
> request against a Postgres service container.

`deploy.yml` is the only workflow in the repository. 3,243 tests, a four-minute
parallel suite, and no gate between a push and production — while much of this
system's correctness lives in those tests. A `test.yml` with a postgres service
container, `pip install -e ".[dev]"` and `pytest` is a dozen lines. It would
have caught nothing in this review, which is the point: it protects the next
change.

### 3.3 A Redis blip on release wedges fetching for an hour · **CONFIRMED**

`fetch_lock.acquire` writes the token with `ex=DEFAULT_TTL_SECONDS = 3600`.
`release()` runs a Lua CAS-delete, and on any Redis exception it logs and
returns — leaving the key to expire. So a transient Redis error during release,
in a cycle that has already finished, blocks the next fetch for up to an hour.
The match and enrich locks use 1800s; fetch is the outlier.

**Fix.** Size the TTL to a slow cycle rather than an hour, and retry the release
once or twice before giving up. A lock whose TTL greatly exceeds the work it
guards converts a network blip into an outage.

### 3.4 A failed final commit loses the whole fetch cycle · **CONFIRMED**

`fetch_and_save_jobs` wraps each job insert in `db.begin_nested()`, which
correctly isolates one bad row. But all those savepoints live inside one outer
transaction committed once at line 1237; if that commit fails (connection loss,
disk pressure) the `except` logs and rolls back, and every insert in the cycle is
gone. The savepoints protect against a bad row, not against a bad commit.

**Fix.** Commit in chunks — every few hundred rows — so a late failure costs a
chunk rather than a cycle. `record_run` already commits separately, so the
pattern is established.

### 3.5 Generated documents are served without authentication · **CONFIRMED**

`caddy/Caddyfile:24` serves `/storage/*` straight off the shared volume with
`file_server`, so the request never reaches FastAPI and
`require_authentication` never runs. The files are
`{application_id}/{application_id}_resume_v1.pdf` — a tailored resume carrying
the user's full name, address, phone, email and complete work history.

The middleware's own docstring explains why it is middleware rather than a
dependency: "the failure mode of forgetting is an endpoint that silently serves
the user's application history to the internet." This is that endpoint, reached
from outside the application. The UUID makes it unguessable in practice and
`file_server` will not list the directory, so this is obscurity rather than
exposure — but any leaked URL is permanently public.

**Fix.** Serve documents through an authenticated FastAPI route
(`FileResponse` behind the existing middleware), or add `forward_auth` to the
`/storage` handler.

### 3.6 The login throttle can be stepped over · **CONFIRMED**

`routers/auth.py:22` reads the **first** `X-Forwarded-For` entry. `X-Forwarded-For`
is client-supplied, and Caddy's `reverse_proxy` *appends* the peer address to
whatever arrived rather than replacing it (this is why `trusted_proxies`
exists). So a request carrying `X-Forwarded-For: 1.2.3.4` reaches the app as
`1.2.3.4, <real ip>` and `split(",")[0]` returns the attacker-chosen half.
Rotate it per request and `MAX_ATTEMPTS = 5` / `LOCKOUT_SECONDS = 300` never
engage — against a single shared password, which is the exact threat
`auth.py`'s docstring names.

**Fix.** Read the **last** XFF entry — the only one your own hop added — or drop
XFF and use `request.client.host`; with one tenant a single global throttle
bucket is not a limitation. The docstring also still says "nginx".

### 3.7 `make up` starts a broken proxy · **CONFIRMED**

`docker-compose.yml` mounts `./nginx/nginx.conf`, which has never existed in
this repository (`git log -- nginx/` is empty; the tree has `caddy/`). Docker
creates an empty *directory* at that path and mounts it, so the container serves
the stock nginx page and leaves a stray directory in the working tree. Harmless
— port 8000 is published directly — but `make up` always leaves one container
wrong. Point it at `caddy/Caddyfile` or drop the service from dev.

---

## 4. Priority 3 — ceilings already close

### 4.1 One Celery queue, two slots, two self-chaining 25-minute tasks · **CONFIRMED**

`celery_app.conf` declares no `task_routes` and no queues, so everything shares
`celery`; production runs `--concurrency=2`. Both `match_jobs` and `enrich_jobs`
carry `soft_time_limit=1500` and re-queue themselves while there is work. They
can hold both slots, and a user clicking "Generate documents" then waits behind
up to 25 minutes of LLM round trips with nothing but a spinner.

**Fix.** Two queues and a second worker container: `batch` (match, enrich, fetch,
archive, prune, backup) and `interactive` (generate, agent-triggered work,
manual triggers), routed by task name with `-Q` per worker. A compose change
plus about ten lines of config.

### 4.2 Beat publishes regardless of queue depth; most tasks have no lock · **CONFIRMED**

Four of thirteen scheduled tasks take a Redis lock and no-op when a pass is
running (`fetch`, `match`, `enrich`, `compare_models`). The other nine —
`poll_mailbox` (15 min), `top_up_browsing` (30 min), `sweep_generations`
(20 min), `check_postings`, `archive_old_jobs`, `refresh_stale_docs`,
`process_followups`, `prune_llm_log`, `prune_agent_history` — do not. During any
window where both slots are busy, beat keeps publishing and the copies run
back-to-back afterwards: several IMAP sessions in a row, several unbounded
scans. `sweep_generations` is unbounded in a second sense too — its
"never queued" query has no `LIMIT` and touches `app.documents` per row.

### 4.3 `Job.scores` is eagerly loaded everywhere, for one page · **CONFIRMED**

`app/models/job.py:164` sets `lazy="selectin"`, justified by the jobs list page
rendering score history on every card. That is true, and every batch path pays
for it too — there is no `noload` anywhere in the codebase. Per run:
`archive.candidates` loads 5,000 Job objects, `requeue_settled_verdicts` 4,000,
`enrichment.select_targets` up to 1,000, `liveness.candidates` 200. Each fires a
second query for score rows nobody reads, on top of de-TOASTing descriptions.

**Fix.** `lazy="select"` on the relationship plus an explicit
`.options(selectinload(Job.scores))` on the jobs list route. One line moved;
identical page behaviour.

### 4.4 Archiving competes with enrichment for the same rows, and wins · **CONFIRMED**

`archive._eligible` takes any `filtered_out` job older than
`ARCHIVE_AFTER_DAYS` (60) whose reason is not user-made:

```python
PROTECTED_REASONS = frozenset({"manual", "blocked_title", "excluded_company"})
```

`DESCRIPTION_DEPENDENT_REASONS` — `no_description`, `few_skills`, `low_score`,
`restricted`, `seniority` — is not protected, and those are exactly the rows
enrichment exists to rescue. Archiving is irreversible for this purpose: the
description is what it discards, and `was_archived()` then makes the fetcher skip
the posting on every future cycle. So any job enrichment has not reached within
60 days leaves the pipeline permanently.

The module docstring's claim that "a job filtered on a title mismatch in June is
not going to be reconsidered" is true of `title_mismatch` and false of the five
reasons above — `enrichment._worth_rescoring` says so directly.

**Fix.** Cheapest guard: never archive a row with
`enrichment_attempted_at IS NULL`, so nothing is retired before it has been
tried once. Better: exclude `DESCRIPTION_DEPENDENT_REASONS` unless the row
already holds a full description, in which case its verdict is genuinely
settled — which is the same predicate §1.1 needs.

### 4.5 Liveness cannot keep up past ~1,200 matched jobs · **CONFIRMED**

`LIVENESS_MAX_PER_CYCLE = 200`, `LIVENESS_INTERVAL_HOURS = 12`,
`LIVENESS_RECHECK_DAYS = 3` → 400 checks a day, sustaining 1,200 jobs on a
three-day cycle. `candidates()` orders `liveness_checked_at ASC NULLS FIRST`, so
new matches always jump ahead of stale re-checks. Past the ceiling the oldest
matched jobs stop being re-checked and keep showing a months-old "still open" —
the failure the module was written to remove, relocated from "never checked" to
"checked once".

**Fix.** Surface the ratio on `/runs` (matched jobs ÷ daily budget) before
raising the budget blind.

### 4.6 `find_duplicate_application_job` scans every application in Python · **CONFIRMED**

It pulls `(id, company, title)` for every job ever applied to into memory and
compares normalised strings, once per matched job. The comment explains why
normalisation cannot go into SQL, which is fair. For one person's search the
candidate set is hundreds of rows, so this is a distant problem rather than a
live one — but it grows monotonically with the user's own success, and it runs
on the read side of every scoring pass.

**Fix when it matters.** Pre-filter in SQL on something normalisation preserves
— an `ilike` on the target company's first token — before the Python pass. Also
add the missing index on `applications.job_id`; Postgres does not index foreign
key columns automatically, and both this join and `sweep_generations` use it.

### 4.7 The title gate is far looser than its name · **CONFIRMED**

`_title_matches_roles` passes on **any single word overlap** with any target role
or LLM-expanded query. With "Software Engineer" among the roles, every "Sales
Engineer", "Civil Engineer" and "Locomotive Engineer" passes a gate labelled
"Title doesn't match target roles". Those then fail the skill check, land under
`few_skills` — which is in `DESCRIPTION_DEPENDENT_REASONS` — and enrichment
spends real requests scraping civil engineering postings so the matcher can
reject them again.

This is a deliberate fail-open and the right default *for the filter*. The
problem is that the same predicate is reused as the *priority* function for
enrichment (`enrichment._title_gate`), where fail-open means nearly every
candidate ranks in the first bucket and the ordering carries no information.

**Fix.** Give enrichment a stricter predicate than the filter: require overlap
on a role's head noun *plus* one qualifier, or exclude a list of generic nouns
("engineer", "manager", "specialist", "analyst") from matching in isolation.
Leave the filter's own behaviour alone — that one should fail open.

---

## 5. Priority 4 — small, cheap, and clearly wrong

### 5.1 The overlay shows `0` for every keyword-matched job · **CONFIRMED**

```python
# app/services/job_context.py:97
def _score(job: Job) -> int | None:
    if job.llm_score is not None:
        return int(job.llm_score)
    if job.keyword_score is not None:
        return int(job.keyword_score)      # keyword_score is 0.0–1.0 → always 0
    return None
```

Two defects in five lines: `int()` on a 0.0–1.0 float truncates to `0`, so the
extension overlay displays "0" for a newly keyword-matched posting; and
`llm_score_deep` is ignored entirely, so a borderline job the deep pass rescued
shows the first pass's number. `Job.effective_score` exists for exactly this.
**Fix:** `return round(job.effective_score) if job.effective_score is not None
else None`, and either scale `keyword_score` to a percentage or drop it from the
overlay.

### 5.2 A `low_score` rejection can describe a penalty it did not apply · **CONFIRMED**

`matcher.py:1126` reads `llm_result["seniority_fit"]` — the **first** pass —
while `score` on the same line is the **deep** score when the second pass ran.
When the two disagree, the sentence either claims a 15-point penalty that was
not applied to the number it quotes, or omits one that was. This is the sentence
the user reads to decide whether to override the filter.
**Fix:** `verdict = deep_result if deep_result is not None else llm_result`.

### 5.3 The seniority explanation quotes the env default, not the user's override · **CONFIRMED**

`matcher.py:315` builds the message with
`getattr(settings, "JUNIOR_MAX_YEARS", 3.0)` while the *decision* at line 113
uses `tunable(profile_data, "junior_max_years")`. Change it in the settings UI
and the filter obeys you while the explanation quotes the old number.
**Fix:** read the tunable in both places.

### 5.4 The language filter leaves a stale `keyword_score` behind · **CONFIRMED**

The early filter path (line 1017) sets `keyword_score = 0.0` and clears both LLM
scores, with a comment explaining why a stale score beside "filtered out" is
wrong. The post-extraction language path (lines 1050–1063) clears the LLM scores
but not `keyword_score`, which was written one line earlier. The fix was applied
to one path and not the other. **Fix:** add `job.keyword_score = 0.0`.

### 5.5 A dead `or` branch swallows the reason a refresh token was refused · **CONFIRMED**

```python
# app/services/linked_auth.py:134
_note_failure(db, row, f"HTTP {response.status_code}: {detail}"
                       or f"HTTP {response.status_code}")
```

The left f-string always contains at least `"HTTP 401: "`, so it is always
truthy and the right branch is unreachable. When `detail` is empty the stored
note is `"HTTP 401: "` with a dangling colon. The intent was
`detail or f"HTTP {response.status_code}"` — the guard belongs on `detail`, not
on the formatted string. **Fix:**

```python
_note_failure(db, row, f"HTTP {response.status_code}: {detail}" if detail
                       else f"HTTP {response.status_code}")
```

### 5.6 A failing follow-up draft is retried forever · **CONFIRMED**

`outreach.draft_due_follow_ups` promises in its docstring: "The window is
cleared whether or not drafting succeeded, so one contact whose draft keeps
failing cannot be retried forever." But the `except` calls `db.rollback()`,
which undoes the pending `message.follow_up_due_at = None` — so the failing
message keeps its due window and is picked up on every beat tick, burning one
generation attempt every six hours indefinitely.

One reviewer also claimed the rollback discards every earlier draft in the
batch. It does not: `draft_message` commits internally (line 859), so each
success is already durable. The damage is confined to the failing row — which is
still precisely the behaviour the docstring says is prevented.
**Fix:** wrap the attempt in `with db.begin_nested():`, or commit the cleared
window before attempting the draft.

### 5.7 An auth test flakes under the suite's own default · **CONFIRMED**

Two full `-n auto` runs on the same commit: the first failed
`test_agent_api.py::TestAuthentication::test_rejects_a_missing_token`
(`assert 503 == 401`), the second passed everything, and it passes alone at
`-n0`. A 503 from that route means `_migration_failure` or
`auth.misconfiguration()` was set when the request ran — both process-global
state that `monkeypatch` restores at teardown, so the leak is most likely
fixture ordering or a `TestClient` lifespan in a worker already left in a bad
state. Worth chasing rather than re-running: `-n auto` is the default, so it will
reappear in whatever CI lands for §3.2, and an auth test that sometimes passes
for the wrong reason is the worst kind to have flake.

### 5.8 Smaller items, each a line or two · **CONFIRMED / LATENT**

* **`experience_level` defaults to `"mid"` on the fetch path** —
  `job_fetcher.py:1193` still has `job_data.get("experience_level", "mid")`,
  the exact bug `base.parse_experience_level` and `harvest._normalize` document
  at length as fixed. **LATENT**: every live adapter sets the key. It bites the
  first one that forgets, silently. Drop the default.
* **Missing FK `ondelete` policies** — `Application.job_id` and
  `ApplicationDocument.application_id` have none, while `job_scores`,
  `fetch_source_runs`, `contacts` and `outreach_messages` all specify one.
  **LATENT**: nothing in the app deletes an Application or a Job except
  `archive`, which pre-filters rows that have applications. If that filter ever
  changes, the whole 5,000-row archive batch fails on a FK violation. Add
  `ondelete="CASCADE"` for symmetry with the rest of the schema.
* **`Contact.application_id` contradicts its own comment** — the column says
  "Nullable so a contact can outlive the application" and the FK says
  `ondelete="CASCADE"`, which deletes the contact with the application.
  Nullable is not `SET NULL`. **LATENT** for the same reason. If the comment is
  the intent, the policy should be `ondelete="SET NULL"`.
* **`generation_status` transitions are read-then-write, not atomic** — the
  column is a plain `String(20)` with four meaningful values, no CHECK
  constraint and no enum, while `ApplicationStatus` beside it uses `SAEnum`, so
  a typo in any writer is storable. More usefully: every writer selects rows in
  one statement and updates them in another. `doc_refresh.stale_applications`
  *does* filter `Application.generation_status == "idle"` (line 119) — a review
  that said it had no guard was wrong about that — but the filter runs in the
  SELECT and the write happens later, so two passes overlapping could both see
  `idle` and both queue. In practice `refresh_stale_docs` is a single beat task
  and `sweep_generations` re-checks, so this is theoretical rather than
  observed. The clean form is a conditional UPDATE
  (`WHERE generation_status IN ('idle', 'failed')`) with the affected row count
  checked before queueing, which makes the transition atomic and removes the
  question.
* **Four live settings are missing from `.env.example`** —
  `FETCH_LINKED_INTERVAL_HOURS`, `FETCH_LINKED_DEEP_INTERVAL_HOURS`,
  `BROWSE_TOPUP_INTERVAL_MINUTES` and `GEMINI_BASE_URL` are all read from
  `settings` with silent defaults in `config.py`, and none of the four appears
  in `.env.example`. That file is the canonical variable list for whoever
  deploys this, so three schedules and the Gemini endpoint are currently
  untunable-by-discovery. Add them with their defaults and a one-line comment.
* **`_extract_json_object` counts braces without skipping string interiors** —
  `matcher.py:584-600` walks the reply tracking `{`/`}` depth with no awareness
  of quoting, so a model that writes a stray `}` inside its `reasoning` string
  closes the span early, `json.loads` fails on the fragment, and the whole reply
  is discarded as unreadable. Narrow, because plain `json.loads` on the full
  text is tried first and only replies wrapped in prose reach the fallback — but
  it is exactly the reasoning-model case the fallback exists to serve. Skip
  characters inside quoted strings during the depth count.
* **An enrichment crash re-fetches its whole batch** — `enrichment_attempted_at`
  is stamped for every job in one loop after all HTTP work completes, and
  committed once. A crash in that window loses up to 200 jobs' worth of requests
  and the next pass repeats them. Bounded at one batch; stamp in chunks if it
  ever matters.
* **`research_company` is dead code** — `app/tasks/interview.py:19` defines a
  Celery task that nothing ever calls: no `.delay()`, no `beat_schedule` entry.
  `routers/apps.py` does the search inline and synchronously instead. Either
  wire the route to dispatch it (which is what a 300-second
  `soft_time_limit` implies it was for, and would stop a dossier build blocking
  a web request) or delete it.
* **`conftest.py`'s test-database fallback mangles the username** —
  `DATABASE_URL.replace("/jobapp", "/jobapp_test")` is a global replace, and
  `postgresql://jobapp:jobapp@host/jobapp` contains `/jobapp` in the userinfo
  too, so the fallback also renames the role and fails with
  `role "jobapp_test" does not exist`. Masked today because `.env.example` sets
  `TEST_DATABASE_URL` explicitly. `make_url(...).set(database=...)` cannot
  misfire.
* **The 500 page renders the traceback** — `main.py:357` passes it into
  `errors/error.html`, which prints it in a `<pre>`. Behind authentication with
  one user, so a note rather than a finding, but it is on by default with no
  `DEBUG` gate and a traceback here carries SQL and connection details.

---

## 6. Claims that did not survive checking

### 6a. Two proposed fixes that would make things worse

These matter more than the false findings, because a false finding costs an hour
of reading and these cost data.

**"Add a `UNIQUE (source, source_job_id)` constraint."** The value is a guess —
`base._listing_job_id` returns "the longest number in the URL" — and it demonstrably
collides when a date or a tracking parameter outruns the posting id (§2.7).
Today a collision silently merges two postings into one row. Under the proposed
constraint it becomes an `IntegrityError`, the per-row savepoint rolls the
insert back, and the posting is counted as `dropped`. That is strictly worse,
and it runs against this project's own stated rule — `IMPROVING.md`,
"Deliberately not doing": *"a duplicate is a small cost while a deletion is the
largest one available."* Fix the id first; the constraint is safe only once the
collision query in §2.7 returns nothing.

**"On `release()` with no token, attempt a plain `DEL`."** `fetch_lock.py`
already explains why not, in a comment above the Lua script: *"A plain DELETE
would let a cycle that outlived its TTL delete the next cycle's lock, quietly
allowing a third to overlap it."* The compare-and-delete is the whole point of
storing a token. The diagnosis behind the proposal is also impossible as
written — it claims Redis being down leaves `acquire()` returning `True` "but
stores no token" so "the stale lock persists for its full TTL". If no token was
stored, nothing was written, and there is no lock to persist. The real mechanism
is the reverse and is in §3.3: acquire succeeds and writes, then Redis fails
*during release*, and the key survives to its 3600-second TTL. The fix is a
shorter TTL and a retried release, not a blind delete.

### 6b. Findings that did not reproduce

Recorded because knowing what is *not* broken is worth as much as the list
above, and because several of these would have cost real work.

1. **"Missing index on `jobs.status`."** Wrong — `ix_jobs_status` has existed
   since migration 0003. The `jobs` table has twelve indexes; the gaps are
   `source_urls`, `(source, source_job_id)`, `url` and `apply_url` (§1.3).
2. **"Liveness marks jobs closed on WAF pages."** Wrong, and the code is
   explicit about it: `liveness.py:97` returns `unknown` for anything `>= 400`
   other than 404/410, with the comment "403/429/5xx say something about the
   server or about us, not about the posting." `CLOSED_MARKERS` are specific
   phrases none of which appear on a challenge page. Two reviewers reached
   opposite conclusions here; the conservative one is right.
3. **"`asyncio.run()` crashes when called from async contexts."** Not a live
   defect. Both call sites — `job_fetcher.py:585` and `contact_finder.py:509` —
   are reached only from synchronous Celery tasks; no `async def` route touches
   either. (There *is* an unawaited-coroutine `RuntimeWarning` visible in the
   suite around `_run_playwright`, which is worth a look on its own, but it is
   not this.)
4. **"`import fcntl` breaks Windows."** True as a fact, irrelevant as a defect.
   This is a Docker-only Linux deployment — Dockerfile, compose, a Linux VPS,
   pdflatex and Playwright — and nothing claims Windows support. Worth a
   `try/except ImportError` only if a native dev path is ever wanted.
5. **"Mid-cycle commits are not transactional with the job inserts."**
   By design, and the code says so: the savepoint around the board registry
   exists specifically so "a registry problem must not discard the query cache
   or the jobs this cycle is about to save." Committing registry state
   independently is the intended behaviour.
6. **"The profile goes stale across batch chains, so hundreds of jobs are
   scored against it."** Overstated by roughly ten times. Each chained batch is
   a separate Celery task that re-reads `db.query(Profile).first()` at the top
   of `match_all_new_jobs`, so staleness is bounded by one batch —
   `MATCH_MAX_JOBS_PER_TASK`, which is 25 — not by the chain.
7. **"The rollback in `draft_due_follow_ups` discards the whole batch."**
   Mechanism right, blast radius wrong — see §5.6. `draft_message` commits
   internally (line 859), so only the failing row is affected.
8. **"`austria` is a substring of `australia`, so Australian jobs are
   classified as European."** The substring claim is simply false — the two
   words diverge at the fifth letter (`austr·i·a` versus `austr·a·lia`).
   Checked against the real Europe keyword list: `Sydney, Australia`,
   `Melbourne, Australia` and `Perth, Australia` match zero Europe keywords.
   The *conclusion* — word-bound the keyword test — is right anyway, for the
   reasons in §2.5: `usa` really is inside **Jer·usa·lem** and `america`
   inside "South America".
9. **"`doc_refresh` sets `generation_status = 'generating'` with no guard."**
   It filters `Application.generation_status == "idle"` at line 119. The
   residual point — the guard is in the SELECT rather than in an atomic
   UPDATE — is fair and is folded into §5.8.
10. **"Board backfill is marked done while its discovered boards are lost."**
    Cannot happen. `_maybe_backfill_boards` records the boards inside
    `with db.begin_nested():` and writes the `done` flag into the *same*
    `db.commit()` a few lines later, so the flag and the boards land together
    or not at all.
11. **"GIN on `source_urls`" as a standalone fix.** Correct as far as it goes
    and inert on its own: the queries use `.any()`, which emits `= ANY`, and
    GIN cannot answer that operator. Migration 0028 already proves the point —
    it added exactly this index on `archived_jobs`, with a comment explaining
    why it was needed, and the index has never once been used. The index and
    the `.contains()` change have to ship together (§1.3).

On the positive side, one reviewer's read of transaction management in
`job_fetcher` is correct and worth keeping: the per-row savepoints, the
`db.refresh(profile)` before merging the JSONB blob, and the deliberate
withholding of the profile write until just before the commit are all sound, and
the lost-update and lock-hold problems they were written to fix stay fixed. The
one gap in that area is the final commit (§3.4), not the savepoint design.

---

## 7. Fix order

Ordered by damage × certainty ÷ effort.

| # | Finding | Why first | Effort |
|---|---|---|---|
| ~~1~~ | ~~§1.4 deploy `restart nginx` → `caddy`~~ | **done** | |
| ~~2~~ | ~~§1.1 re-scoring loop guard~~ | **done** | |
| ~~3~~ | ~~§3.1 migration out of the two-worker lifespan~~ | **done** | |
| ~~4~~ | ~~§1.2 fetch group lock inversion~~ | **done** | |
| ~~5~~ | ~~§1.3 indexes + `.any()` → `.contains()`~~ | **done** | |
| ~~6~~ | ~~§3.2 a CI workflow~~ | **done** | |
| 7 | §2.1 / §2.2 eligibility | silently deleting whole employers, and asserting the opposite of what postings say | pure functions, existing test file |
| 8 | §2.3 document truncation | the core promise runs on the wrong half of the input | small; structured facts already extracted |
| 9 | §2.4 seniority ordering | wasted paid calls the local prefilter exists to prevent | one block moved |
| 10 | §2.7 run the collision query, then fix the guessed job id | one query; it either clears a suspected silent job-loss path or turns it into a live P0 | minutes to check |
| 11 | §2.5 region matcher · §4.4 archive guard | wrong-continent jobs; permanent row loss | small each |
| 12 | §2.6 salary period | already planned in `IMPROVING.md` §0 | migration + prompt |
| 13 | §4.1 / §4.3 queue split, eager relationship | both config-shaped | small |
| 14 | §5.x the one-liners, `.env.example` included | each is a line or two and several are user-visible | an afternoon together |
| 15 | §3.3–§3.7, §4.2, §4.5–§4.7 | as they come up | — |

Items 1–6 are done: the deploy reaches the proxy that exists, the re-scoring
treadmill stops, one worker migrates instead of two racing, the fetch groups run
beside each other, the dedupe path is indexed, and CI runs the suite. What
follows is where the next work is.

Item 10 is out of severity order on purpose: it is a single read-only query, and
its answer moves §2.7 either off this list entirely or to the top of it.
