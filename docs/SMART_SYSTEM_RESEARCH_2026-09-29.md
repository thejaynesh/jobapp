# Making Jobapp smarter: research and implementation strategy

Research date: September 29, 2026. Local baseline: `2afe7bafee179a8810d5de5a39e4998da9f126fa`.

## Recommendation

Build a personal job-search assistant that can explain **which opportunity deserves attention, what evidence supports the application, what to do next, and what happened afterward**. Optimize for worthwhile interviews per hour of the user's effort, with truthfulness and reliability as requirements.

The existing application already has extensive discovery, matching, document generation, browser assistance, and tracking. The next substantial improvement is connecting those capabilities through verified evidence and trustworthy history. More sources, more model calls, and more autonomous agents are weaker investments until those connections work.

My recommended sequence is:

1. Protect responsiveness and establish quality/cost baselines.
2. Give matching the candidate evidence it currently omits; produce requirement-level explanations.
3. Record decisions, submissions, and outcomes as durable events.
4. Build a daily action queue using those records and the user's available time.
5. Improve document selection and browser verification around that queue.
6. Introduce semantic retrieval and learned outcome ranking only when evaluation justifies them.

This is a research proposal, not a claim that these changes have been implemented or that they will produce a particular interview uplift.

## Research method and limits

I compared the current source with relevant public GitHub repositories, inspected selected implementations and tests, checked repository redirects and license metadata, and read official product documentation and primary research. Six repositories received implementation-level inspection; three more were screened as references. Commercial features were evaluated from their documentation, not through paid-account testing. External test suites and applications were not executed.

Repository stars, README feature lists, vendor testimonials, and an AI-generated score are not evidence of improved hiring outcomes. Existing `docs/JOB_SEARCH_RESEARCH.md` already covers extensive ATS/source research and work shipped from it. This report deliberately extends that work rather than recommending the same integrations again.

The Hostinger constraints are the user's reported 2 CPU cores and 8 GB RAM. Production latency, current CPU restriction status, actual token expenditure, and the number of labeled applications were not measured during this research. Proposed budgets and acceptance targets below need a production baseline.

## 1. What we already have, and the actual gaps

| Area | Current implementation | Incremental improvement |
|---|---|---|
| Discovery | Many ATS adapters, canonical posting identities, company-board discovery, browser harvest, source-specific cooldowns, unique-source yield and reference-list recall | Allocate collection effort using marginal useful yield, freshness, resource cost, and coverage constraints |
| Matching | Deterministic filters, TF-IDF similarity, an LLM rubric, a second model for a score band, score history, user feedback and an evaluation harness | Feed relevant achievements into scoring; distinguish evidence, inference, missing information, preference, and explicit incompatibility |
| Personalization | Small logistic model trained on applied/starred versus dismissed jobs; random holdout comparison with the match score | Record what was shown and what was known at decision time; evaluate chronologically and distinguish preference from employer response |
| Documents | Structured content, version history, manual editing, profile facts, approved bullet variants, story bank, generation checks, PDF text extraction, one-page fitting | Requirement-to-evidence links, incremental proposed edits, explicit coverage after rendering, actual submitted-version confirmation |
| Browser assistance | Profile autofill, radio/custom controls, remembered answers, draft essay answers, multi-step observation, real-browser fixtures; user submits | Verify filled values, persist checkpoints, detect submission evidence, and show unresolved fields explicitly |
| Tracking | Application board, reminders, next actions, current-status response rates, a sent-resume reference; mailbox recognizes outreach replies and bounces | Application event history, evidence-based mail reconciliation, historical interview milestones, time-to-response cohorts |
| Operations | Separate interactive/batch workers, CPU ceilings, call budgets, durable ingestion retries, Redis persistence, CI-built deployment image | Coordinate these controls using web latency, queue age, database load, and host pressure |

Sources are the inspected local services: `matcher.py`, `similarity.py`, `for_you.py`, `match_report.py`, `match_eval.py`, `source_yield.py`, `doc_generator.py`, `content_checks.py`, `document_content.py`, `tracker.py`, `mailbox.py`, and the extension README at the baseline commit.

### Four concrete findings that change the priorities

**The matcher omits valuable candidate evidence.** `_build_match_prompt()` renders experience as title/company, duration, and technologies. It does not include those entries' achievement bullets, supplied facts, or approved bullet-bank content. The deeper model uses the same prompt builder. Generation already has an evidence builder, so matching is working from a thinner candidate description than document writing. Start by selecting and sharing a bounded set of relevant, attributable facts. [Matching prompt](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/app/services/matcher.py#L662), [generation evidence](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/app/services/doc_generator.py#L800).

**The personalization evaluation does not preserve the decision context.** `fit()` reconstructs features using today's job records and `now`, then randomly splits decisions. `decisions()` reads the current score. A job's age, description, or score can differ from what the user saw. Similar jobs from the same employer can also cross the split. This is an evaluation weakness, not proof that the ranker performs poorly. Capture decision-time features and evaluate on later periods, with duplicate families kept together. [Ranker](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/app/services/for_you.py#L125), [decision labels](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/app/services/match_report.py#L76).

**The outcome report can forget an interview.** `response_rates()` counts an interview only when the current status is `interviewing` or `offered`. Moving an application from interviewing to rejected removes its interview from that calculation. Historical milestones should survive later status changes. Also, rejected applications count as “heard back”; that metric must stay separate from positive responses. [Outcome calculation](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/app/services/tracker.py#L179).

**The recorded resume is an assumption about submission.** `set_status()` records the current resume when an application first enters a sent status. That is useful bookkeeping, but does not establish which file was uploaded outside the app. Mark this as inferred until a user or supported browser receipt confirms it. [Status transition](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/app/services/tracker.py#L69).

These findings came from source inspection, not a new production incident or a benchmark run.

## 2. GitHub projects worth learning from

All six implementation references below were active, non-archived repositories when checked. Licenses are GitHub's reported SPDX identifiers; any future code reuse should also review the actual file notices. These are selective references, not recommendations to replace our stack.

| Project / inspected revision | Concrete evidence inspected | What to adopt | Boundary or tradeoff |
|---|---|---|---|
| [Resume Matcher](https://github.com/srbhr/Resume-Matcher), Apache-2.0, `9c05e42` | Bullet relevance scoring feeds deterministic selection and page fitting, with explicit deadlines and fallback warnings; separate opt-in generated-output evals | Select the best existing evidence before rewriting; bound rendering and review work | We already select experience/projects and fit pages. Improve selection granularity and traceability rather than rebuilding generation |
| [Reactive Resume](https://github.com/reactive-resume/reactive-resume), MIT, `1fc835e` | Structured resume schema; typed patch proposals carrying operations and a base update timestamp | AI suggestions as inspectable edits; version-aware application of changes | A valid schema does not prove a statement is true. Preserve factual checks and our existing document versions |
| [JobSync](https://github.com/Gsync/jobsync), MIT, `ef3d7ee` | An add-job tool binds user identity outside model inputs, preserves original pasted text, and has explicit approval tests; resume-review eval assertions | Narrow assistant tools, trusted identity boundaries, source preservation, concrete action previews | Do not copy its confirmation requirement for every local write. Low-impact actions can remain fast under our product's authorization rules |
| [ApplyPack](https://github.com/applypack/applypack), MIT, `ec38fcb` | Screening anchors check model-provided quotations; unsupported gate verdicts become unknown | Validate claimed supporting passages in code; expose unknowns; derive summaries from a visible rubric | This inspected module is employer-side screening. Its weights and labels are not validated candidate interview probabilities |
| [Stagehand](https://github.com/browserbase/stagehand), MIT, `ad2bf12` | Action/observation/extraction caching, DOM-related cache inputs, failure fallback, current caching documentation | Reuse verified form mappings and invalidate them when the page changes | Its inspected v4 managed cache requires Browserbase credentials/session. This is not a free local-extension cache we can enable unchanged |
| [JobSpy](https://github.com/speedyapply/JobSpy), MIT, `96e1b12` | A common scraping entry point, site adapters, bounded result requests, and normalized output | Adapter contracts and independent small coverage comparisons | It is a scraper library, not durable orchestration or proof of full-market coverage. Adding it wholesale would overlap existing readers and introduce another failure surface |

Implementation references: [Resume Matcher selection](https://github.com/srbhr/Resume-Matcher/blob/9c05e423dfde44a5b4bb398d2dc7507194252ded/apps/backend/app/services/tailor_selection.py), [generated eval](https://github.com/srbhr/Resume-Matcher/blob/9c05e423dfde44a5b4bb398d2dc7507194252ded/apps/backend/tests/evals/test_tailoring_eval.py), [Reactive Resume proposals](https://github.com/reactive-resume/reactive-resume/blob/1fc835e5f296d579ab88685de9dc573b174c6533/packages/ai/src/tools/patch-proposal.ts), [JobSync tool](https://github.com/Gsync/jobsync/blob/ef3d7eec5c9aebaa1c6e03fc3ddd62d7dca8e37f/src/lib/agent/tools/addJob.ts), [JobSync tests](https://github.com/Gsync/jobsync/blob/ef3d7eec5c9aebaa1c6e03fc3ddd62d7dca8e37f/__tests__/agentAddJobTool.spec.ts), [ApplyPack evidence anchoring](https://github.com/applypack/applypack/blob/ec38fcb6c9b0cd9fb86e4ebbb263d7b4746e5517/src/screening/anchor.ts), [Stagehand cache implementation](https://github.com/browserbase/stagehand/blob/ad2bf12ea7abd95bb1d6f3a59600842a0954fffb/packages/extension/services/cacheService.ts), [Stagehand cache limitations](https://github.com/browserbase/stagehand/blob/ad2bf12ea7abd95bb1d6f3a59600842a0954fffb/packages/docs/v4/best-practices/caching.mdx), [JobSpy entry point](https://github.com/speedyapply/JobSpy/blob/96e1b12c490fb339f136521741df9ab670da5b60/jobspy/__init__.py).

Additional screened references:

- [browser-use](https://github.com/browser-use/browser-use): useful as a future browser-agent benchmark, but a general autonomous browser is not the immediate missing component. Only metadata/project scope was assessed here; no claim of verified suitability for our ATS flows.
- [mattohan567/job-application-agent](https://github.com/mattohan567/job-application-agent): a small human-reviewed workflow reference. Insufficient evidence to treat it as an operational replacement.
- The historic [AIHawk URL](https://github.com/feder-cr/AIHawk) now resolves to `feder-cr/invisible_playwright_mcp`. Current metadata describes a general browser-automation project. Old “AIHawk job bot” comparisons do not accurately describe its current scope. Its automation/stealth positioning is not the direction recommended here.

Repository discovery needed correction: old JobSpy and Reactive Resume owner URLs redirect, and the canonical Resume Matcher repository is `srbhr/Resume-Matcher`. This is why the report uses verified repository identities and pinned implementation links.

## 3. Lessons from commercial tools and primary research

**Simplify: continuity through the application.** Its documented Copilot workflow combines profile fields, saved answers, document choice, user review, and tracker updates after applying. We already cover much of this. The useful remaining lesson is to verify the transition from prepared application to actual submission and make unsupported fields easy to finish. Its published coverage numbers were not independently verified. [Simplify documentation](https://help.simplify.jobs/articles/2415391-using-copilot-to-autofill-applications).

**Teal: curate a master library.** Its resume workflow starts with a comprehensive collection of experience and selects relevant material for an individual job. Its documented auto-selection works on existing content and leaves the user in control. Our bullet bank is a suitable foundation; the next step is explaining why each selected achievement supports a requirement. The older auto-selection help page was available in search indexing but returned 404 on direct retrieval, so current workflow conclusions also use its accessible recent tailoring guide. [Teal tailoring guide](https://help.tealhq.com/en/articles/14435726-how-to-tailor-your-resume-for-a-specific-job), [Teal resume builder](https://www.tealhq.com/tools/resume-builder).

**Huntr: preserve the journey.** Its tracker documents activities, contacts, documents, and funnel/time metrics. The relevant improvement for us is a coherent timeline that preserves milestones and links them to the exact materials and people involved. Our existing board does not need replacing. [Tracker documentation](https://help.huntr.co/en/articles/9883324-job-tracker), [metrics](https://huntr.co/product/job-search-metrics).

**A model score is not a hiring probability.** A NAACL 2025 observational study compared GPT-4 and human ratings on 736 resumes and found limited correlation. This is evidence against treating an uncalibrated LLM score as interchangeable with human evaluation; it is not proof about every newer model or our target jobs. Keep fit, preference, evidence completeness, and observed employer outcomes separate. [Paper](https://aclanthology.org/2025.findings-naacl.270/).

**Better retrieval needs measurement.** Sentence Transformers documents retrieving candidates cheaply and reranking a smaller set. BEIR found that a lexical baseline remained competitive across heterogeneous tasks and that strong rerankers carried computational costs. Neither establishes the best model for our job corpus. Compare hybrid retrieval against our current filters and TF-IDF before deployment. [Retrieve and rerank](https://sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html), [BEIR](https://arxiv.org/abs/2104.08663).

**Feedback is selected by the system itself.** Recommendation research explains how user self-selection and system exposure bias training/evaluation data. If we only label jobs the current ranker shows, we cannot measure the opportunities it hid. Record impressions and keep a small explicitly labeled exploration sample. Sophisticated propensity correction is a later option requiring reliable exposure probabilities and enough data, not a default fix for a small personal dataset. [Recommendations as Treatments](https://arxiv.org/abs/1602.05352).

**Complexity should earn its cost.** Anthropic's engineering guidance favors composable workflows and explicit stopping conditions. Its evaluation guidance combines deterministic checks, calibrated model graders, and human review. LangGraph's persistence documentation separates resumable workflow state from long-lived user memory. Borrow those design principles in our existing Celery/Postgres architecture; no framework migration is needed to implement them. [Workflow guidance](https://www.anthropic.com/engineering/building-effective-agents), [evaluation guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence).

## 4. The proposed intelligence model

### A. Evidence-backed opportunity assessment

Introduce a small common evidence format shared by matching, documents, answers, and interview preparation. Extend existing structured job fields and profile facts rather than importing a separate knowledge-graph service.

Each job requirement should carry: a stable ID; its quoted text and source location; a posting snapshot/hash; whether it is required, preferred, or ambiguous; alternatives such as “Java or Kotlin”; and an extraction version. Each candidate fact should carry its profile entry, original statement, user approval state, relevant dates, and a version.

An assessment links the two with one of: **supported, transferable, unknown, or explicitly conflicting**. The UI should show the requirement, candidate evidence, and the explanation together. A quote existing in a document is necessary evidence for attribution, but does not by itself establish that the quote entails the model's conclusion; add targeted checks and human review for that distinction.

Example, using fictional facts:

| Posting requirement | Candidate evidence | Assessment / next action |
|---|---|---|
| Operate production APIs | Approved achievement describing an API deployment and its measured traffic | Supported, with a link to the original achievement |
| Kubernetes experience | Docker is listed; Kubernetes is not discussed | Unknown; ask whether relevant experience exists, without claiming equivalence |
| Java or Kotlin | Verified Java project | Satisfies the stated alternative; do not count Kotlin as a separate missing requirement |
| Hybrid work in a named city | User's commute preference is missing | Ask once; do not infer willingness or silently reject |

Choose questions by usefulness: an answer that resolves uncertainty for several strong opportunities should come before a minor wording preference for one low-priority posting. Reuse the existing facts/story/answer stores. Expire context-dependent answers such as availability and avoid reusing employer-specific prose across companies just because question wording matches.

**First implementation:** add a deterministic evidence selector with a token budget; feed the same selected fact IDs into both matching passes; return a structured requirement assessment; validate IDs and source quotations. Start with lexical retrieval over approved facts. Semantic retrieval can come later.

**Acceptance:** fixtures for alternatives, negation, preferred versus required skills, transferable skills, overlapping dates, and unknown qualifications; no invented achievements; no loss of strong matches in a held-out user-labeled set; cost recorded per assessment.

### B. Reliable memory of decisions and outcomes

Add append-only application/decision events alongside the current status projection. Useful fields include event type, application/job ID, occurred/observed timestamps, origin, deduplication key, evidence reference, confidence, and any correction/supersession. The current board can continue using a compact status column.

Capture the selected posting snapshot, profile version, rank/model version, feature values, and document version at decision time. Retain only the necessary snapshot data, deduplicate shared versions, and define retention/export/deletion behavior; this need not become a second copy of the whole database for every click.

Separate milestones: applied, receipt confirmed, assessment requested, interview invited, interview completed, rejected, offered, withdrawn. “Interviewed then rejected” must retain both facts. Store an uploaded-document hash/version when observed; otherwise label the linkage as user-confirmed or inferred.

Extend the read-only mailbox service from outreach replies/bounces to application events. Start with deterministic message/application IDs and a review queue for uncertain associations. A shared recruiting sender or company name alone cannot identify the right application. Extract only the relevant snippet for a classification request; do not send unrelated mailbox contents to a model. Corrections should be reversible and should not erase the original evidence.

Show outcome cohorts with elapsed follow-up time and sample sizes. No response after two days is not a negative training label. Compare like periods/roles and keep cold applications, referrals, and outreach distinct. Even then, descriptive differences do not establish that a particular resume caused the result.

**Acceptance:** duplicate mail ingestion creates one event; out-of-order messages preserve milestones; two jobs at one company are not conflated; an interview remains counted after rejection; an inferred document is never displayed as a confirmed upload.

### C. A daily action queue

The main experience should answer “What should I do with my next 30 minutes?” The user chooses a time budget; the system proposes a short plan with reasons, missing information, and estimated effort.

Example output: two strong applications worth preparing, one approaching assessment deadline, one reply needing attention, and one question that improves several matches. Users can pin employers, adjust priorities, and dismiss with reasons. This should be reachable without chatting; natural-language instructions can modify the same explicit plan.

Initially rank actions with a transparent rule using evidence fit, user preferences, known deadlines, freshness, verified relationship context, readiness, and estimated time. Show components instead of a spurious “87% chance of interview.” Do not infer a warm relationship from an email address found online.

Later, evaluate an outcome-informed ordering separately from the preference model. Reserve a small configurable exploration portion for plausible overlooked jobs; avoid crowding the plan with near-duplicates or one employer. Log which items were visible. A user should be able to understand why an item moved.

**Acceptance:** every proposed action names its reason and evidence; deadlines outrank routine background work; the plan fits the selected time budget; missing information is visible; a rejected suggestion updates preferences without fabricating an employer-outcome label.

### D. Documents that use the evidence well

We already generate, edit, version, critique, and parse documents. Improve the selection problem: choose a compact set of truthful achievements covering the most important requirements, with penalties for redundant bullets and a page-length constraint.

Display proposed changes as a diff: what was selected, what was rephrased, which facts support it, and which requirements remain unsupported. An approved phrase is not a license to transfer its metric to another job or project. Bind changes to a base version and reject or reconcile stale edits.

After fitting and rendering, check whether the important supporting statements survived. Distinguish PDF readability, field/section extraction, and requirement coverage; keyword presence alone does not establish that an employer's ATS will interpret a resume correctly. Keep the original resume available and cap critique/rewrite attempts.

Use the same evidence links for interview preparation: select relevant existing stories, quote the submitted resume, identify follow-up questions, and let the user record what an interview actually tested. Extend the current interview corpus and story bank rather than adding another disconnected question generator.

**Acceptance:** unsupported metrics fail deterministic checks; removed bullets do not count toward final coverage; old versions remain accessible; stale proposals cannot overwrite newer edits; rubric-based quality review uses human spot checks rather than only the writer model grading itself.

### E. Browser assistance with verified completion

Retain the user's browser session and the existing rule that the user submits. Prefer deterministic ATS adapters, field labels, and accessibility information. Add narrowly scoped semantic interpretation only when established mappings fail.

For each field, retain a status such as recognized, filled, verified, or needs input. Verify the page's value after typing and after dynamic rerenders. Persist a checkpoint when navigating between steps. Observe a success message, receipt, or supported application identifier before suggesting that submission succeeded; a click or route change is insufficient.

A cached mapping should include ATS, origin, form structure/version, and relevant field semantics. Cache mappings separately from personal answers, and invalidate when the page or answer context changes. A cached checkbox mapping must not carry a previous person's or employer's answer.

Keep a redacted fixture gallery for supported ATS flows, including repeated fields, consent questions, custom dropdowns, validation errors, and rerenders. Measure field correctness and completion evidence, not just whether a script ran without throwing. Unknown declarations remain for the user; never infer sensitive answers from other profile fields.

**Acceptance:** existing values are preserved; false submission confirmations are absent from the fixture suite; unsupported fields are clearly reported; browser/server interruption resumes without duplicate ingestion; no final submit occurs automatically.

## 5. Running this on the existing VPS

The production Compose file already gives the batch worker a default 0.8 CPU ceiling and the interactive worker 0.6, with separate queues. Those limits are ceilings, not reservations for the website, and worker-triggered database work consumes resources in the database container. Docker CPU shares are relative weights under contention. [Current configuration](https://github.com/thejaynesh/jobapp/blob/2afe7bafee179a8810d5de5a39e4998da9f126fa/docker-compose.prod.yml), [Docker resource controls](https://docs.docker.com/engine/containers/resource_constraints).

Add a small admission controller to the existing scheduler:

| Signal | Proposed response |
|---|---|
| Web latency or database query latency deteriorates | Delay broad discovery, archive maintenance, and bulk rescoring; prioritize user requests |
| Interactive queue becomes old | Stop admitting more background work until it recovers |
| Sustained CPU pressure, host steal, or memory pressure | Enter a visible reduced-work mode; recover gradually after a cooldown |
| A source returns 429 or repeated errors | Apply its cooldown and bounded backoff; retain a scheduled probe |
| A source adds mostly duplicates or irrelevant jobs | Reduce polling frequency while preserving minimum coverage and watchlist freshness |
| A posting/profile has not materially changed | Reuse versioned extraction and assessments rather than paying to repeat them |

Scrapy's AutoThrottle is a useful example of load-sensitive crawling; our implementation should also consider the website and database it shares resources with. This does not require adopting Scrapy. [AutoThrottle](https://docs.scrapy.org/en/latest/topics/autothrottle.html).

Keep the application, database, queues, and cheap feature calculations on the VPS. Continue using remote model providers for costly inference. Keep interactive browser activity in the user's extension; tightly bound the existing server browser fallback. Do not add an always-on local LLM or unrestricted browser-agent pool to this machine.

Measure requests, transferred bytes, unique useful postings, total elapsed time, database time, and CPU use where attribution is practical. Source counts alone cannot tell us which source is economical. Discovery cadence should follow marginal useful yield with a minimum exploration schedule, not permanently shut off sources with little initial data.

Introduce semantic search only as an evaluated option. Begin with a small active/watchlist cohort and cached job vectors; recompute on content/model changes. Do not send the full historical corpus for embedding as the first experiment. If it earns broader use, evaluate storage/search in the existing database before operating a separate vector service. Account for index/build memory and database contention before enabling a large index.

Hostinger documents automatic restrictions under sustained CPU consumption and restoration after usage falls. The earlier dashboard warning establishes that restriction was present then, but this research did not check whether it is still active. Application improvements can reduce demand; they cannot guarantee immediate restoration of host CPU capacity. [Hostinger CPU limits](https://www.hostinger.com/support/6899741-what-is-the-cpu-use-limit-for-vps-at-hostinger/), [CPU steal and restoration](https://www.hostinger.com/support/9615642-understanding-cpu-steal-and-its-impact-on-vps-at-hostinger/).

## 6. Evaluation before rollout

Maintain two different suites: deterministic software regression tests, and a small versioned quality benchmark for the actual AI outputs. Passing thousands of mocked/unit tests does not establish recommendation or writing quality. Resume Matcher's explicit separation of paid generated-output evaluation from deterministic tests is a useful example; an LLM judge still needs independent checks and human calibration.

Start with roughly 50–100 representative job/profile pairs if the user has enough labeling time, plus a fixed challenge set. This is a practical initial engineering sample, not a statistically powered hiring-outcome experiment. Include good fits, clear mismatches, sparse descriptions, alternatives, adjacent skills, seniority ambiguity, duplicates, and malicious instructions embedded in postings.

| Capability | Main measure | Guardrail |
|---|---|---|
| Retrieval/filtering | Recall of worthwhile jobs, including audited rejects | No evaluation limited to the jobs the current filter already accepts |
| Top-of-list ranking | Human-rated usefulness/precision in the top 10; ranking quality over time | Split chronologically and by posting family; report counts and uncertainty |
| Requirement assessment | Supported claims, missed evidence, unknowns handled correctly | Model-supplied fact IDs and quotes must resolve to authorized source material |
| Documents | Human preference, factuality, final rendered coverage, editing time | No invented dates, employers, skills, responsibilities, or metrics |
| Browser | Correct verified fields and accurate receipts | No automatic submit, invented answer, or overwritten user input |
| Outcome tracking | Correct application association and milestone preservation | Ambiguous messages cannot silently become definitive outcomes |
| Operations | Page p95, 5xx rate, queue age, cost per useful shortlisted job | Quality improvements cannot make the portal unusable |
| Product benefit | Worthwhile interviews and user effort, over comparable mature cohorts | No-response censoring and selection effects remain visible |

Compare the current baseline with one changed component at a time: better evidence input, then a structured rubric, then optional hybrid retrieval. Freeze a test set before prompt tuning. Repeat a small sample to expose model variability. Evaluate the deployed/pruned ranker representation, not only its unpruned training object.

Use shadow mode first: produce proposed assessments without hiding jobs or changing documents. Then make the new behavior selectable and keep the previous ranking available. Promotion requires useful improvement on the held-out set at an acceptable latency/cost; tiny differences on tiny samples should be treated as inconclusive.

A suggested initial service target is page p95 below two seconds during a representative batch run and no sustained 504s. This is a proposed target, not a measurement or guarantee on a CPU-restricted host. Establish baseline latency and resource availability before deciding whether to tune the target or the infrastructure.

## 7. Implementation sequence

Effort below is a rough engineering estimate for one developer with the current architecture, including tests and review. It excludes waiting for enough real outcomes and production stabilization; several phases may need revision after baseline measurement.

| Phase | Scope and main code touchpoints | Acceptance / dependency | Estimate |
|---|---|---|---|
| 0. Measurement and immediate corrections | `tracker.py`, `match_report.py`, `for_you.py`, `schedule.py`, operations metrics | Define milestone counting and decision snapshots; establish load and quality baselines | 2–4 days |
| 1. Shared evidence assessment | New small evidence/requirements services; `matcher.py`, `job_details.py`, existing profile facts | Both matching passes use bounded real achievements; supported/unknown/conflicting cases pass fixtures and held-out comparison | 4–7 days |
| 2. Durable outcome history | Application/decision event migration; `tracker.py`, `mailbox.py`, agent/application routes | Idempotent events, reversible corrections, preserved interview milestones, confirmed versus inferred submitted materials | 4–7 days |
| 3. Daily plan and adaptive work | `for_you.py`, `source_yield.py`, `schedule.py`, dashboard | Explainable time-budgeted plan; interactive responsiveness under batch load; configurable exploration | 3–6 days |
| 4. Better document and browser execution | `doc_generator.py`, `document_edit.py`, `document_content.py`, `extension/autofill.js`, overlay, browser fixtures | Requirement coverage survives rendering; change proposals are version-aware; form values and receipts are verified | 5–9 days |
| 5. Optional learned retrieval/outcomes | `similarity.py`, `match_eval.py`, `for_you.py`; cached model artifacts | Better held-out ranking at a measured cost, with enough mature labels; otherwise keep the simpler baseline | 3–6 days initially, plus observation time |

Ship these as small releases. The first useful release should address evidence omission, correct outcome semantics, and establish the baseline—not wait for the entire program. Follow repository conventions: behavior settings belong in `TUNABLES` and Settings UI, scheduled work in `SCHEDULE`, infrastructure settings in environment configuration, and meaningful checks in CI.

## 8. What to defer

- A multi-agent rewrite or a new orchestration framework. Existing queues and services can support the proposed workflow.
- More indiscriminate crawling. First establish which sources contribute useful opportunities per unit of resource.
- Training a bespoke LLM or running a large local model on the current VPS. Improve the evidence available to existing providers first.
- A separate vector database or graph database before a measured need. A few relational records and explicit links are sufficient for the first evidence model.
- Unattended mass applications. Preserve review and invest in better selection, accurate materials, and verified completion.
- Rewriting resumes until an AI score rises. A higher internal score may only indicate successful optimization of the scorer.
- A chat interface that cannot perform or explain concrete actions. The daily plan and application workspace should remain usable directly.
- Claims that a universal ATS score predicts employer behavior, or that a handful of responses proves one resume strategy is better.

The intended result is a system that can justify each recommendation, learn from accurately recorded experience, and finish useful work within both the user's time and the server's capacity.
