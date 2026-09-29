# Intelligence workflow and rollout

This implements the development work from `SMART_SYSTEM_RESEARCH_2026-09-29.md` within the existing FastAPI, PostgreSQL, Celery and browser-extension architecture. It does not require another service, a vector database, or a local model.

## Everyday workflow

Open **Today** (`/today`) and choose the minutes available. Due application actions come first; suggestions explain evidence, readiness, starred jobs and pinned employers. A small configurable portion of the budget can review overlooked postings. Suggestions fit the time budget and avoid repeating an employer. Unknown requirements can be answered once, edited by replacing the answer, or forgotten. Eligibility clarifications expire; the existing remembered-answer store also expires time-sensitive answers and scopes newly captured answers to the employer site/path.

Each job's **Inspect supporting evidence** page links requirements to posting quotations and candidate facts. The assessment distinguishes supported, transferable, conflicting and unknown information. A listed skill is distinguishable from a substantive achievement; a term surviving a PDF is not proof that its underlying claim survived. Non-skill requirements, including years and education, remain questions when evidence cannot establish them. This is intentionally a conservative lexical assessment. It is not a universal ATS score.

Application pages now have an append-only history and a board projection. Record receipts, assessments, interviews, offers, rejections and withdrawals separately. Retracting an incorrect event preserves its original record; retracting the correction restores it. An interview remains in history after rejection. Select **Confirm submitted version** to identify the resume actually used. Initial document links are explicitly inferred; a download is not treated as an upload. Interview prompts quote the linked resume and sit alongside the existing story bank and interview corpus.

Mailbox polling proposes updates under **Updates to review**. It requires a known application and a posting URL or company-and-role identity; company alone is insufficient. Duplicate messages are idempotent. Auto-generated ATS receipts can be proposed without being counted as human outreach replies. Ambiguous associations remain visible for review. The user confirms any milestone; mail is never modified or sent by this feature.

Document generation selects approved achievements within each employer/project, preferring requirement coverage and less redundancy. Existing one-page fitting, content checks and bounded critique passes continue. The final PDF is read back for evidence coverage. The review panel offers a change preview, source bullets and a version-bound save. Manual edits and concurrent generations cannot silently overwrite a newer document. Compilation attempts have unique paths; older saved versions remain downloadable.

The extension reads back filled values after framework updates. Reverted, invalid or unknown fields need input. It checks custom controls and replacement DOM nodes, preserves existing user values, and catches a form step appearing during initial verification. A bounded checkpoint stores structural hashes and field-category statuses, never personal answers. Structure changes invalidate it. A visible receipt can suggest confirmation; only the user marks the application applied and submits the employer's form.

## Defaults and experimental settings

All behavioral controls are declared in `TUNABLES`, editable in Settings and documented in `.env.example`.

| Control | Default | Behavior |
|---|---|---|
| Evidence-assisted matching | `shadow` | Records inspectable assessments while retaining the existing score prompt. `assist` supplies approved achievements and fact IDs to both scoring passes. |
| Outcome ordering | `shadow` | Evaluates mature, explicitly observed outcomes. `assist` contributes a small ordering signal only after the minimum sample and chronological/family holdout gate pass. Silence stays unknown. |
| Semantic retrieval | `off` | No embedding requests. `shadow` compares cached ordering; `assist` contributes a bounded relevance signal to Today without hiding postings. |
| Embedding batch / active window | 20 / 30 days | At most 20 new content/model keyed vectors per maintenance pass over a cohort of at most 200 recent/favorite candidates. No historical-corpus backfill. |
| Intelligence maintenance | 24 hours | Prunes observations and expired vectors, evaluates outcomes and refreshes an enabled semantic experiment. Admission control can defer it. |
| Decision retention | 180 days | Retains feature, score, profile/posting hashes, rank version, origin and position at the time of a decision or displayed suggestion. |
| Daily time budget | 30 minutes | Due actions, clarifications, application preparation and a small exploration budget. Estimates are editable. |

Semantic inference needs `SEMANTIC_BASE_URL` and `SEMANTIC_API_KEY` in the environment plus a model in Settings. The endpoint accepts an OpenAI-compatible `/embeddings` request. No provider or paid model is silently selected. Responses are bounded and validated for count, order, finite values and dimensions. Cache keys include endpoint, model and content; stale vectors are removed after twice the active window. Outcome/preference scores are explicitly uncalibrated ordering signals, not interview probabilities.

The preference model now uses decision-time features and a chronological split with posting-family separation. It evaluates pruned weights against the score baseline; older models without this validation are not promoted. The outcome model additionally uses a maturity window, confirmed outcomes and at least five examples of each class in its holdout. These small comparisons remain uncertain; no causal resume-effect claim is made.

**Privacy controls:** Today exports up to 50,000 records per collection and the saved clarifications. The limit is explicit in the JSON. Delete ranking observations to remove observations and learned models and pause new collection; re-enable collection in Settings when wanted. Application events cascade when their application is deleted. Clarifications can be forgotten individually. Browser checkpoints retain at most 20 structural records for up to 24 hours. Existing profile-answer deletion remains available.

## VPS admission and diagnostics

No new always-running process is added. The capacity controller reads bounded recent page samples, SQL execution time, the oldest queued interactive message and Linux CPU/memory pressure. CPU steal is measured from `/proc/stat` deltas. Unavailable telemetry stays unknown; a telemetry outage fails open rather than taking down the website.

Default admission thresholds are page/DB p95 of 2,000 ms (after five samples), interactive queue age of 180 seconds, or CPU pressure, memory pressure or steal of 80%. A 300-second cooldown prevents rapid toggling. The controller caches its status for 15 seconds and records at most 100 page observations, retaining five minutes for decisions. These are diagnostic samples, not a production SLO guarantee.

The scheduler pauses broad background work while recovery, mailbox and reminders remain eligible. Fetch groups recheck at dispatch and start; matching/enrichment stop chaining; scheduled discovery also checks admission. Already-running operations are not killed, and explicit interactive actions remain available. Source polling backs off after three completed probes with no new or updated postings and retains a periodic probe. Existing per-source quota/error controls still apply; manual source checks bypass adaptive polling.

Existing worker CPU ceilings and queue separation remain in force. Database work is still charged to the database container. Page response and DB latency, queue age and host pressure appear in Today. Existing source diagnostics provide elapsed time and useful-yield counts; per-source CPU and network bytes are not inferred from shared-process measurements. Application changes cannot force Hostinger to lift a restriction immediately.

## Quality checks and promotion

The deterministic suite covers evidence alternatives/negation, invalid fact IDs, missing qualifications, retention, milestone corrections, idempotent mail, outcome censoring, time budgets, stale edits, model opt-in and real Chromium forms including rerenders and false receipts. The existing content-check, date-overlap and browser fixture suites remain part of CI.

Model quality needs frozen human labels separately from these software checks. Build the existing fixture from decisions:

```sh
python -m app.tasks.match_eval --build --per-side 25
```

Review it and add manually audited rejected jobs using the same JSON format. Keep ambiguous labels out or explain their interpretation. Freeze the profile/job contents before tuning; reserve later decisions and whole posting families for a held-out comparison. Do not commit a private profile or mailbox content to a public repository.

Run a paid comparison only deliberately:

```sh
python -m scripts.evaluate_intelligence --path fixtures/match_labels.json --output /storage/evidence-comparison.json --allow-paid --rounds 1 --max-calls 100
```

The harness compares baseline and evidence prompts on the same fixture, records its hash, model, agreement, false rejects/accepts, recall, top-ten usefulness and elapsed time. Existing LLM logs record attempts and provider usage; a configured provider's retries can exceed the requested assessment count. Human rubric dimensions are in `quality_benchmark.rubrics()`. Check factuality, final rendered coverage, editing effort and provider cost as well as ranking. No paid evaluation or hiring-quality improvement is implied by a green regression suite.

Before changing an experiment to `assist`, review the report and human preferences, then observe page latency during representative background work. Keep the default if quality or cost is inconclusive. Mature hiring outcomes and sustained VPS behavior require observation after deployment.

## Deployment and rollback

Push to `main` uses the existing test → GHCR image → VPS migration/readiness workflow. Migrations 0054–0057 add small event/vector tables and a nullable evidence JSON field; they do not rebuild the jobs table or add a jobs index. They do not invent historical events or initiate an embedding backfill. Generation outputs are ordinary versioned documents in existing storage.

The new behavior can be disabled in Settings independently: capacity protection, adaptive source polling, mail suggestions, observation collection and optional ranking signals. On an application-image rollback, additive schema changes may remain. Do not downgrade event-table migrations in production unless their records have been exported; a downgrade drops those tables. Reload the unpacked browser extension to receive its JavaScript changes.

For local container tests, bind `tests/fixtures/empty.env` over `/app/.env`, supply inert test credentials explicitly, and use an internal Docker network containing only the temporary PostgreSQL and Redis services. This prevents a developer's provider keys from entering the test runtime. Run one pytest suite at a time because xdist worker database names are shared.

Local migration validation exercised downgrade from 0057 to 0053 and upgrade back to 0057 successfully. `alembic check` still reports existing model/schema differences for older nullable columns and migration-managed indexes; it reports no drift for the new event/vector tables or evidence column. Do not apply its suggested index removals as an automatic fix.
