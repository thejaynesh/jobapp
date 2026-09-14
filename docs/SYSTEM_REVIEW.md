# Whole-system review — September 2026

A read of the pipeline end to end, looking for logic that is wrong rather than
work that is missing. `docs/IMPROVING.md` is the design pass; this is the defect
pass, and it deliberately overlaps as little as possible with it.

Everything below was checked against the code, and where a claim is measurable
it was measured — the numbers are from a local Postgres 16 with the schema at
`head`, not estimates. Where a finding is latent rather than live, it says so.

**Suite status at time of writing:** 3,243 tests. Two full `-n auto` runs: one
green, one with a single failure that passes on its own and passed on the
re-run. That is a flake, not a regression. See §D.

One thing worth saying before the list, because it shapes the priorities. The
mechanics here are in good order: three dedupe layers with a stated invariant, a
merge rule with one owner, `manual_fields` in front of every automatic writer,
locks with tokens, savepoints per row, and comments that explain the *reason*
rather than the code. Almost nothing below is sloppiness. The defects cluster
into three shapes:

* **Hot-path reads that are subtly wrong** — a filter that fires on the wrong
  sentence, a region test that admits the wrong country (§A).
* **Infrastructure that has drifted from the code it deploys** (§B). This is
  the highest-severity group and the cheapest to fix.
* **Ceilings that are already close** — index-less scans, one queue, one
  eager relationship (§C).

---

## A. The job-understanding chain

### A1. Two of the three dedupe layers scan the whole table, once per posting

`deduplication.find_existing_job` runs on every fetched posting:

```python
# app/services/deduplication.py:123
job = db.query(Job).filter(Job.source_urls.any(url)).first()          # layer 1
...
.filter(Job.source == source, Job.source_job_id == source_job_id)     # layer 2
...
.filter(Job.dedupe_hash == dedupe_hash)                               # layer 3
```

`jobs` has twelve indexes. None of them covers layer 1 or layer 2:

| Layer | Predicate | Index | Measured (120k rows, miss) |
|---|---|---|---|
| 1 | `url = ANY(source_urls)` | none | **49.6 ms**, 120,000 rows scanned |
| 2 | `source = ? AND source_job_id = ?` | none usable | **32.5 ms**, 120,000 rows scanned |
| 3 | `dedupe_hash = ?` | unique btree | ~0.05 ms |

Layer 2 looks like it has `ix_jobs_source`, but `source` has about twenty
distinct values, so the planner correctly ignores it and scans.

The *miss* case is the one that matters: a genuinely new posting pays both
scans before it is inserted, and new postings are what a fetch cycle is for. At
the ~300k rows this system reports, that is roughly 200 ms per new posting
before anything is written. A cycle taking in two thousand postings spends
several minutes deciding "have we seen this?".

**And the index that exists for this has never been used.** Migration 0028 adds
a GIN index on `archived_jobs.source_urls`, with a comment explaining exactly
why ("GIN, because the URL layer asks 'is this URL in the array' — which a
btree cannot answer and which runs once per fetched posting"). But
`was_archived` queries it with `.any(url)`, which SQLAlchemy emits as
`= ANY(...)`, and **GIN cannot answer `= ANY`** — only the containment operator
`@>`. Measured on the same 120k rows with the GIN index present:

```
source_urls @> ARRAY['…']::varchar[]   →  0.065 ms   (Bitmap Index Scan)
'…' = ANY(source_urls)                 → 48.283 ms   (Seq Scan, 120,000 rows removed)
```

So the fix is two lines of DDL *and* a query-shape change; either alone does
nothing.

```python
# deduplication.py — both call sites
.filter(Job.source_urls.contains([url]))          # emits @>, uses GIN
.filter(ArchivedJob.source_urls.contains([url]))
```

```python
# new migration
op.create_index("ix_jobs_source_urls", "jobs", ["source_urls"],
                postgresql_using="gin")
op.create_index("ix_jobs_source_job", "jobs", ["source", "source_job_id"])
```

Verified after: layer 2 drops from 32.5 ms to 0.056 ms, layer 1 to 0.065 ms.

Related but not the same problem: `job_fetcher._known_urls` (line 638) answers
"have we seen this URL" by pulling every URL on the table into a Python set,
once per cycle. As a bulk membership test that is defensible — one query
instead of N — but it is a second implementation of the dedupe question with
its own rules, and at 300k rows it is tens of megabytes held for the length of
a cycle in each of two worker processes. Worth folding into the indexed lookup
once one exists.

**Severity: high.** Silent, compounding, and it gets worse every week the table
grows.

---

### A2. The eligibility scanner blocks jobs on bare mentions

`eligibility.scan` is a **blocking** filter — a hit sets `filter_reason =
"restricted"` and the job leaves the list. Four of its patterns match a phrase
with no requirement language around it:

```python
# app/services/eligibility.py:74-81
(re.compile(r"(?:top[\s-]secret|ts/sci)\b", re.I),      "Security clearance required"),
(re.compile(r"\bsecret\s+clearance\b", re.I),           "Security clearance required"),
(re.compile(r"\bu\.?s\.?\s+person(?:s)?\b", re.I),      "ITAR / US Person requirement"),
(re.compile(r"export[\s-]control(?:led|s)?\b", re.I),   "Export-control restriction"),
```

Three guards exist (EEO
boilerplate, negation, cased acronyms) and none of them catches "this sentence
is about the company, not about you". Run against realistic text:

| Posting text | Verdict |
|---|---|
| "Acme builds software that helps manufacturers manage export control and trade compliance at scale." | **blocked** — Export-control restriction |
| "Our TS/SCI-cleared customers rely on us. This role is fully remote and open to all." | **blocked** — Security clearance required |
| "Acme collects personal data about U.S. persons and processes it under CCPA." | **blocked** — ITAR / US Person requirement |

The first is every posting at a trade-compliance or GRC vendor. The second is
every posting at a security company that sells to government. Neither role is
restricted; both disappear, and product principle 3 ("every automatic decision
shows its evidence") is technically satisfied by quoting a sentence that is
about the customer base.

The other patterns already model this correctly — `must (?:be|hold|
possess|have) .{0,40}?clearance` requires the obligation. The bare-mention
patterns should do the same: require a requirement verb within the sentence, or
demote them from blocking to advisory. Blocking is the tier with the
irreversible consequence, so it should be the tier that demands the most
evidence.

**Severity: high** for anyone whose target roles touch defence, aerospace,
fintech compliance, or security vendors. Cheap to fix, and easy to test — the
module is pure and already has a test file.

---

### A3. Sponsorship direction is read from the whole sentence

`_classify_sponsorship` (eligibility.py:238) searches the entire sentence for
any negation word and calls the result negative. Two verified misreadings:

| Posting text | Recorded | Correct |
|---|---|---|
| "Although we cannot offer relocation assistance, visa sponsorship is available for this role." | negative | positive |
| "Sponsorship is provided at no cost to the candidate." | negative | positive |

The first negation belongs to a different clause; the second is the literal
words "no cost". The module's own comment defends word boundaries because "as
bare substrings, 'no' matches 'now'" — the boundary is there, and `no cost` is
still a whole-word `no`.

This is advisory-only, so it changes no score and loses no job. But the badge is
shown on the job list, the detail page, the apply queue and the extension
overlay — four places where the product asserts, in the employer's name, the
opposite of what the employer wrote. Under principle 4 that is worse than
showing nothing.

The fix is scope: classify on the clause containing the `sponsor` token (split
on `,` / `;` / ` but ` / ` although `), not on the sentence; and check the
positive pattern before the negative one when both match.

---

### A4. The region filter admits jobs from the wrong continent

`locations._region_matches` (line 231) tests two things, and both over-match:

* `keywords` are plain substrings with no word boundary.
* `abbrevs` for `usa` are the 50 two-letter state codes, matched
  case-sensitively — and **two-letter US state codes collide with ISO-3166
  country codes.**

Verified against `prefs = {"regions": ["usa"]}`:

| Location text | Matches `usa` via |
|---|---|
| `Toronto, CA` / `Vancouver, CA` | `CA` (California / Canada) |
| `Berlin, DE` / `Munich, DE` | `DE` (Delaware / Germany) |
| `Bengaluru, IN` | `IN` (Indiana / India) |
| `Tel Aviv, IL` | `IL` (Illinois / Israel) |
| `Valletta, MT` | `MT` (Montana / Malta) |
| `Panama City, PA` | `PA` (Pennsylvania / Panama) |
| `Jerusalem, Israel` | substring `usa` in **Jer-usa-lem** |
| `South America` | substring `america` |

`location_allowed` tests the user's own regions *first* and returns `True` on
the first hit, so all of these pass the gate as "matches your preferences".
They then cost a full scoring call each and land in the list.

Two contained fixes:

* Require a word boundary on multi-character keywords, or at minimum drop the
  three-letter ones (`usa`, `u.s.`) to a boundary match.
* Only accept a two-letter state code when something else in the string already
  says United States, or when it is preceded by a comma **and** the string has
  no other country signal. A simpler version that removes most of the damage:
  check the *other* regions first and return `False` on a match, so
  `Toronto, CA` loses to `canada`'s explicit `toronto` keyword.

Note `deduplication.normalize_location` does **not** have this bug — it strips
the same tokens by name and falls back when nothing survives. The two functions
solve adjacent problems with opposite care.

---

### A5. A `low_score` rejection can describe a penalty it did not apply

```python
# app/services/matcher.py:1126
penalty = " (after a 15-point seniority penalty)" if not llm_result.get(
    "seniority_fit", True) else ""
job.filter_detail = f"AI scored this {score}/100{penalty}, below your minimum of {min_score}."
```

`score` at this point is the **deep** score when the second pass ran (line
1089), but `llm_result` is the **first** pass. When the two passes disagree on
`seniority_fit`, the sentence either claims a penalty that was not applied to
the number it quotes, or omits one that was.

One-line fix: carry the result that produced `score`.

```python
verdict = deep_result if deep_result is not None else llm_result
penalty = " (after a 15-point seniority penalty)" if not verdict.get("seniority_fit", True) else ""
```

**Severity: low** in effect, but this is the sentence the user reads to decide
whether to override the filter, and the codebase treats that as load-bearing.

---

### A6. The two ingest paths disagree about `experience_level`

`base.parse_experience_level` returns `None` when a posting gives no signal, and
its docstring spends a paragraph on why: "'mid' was never a finding — it was the
fallback … a posting that says 'Mid-level Engineer' and one that says nothing at
all" became identical, the jobs-page filter returned every unclassifiable
posting under "Mid", and `enrich_from` could not merge the column.
`harvest._normalize` was fixed to match, with its own comment
(`harvest.py:745`).

The API fetch path was not:

```python
# app/services/job_fetcher.py:1193
experience_level=job_data.get("experience_level", "mid"),
```

Today this is **latent** — every adapter reachable through `_run_all_adapters`
sets the key, either directly or through `base.jobs_from_listing`. It bites the
first adapter that forgets, and it will do so silently: the column fills with
`"mid"`, the scoring prompt states it as a fact, and `_FILL_IF_NULL` can no
longer merge a real value in from a second sighting. Change it to
`job_data.get("experience_level")`.

---

### A7. The title gate is far looser than its name

`_title_matches_roles` passes on **any single word overlap** with any target
role or any LLM-expanded query. With "Software Engineer" among the roles, every
"Sales Engineer", "Civil Engineer" and "Field Service Engineer" passes the gate
labelled "Title doesn't match target roles".

This is a deliberate fail-open and it is the right default for the filter. But
the same predicate is reused as the *priority* function for enrichment
(`enrichment._title_gate`, used by `select_targets`), where fail-open means
nearly every candidate ranks in the first bucket and the ordering carries almost
no information. If the enrichment queue is meant to work the most promising jobs
first, that ranking needs a stricter test than the filter's — for instance
requiring overlap on a role's *head noun* plus one qualifier.

**Severity: low.** Worth knowing before trusting the enrichment ordering.

---

## B. Infrastructure that has drifted from the code

This group is the highest severity in the review and the cheapest to fix.

### B1. Every deploy fails, and deploys three times

`.github/workflows/deploy.yml` ends each of its three scripts with:

```
docker compose -f docker-compose.prod.yml restart nginx
```

There is no `nginx` service in `docker-compose.prod.yml` — the proxy is
`caddy`. Verified locally:

```
$ docker compose -f docker-compose.prod.yml restart nginx
no such service: nginx
EXIT=1
```

The step therefore fails *after* a successful build and migration. That trips
`continue-on-error` → sleep 120 → **full rebuild + `alembic upgrade head`
again** → fails again → sleep 240 → **third rebuild + migration**, this time
with no `continue-on-error`, so the workflow ends red.

Every push to `main`: three image builds, three migration runs, ~7 minutes, and
a red check that says nothing about whether the deploy worked. The retry ladder
was built for intermittent SSH timeouts and is now firing on a certainty.

**Fix:** `restart caddy`, or delete the line — `up -d --build` already restarts
what changed, and the Caddyfile is a read-only bind mount that Caddy does not
need a restart to pick up unless it changed.

---

### B2. Nothing runs the tests

`deploy.yml` is the only workflow in the repository. 3,243 tests, a four-minute
parallel suite, and no gate between a push and production. Given how much of
this system's correctness lives in those tests — the eligibility scanner, the
merge rules, the dedupe invariant — that is the single biggest process gap.

A minimal `test.yml` (postgres service container, `pip install -e ".[dev]"`,
`pytest`) is a dozen lines and would have caught nothing in this review, which
is exactly the point: it protects the next change, not this one.

---

### B3. `make up` starts a broken proxy

```yaml
# docker-compose.yml
nginx:
  volumes:
    - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf
```

`./nginx/` has never existed in this repository — `git log -- nginx/` is empty,
and the tree has `caddy/` instead. Docker creates an empty *directory* at that
path and mounts it, so the container comes up serving the stock nginx page.
Harmless because port 8000 is published directly, but it means `make up` always
leaves one container in a wrong state and one stray directory in the working
tree. Either point it at `caddy/Caddyfile` or drop the service from dev.

---

### B4. Two uvicorn workers race the migration at startup

```yaml
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

```python
# app/main.py:105 — inside the lifespan
result = subprocess.run(["alembic", "upgrade", "head"], ...)
if result.returncode != 0:
    _migration_failure = detail          # module-level global
```

With `--workers 2`, uvicorn forks two processes and **each runs the lifespan**,
so two `alembic upgrade head` run concurrently against the same database. When
there is anything to apply, one commits and the other fails on an object that
now exists. The loser sets `_migration_failure`, and the middleware then serves

```
503 — The database schema is not up to date, so the application is refusing
      to serve against it.
```

to every request that process receives, for the life of the process. Both
workers share the listening socket, so roughly half of all requests 503 after a
deploy that, from the outside, succeeded. `_migration_failure` is only cleared
by another lifespan, so it does not heal.

B1 makes this worse: the retry ladder runs `alembic upgrade head` three times,
against a stack that is simultaneously restarting.

**Fix:** take the migration out of the request path. Run it once as a
pre-start step (the deploy script already does, line 24), and have the lifespan
*verify* instead — compare `alembic_version` to `script.get_current_head()` and
set `_migration_failure` on a mismatch. Same guarantee, no write, no race. If it
must stay in the lifespan, wrap it in a Postgres advisory lock.

---

### B5. Generated documents are served without authentication

```
# caddy/Caddyfile:24
handle_path /storage/* {
    root * /storage
    header Content-Disposition attachment
    file_server
}
```

Caddy serves this from the shared volume; the request never reaches FastAPI, so
`require_authentication` never runs. What is in there is
`{application_id}/{application_id}_resume_v1.pdf` — a tailored resume carrying
the user's full name, address, phone number, email and complete work history.

The middleware's own docstring explains why it is middleware and not a
dependency: "the failure mode of forgetting is an endpoint that silently serves
the user's application history to the internet." This is that endpoint, reached
from outside the application.

The UUID makes it unguessable in practice and `file_server` will not list the
directory, so this is obscurity rather than exposure — but it means any leaked
URL (browser history, a referrer, the extension, a shared link) is permanently
public, and it is the one place the stated security model is not enforced.

**Fix:** either serve documents through an authenticated FastAPI route
(`FileResponse` behind the existing middleware), or add
`forward_auth`/`basicauth` to the `/storage` handler in the Caddyfile.

`docs/IMPROVING.md` says "There is no security work" — so this is offered as
information, not as an argument about priorities.

---

### B6. The login throttle can be stepped over

```python
# app/routers/auth.py:17
def _client(request: Request) -> str:
    """Behind nginx every request arrives from the proxy, so the forwarded
    address is the only thing that distinguishes callers."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
```

`X-Forwarded-For` is a client-supplied header, and Caddy's `reverse_proxy`
**appends** the peer address to whatever the client sent rather than replacing
it (this is why `trusted_proxies` exists). So a request carrying
`X-Forwarded-For: 1.2.3.4` arrives at the app as `1.2.3.4, <real ip>`, and
`split(",")[0]` returns the attacker-chosen half. Rotating that header per
request means `MAX_ATTEMPTS = 5` / `LOCKOUT_SECONDS = 300` never engage.

The general rule is the same whatever the proxy does: the only entry in an XFF
chain you can trust is the **last** one, because that is the one your own hop
added. Reading the first is reading whatever the client typed.

`auth.py`'s own docstring states the threat this defends against: "One password
and no user database means an unthrottled login form is a plain offline-speed
guessing target."

**Fix:** take the **last** XFF entry (the hop Caddy itself added), or drop XFF
entirely and use `request.client.host` — with one tenant, a single global
throttle bucket is not a limitation. The docstring also still says "nginx".

---

### B7. The 500 page renders the traceback

`main.py:357` passes `traceback` into `errors/error.html`, which renders it in a
`<pre>` (line 41). It is behind authentication and there is one user, so this is
a note rather than a finding — but it is on by default with no `DEBUG` gate, and
a traceback through this codebase carries SQL and connection details.

---

## C. Ceilings that are already close

### C1. One queue, two slots, two self-chaining 25-minute tasks

`celery_app.conf` sets no `task_routes` and declares no queues, so everything
shares `celery`. Production runs `--concurrency=2`. Both `match_jobs` and
`enrich_jobs` have `soft_time_limit=1500` (25 min) and re-queue themselves while
there is more to do.

The consequence: the two batch chains can hold both slots, and a user clicking
"Generate documents" or "Fetch now" waits behind up to 25 minutes of LLM round
trips with no feedback other than a spinner. `worker_prefetch_multiplier=1` makes
this fair but not fast.

**Fix:** two queues and a second worker container — `batch` (match, enrich,
fetch, archive, prune, backup) and `interactive` (generate, agent-triggered
work, manual triggers) — `task_routes` by task name, `-Q` per worker. It is a
compose change plus about ten lines of config.

### C2. Beat publishes regardless of queue depth; most tasks have no lock

Four of the thirteen scheduled tasks take a Redis lock and no-op when a pass is
already running (`fetch`, `match`, `enrich`, `compare_models`). The other nine —
`poll_mailbox` (every 15 min), `top_up_browsing` (30 min), `sweep_generations`
(20 min), `check_postings`, `archive_old_jobs`, `refresh_stale_docs`,
`process_followups`, `prune_llm_log`, `prune_agent_history` — do not. During any
window where both slots are busy, beat keeps publishing and the copies run
back-to-back afterwards. For `poll_mailbox` that means several IMAP sessions in
a row; for `sweep_generations`, several unbounded scans.

`sweep_generations` is also unbounded in another sense: its "never queued" query
loads every matched application with no `LIMIT`, and touches `app.documents`
per row.

### C3. `Job.scores` is eagerly loaded everywhere, for one page

```python
# app/models/job.py:164
scores: Mapped[list["JobScore"]] = relationship(..., lazy="selectin", ...)
```

The comment justifies it by the jobs list page rendering score history on every
card. That is true, and every batch path pays for it too — there is no `noload`
anywhere in the codebase. Per run: `archive.candidates` loads 5,000 Job objects,
`requeue_settled_verdicts` 4,000, `enrichment.select_targets` up to 1,000,
`liveness.candidates` 200. Every one of them fires a second query for score rows
nobody reads, on top of de-TOASTing the descriptions.

**Fix:** `lazy="select"` on the relationship plus an explicit
`.options(selectinload(Job.scores))` on the jobs list route — one line moved,
same page behaviour, and the batch paths stop paying.

### C4. `requeue_settled_verdicts` will wall, for the reason it says it avoids

```python
# app/services/enrichment.py:812
# No ordering, deliberately. … Unordered, every pass makes progress, and the
# whole set is a day's work.
```

The rows that pass the in-Python length test are updated (`status = new`), which
in Postgres writes a new tuple version elsewhere in the heap and removes them
from the filter. The rows that *fail* it — still thin — are not written at all,
so they stay exactly where they are. A sequential scan returns them first again
next pass, and the proportion of a batch that qualifies falls monotonically.

That is the same starvation `select_targets` needed `enrichment_attempted_at` to
escape, one function further down the file. The same fix works: stamp a column
when a row is examined and excluded, and filter on it.

### C5. Archiving and enrichment compete for the same rows, and archiving wins

`archive._eligible` takes any `filtered_out` job older than
`ARCHIVE_AFTER_DAYS` (60) whose reason is not user-made:

```python
PROTECTED_REASONS = frozenset({"manual", "blocked_title", "excluded_company"})
```

`DESCRIPTION_DEPENDENT_REASONS` — `no_description`, `few_skills`, `low_score`,
`restricted`, `seniority` — is not protected. Those are precisely the rows the
whole enrichment subsystem exists to rescue, and archiving is irreversible for
this purpose: the description is what archiving discards, and `was_archived()`
then makes the fetcher skip the posting on every future cycle.

So any job enrichment has not reached within 60 days leaves the pipeline
permanently, and the module docstring's claim that "a job filtered on a title
mismatch in June is not going to be reconsidered" is true of `title_mismatch`
and false of the five reasons above — `enrichment._worth_rescoring` says so
directly.

Whether this is currently losing jobs depends on whether enrichment drains
faster than 60 days; with ~58,700 rows parked under description-dependent
verdicts and `ENRICH_MAX_PER_RUN = 200`, it is worth measuring before assuming
it does. Cheapest guard: exclude rows that still have `enrichment_attempted_at
IS NULL` from archiving, so nothing is retired before it has been tried once.

### C6. Liveness cannot keep up past ~1,200 matched jobs

`LIVENESS_MAX_PER_CYCLE = 200`, `LIVENESS_INTERVAL_HOURS = 12`,
`LIVENESS_RECHECK_DAYS = 3` → 400 checks a day, sustaining 1,200 jobs on a
three-day cycle. `candidates()` orders `liveness_checked_at ASC NULLS FIRST`, so
newly matched jobs always jump ahead of stale re-checks. Past the ceiling, the
oldest matched jobs stop being re-checked entirely and keep showing a "still
open" state that is months old — which is the exact failure the module was
written to remove, relocated from "never checked" to "checked once".

Worth surfacing the ratio on `/runs` (matched jobs ÷ daily check budget) rather
than raising the budget blind.

---

## D. Test health

* **A flake in the auth tests.** Two full `-n auto` runs on the same commit:
  the first failed
  `tests/test_agent_api.py::TestAuthentication::test_rejects_a_missing_token`
  (`assert 503 == 401`), the second passed everything. The test passes on its
  own at `-n0`. A 503 from that route means either `_migration_failure` or
  `auth.misconfiguration()` was set when the request ran, and both are
  process-global state that `monkeypatch` restores at teardown — so the leak is
  most likely a fixture ordering or a `TestClient` lifespan running in a worker
  that had already been left in a bad state. Worth chasing rather than
  re-running: `-n auto` is the suite's own default, so this will reappear in
  whatever CI gets added for §B2, and an auth test that sometimes passes for
  the wrong reason is the worst kind to have flake.
* `tests/conftest.py:12` derives the test database by string replacement:
  ```python
  settings.TEST_DATABASE_URL or settings.DATABASE_URL.replace("/jobapp", "/jobapp_test")
  ```
  `str.replace` is global and `postgresql://jobapp:jobapp@host/jobapp` contains
  `/jobapp` in the **userinfo** as well, so the fallback also renames the role
  and fails with `role "jobapp_test" does not exist`. Masked today because
  `.env.example` sets `TEST_DATABASE_URL` explicitly. `make_url(...).set(
  database=...)` is the version that cannot misfire.
* `app/main.py` shells out to `alembic` by bare name, so the app's startup
  depends on PATH. Fine in the container; it is why a venv-based run of the
  suite returns 503 from every HTTP test until `PATH` includes the venv's
  `bin`. Worth a line in the README if anyone ever runs the suite outside
  Docker.

---

## E. Already tracked, confirmed still live

Not re-argued here, just confirmed against the current tree:

* **Salary has no period** (`docs/IMPROVING.md` §0). `jobs.salary_min/max` still
  mix hourly, annual and multiple currencies in one column, and
  `routers/jobs.py:232` still compares them against a single floor. Still the
  right thing to do first.
* **Numerators without denominators** (`IMPROVING.md`, passim). Agreed, and §A1
  and §C6 above are two more instances: the dedupe cost and the liveness
  coverage ratio are both invisible today.

---

## Suggested order

Ordered by (damage × certainty) ÷ effort, not by section:

1. **B1** — `restart nginx` → `restart caddy`. One word; every deploy stops
   failing and stops running three times.
2. **B4** — take `alembic upgrade head` out of the two-worker lifespan. This is
   the one that intermittently 503s production.
3. **A1** — two indexes plus `.any()` → `.contains()`. Measured 500×–750× on the
   hot path, and it makes an index already in the schema start working.
4. **B2** — a CI workflow that runs the suite.
5. **A2 / A3** — eligibility false positives. Pure functions, already have a
   test file, and A2 is silently deleting whole employers from the list.
6. **A4** — region matcher. Wasted scoring calls and wrong-continent jobs.
7. **C5** — stop archiving rows enrichment has never attempted.
8. **C1 / C3** — queue split and the eager relationship; both are config-shaped.
9. **A5, A6, A7, C2, C4, C6, B3, B5, B6, D** — as they come up.
