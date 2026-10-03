# Working on jobapp

A self-hosted job-search pipeline: fetch postings from many sources, score them
against a profile, generate tailored documents, track applications. FastAPI +
Jinja/HTMX, Celery + Redis, SQLAlchemy + Alembic + Postgres, a Playwright tier,
and a Chrome MV3 extension.

---

## Settings belong in the dashboard, not in `.env`

**Standing instruction from the owner of this repo.** Anything that changes how
the application behaves must be editable from the settings page in the UI.
Never ship a knob whose only adjustment is editing `.env` and redeploying — and
never leave an existing one that way once you touch it.

`.env` is where a value *starts*. The settings page is where it *changes*.

### How to comply

Three steps, all required. Any one of them missing gives you a setting that
looks configurable and is not.

1. **Declare it** in `TUNABLES` in `app/services/tunables.py` — `key`, `env`
   (the matching `app.config.Settings` attribute), `kind`, `group`, `label`,
   and a `help` string that says what it does and what happens at the extremes.
   Set `restart_required=True` if the value is read once at process start.
2. **Read it through `tunables.value(profile_data, key)`**, never
   `settings.THE_ENV_NAME` directly. `value()` resolves the profile override
   first and falls back to the environment; a consumer that reads `settings`
   sees only the environment and silently ignores whatever the user set.
3. **Keep it in `.env.example`** with a comment. That file is the canonical
   variable list for a first deploy, and the env value remains the default the
   override falls back to.

The module docstring in `tunables.py` explains why the shape is this way: the
settings page once wrote `profile.data["settings"]` and nothing read it, so
every field on it was theatre. The declaration *is* the wiring, so a field that
isn't wired up cannot exist.

### The failure mode to watch for

A control that renders in the UI, saves without error, and changes nothing.
It is invisible in tests that only check the form, and it looks to the user
like the feature is broken rather than the wiring. When adding a tunable, write
a test that stores an override on the profile and asserts the *behaviour*
changes — not that the form accepts the value.

```python
profile = Profile(data={tunables.STORE_KEY: {"my_key": 60}})
db.add(profile); db.commit()
# now assert the thing the setting controls actually differs
```

### The one exception

Secrets and infrastructure stay in the environment: API keys, session cookies,
SMTP and IMAP credentials, `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`,
`APP_DOMAIN`. Those are deployment facts, not preferences, and putting them in
a web form means storing them in the profile JSON and rendering them back into
a page.

`tunables.py` states the test for this well: *is this something you would
change to see a different set of jobs?* If yes, it is a tunable. If it is what
lets the application connect or authenticate at all, it is environment.

### Three ways to read a tunable

Which one depends on what you have to hand.

* `tunables.value(profile_data, key)` — one setting, where you can load the
  profile. This is what `routers/jobs._age_cutoff` does.
* `tunables.effective_settings(profile_data)` — a `settings`-shaped overlay for
  code that reads several values as `cfg.THE_ENV_NAME`. `job_fetcher` builds
  one and threads it through `_run_all_adapters` as `cfg`, so adapters have it
  without needing a session of their own.
* `live().THE_ENV_NAME` (`from app.config import live`) — for code with no
  profile to hand. It is the same overlay, read once per web request and once
  per Celery task (`tunables.read_once`), so it is cheap inside either. Outside
  them it reads the profile per call: read it once at the top of a pass, not
  per item, and give a thread pool the caller's context
  (`contextvars.copy_context()`) rather than letting each thread re-read.

### What enforces it

`tests/test_settings_coverage.py` fails when a `Settings` field is neither a
tunable nor named in `tunables.ENVIRONMENT` with its reason, when a tunable is
missing from `.env.example`, and when code under `app/` reads a tunable as
`settings.X` or `getattr(settings, "X")` — which is exactly the read that
ignores the page. `tests/test_settings_take_effect.py` stores an override for
every tunable and checks `live()` returns it; add a behaviour test there (or
beside the feature) for a new one.

### Intervals

Beat reads its schedule once, so an interval in `beat_schedule` can never be a
setting. Put a new periodic task in `app/tasks/schedule.SCHEDULE` with the
tunable holding its interval; the dispatcher ticks every minute and sends it
when that interval, as currently set, has passed.

---

## Testing

- `pytest` runs the suite. `-n auto` is in `addopts`, so it is parallel by
  default and each xdist worker gets its own database.
- **Never run two suites at once.** Worker databases are named by worker id, so
  concurrent runs share those names and corrupt each other's schema. This has
  produced 48 spurious failures in one run and 4 in another.
- **No test may touch the network.** Several have, and they pass or fail on
  whether a third party answers. `conftest.py` turns off the offenders it knows
  about (`ATS_LIST_HARVEST`, board validation); if a new test needs an outbound
  call, mock it at the seam the production code uses — `httpx.get`, not a layer
  above it.
- Production defaults that would make unrelated fixtures fail belong in an
  autouse conftest fixture, not in seventeen fixture edits. See
  `_age_cutoff_off_by_default` for the pattern and the reasoning.

## Migrations

- One concern per migration, with the reasoning in the module docstring.
- `SET LOCAL maintenance_work_mem = '512MB'` before building an index on
  `jobs`. At the 64MB default the sort spills — measured at 10GB written and
  over an hour on the 2-core production VPS.
- Migrations take locks on `jobs`, and the deploy stops the workers before
  running them for that reason. If you add an index to a hot table outside that
  guarantee, take it out of the transaction and use `CONCURRENTLY`.
- Measure before adding an index. A single-column index on `jobs.fetched_at`
  was added on sound-seeming reasoning and the planner ignored it in all four
  call sites, because each leads with a `status` predicate; `(status,
  fetched_at)` is used and removes a sort. Every index also makes every vacuum
  more expensive.

## Deploying

Push to `main`; `.github/workflows/deploy.yml` first runs the reusable test
workflow. Runtime changes build the production target on GitHub Actions,
publish to GHCR, and deploy the resulting immutable digest; documentation and
test-only pushes skip the VPS. `scripts/deploy-vps.sh` preserves the
existing Redis volume, enables AOF before restarting Redis, stops workers for
migrations, and waits for web and proxy readiness before starting workers. It
restores the prior application image if migration or readiness fails. The script records APP_IMAGE
and REDIS_DATA_VOLUME in the VPS `.env`; no registry credentials are retained.
See `docs/DEPLOYING.md`. Never build the production image on the restricted VPS.
