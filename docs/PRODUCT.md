# Job Application Automator — Product Description

**Status:** Living document
**Companion:** `docs/superpowers/specs/2026-08-10-jobapp-v2-design.md` (technical design)

This document describes *what the product is and why*. The spec describes *how it
is built*.

---

## 1. What this is

A self-hosted job search system that finds roles worth your time, gets you in
front of them early, connects you to someone on the inside, applies with tailored
documents, tracks every outcome without bookkeeping, and prepares you for the
interviews that result.

It runs as three cooperating tiers: a VPS that thinks, a laptop that acts with
your identity, and a mailbox that observes what happened.

It is not a job board and not a mass-apply tool. It is a system for spending a
fixed amount of weekly effort where that effort converts.

---

## 2. Who it is for

**Primary user (today):** one person conducting a serious job search, with limited
weekly hours and a strong preference for spending them where they convert.

**Generalization (later):** anyone whose job search is systematic rather than
casual. The architecture makes no assumptions about the user's circumstances
beyond a profile and a mailbox.

The architecture is deliberately kept multi-tenant-shaped even while there is one
tenant, because pushing execution to each user's own browser is the only design
that scales without a proxy bill (see spec, "Strategic note").

---

## 3. The problem

Job searching presents as a volume problem and is actually a leverage problem.

The default behaviour — apply to as many things as possible — fails in five
specific ways, and each has a fix:

| Failure | What it costs | Fix |
|---|---|---|
| **Applying late** | Applicant #400 instead of #25 | Speed lane on fresh postings (§6.3) |
| **Applying cold** | ~2–5% response instead of several times that | Referral and alumni paths (§6.5) |
| **Applying to noise** | Ghost jobs, duplicate postings, roles that were never real | Quality gating (§6.2) |
| **Losing track** | Stale tracker, missed deadlines, duplicate applications, follow-ups sent to people who already replied | Automatic tracking from email and submit detection (§6.6) |
| **Never learning** | The same mistakes for months; no idea which sources or documents work | Outcome-driven calibration (§6.8) |

The through-line: **`early × warm × well-matched` beats volume.** Every feature in
this product serves one of those three, or serves the loop that measures them.

---

## 4. Product principles

These are binding. Features that violate them do not ship.

**1. Everything automatic has a manual trigger.**
Every scheduled or event-fired behaviour also has a button. Automation decides
*when* something usually happens; the user decides when it happens *now*. See §7
for the complete trigger matrix. This is already the pattern in v1 — `/runs` has a
manual fetch trigger alongside the 5-hour beat — and it becomes a rule.

**2. Nothing irreversible happens without a human click.**
Documents generate automatically. Messages draft automatically. Applications
pre-fill automatically. But **nothing is submitted or sent to a human being
without explicit approval.** `outreach_sender` already enforces this on email;
it extends to application submission and LinkedIn messages.

**3. Every automatic decision shows its evidence and can be undone.**
"Marked rejected" links to the email that said so. "Filtered out — citizens only"
quotes the sentence that triggered it. Every filter has an override. A pattern
match *will* have false positives, and the user needs an escape hatch that takes
one click.

**4. Never fabricate. Always cite.**
Interview questions, company facts, match reasoning — every claim traces to a
source with a link. A hallucinated interview question is worse than no dossier
because it gets acted on. When there is no data, the product says so and degrades
gracefully rather than inventing.

**4a. Immigration status is not an input.**
It is not stored on the profile, never written into a resume or cover letter, and
never passed to an LLM for matching, scoring, or ranking. The single exception is
a deterministic text filter that removes postings *explicitly stating* the role is
restricted to US citizens — reading the posting's own words, inferring nothing
about the user (§6.2).

**5. Harvest before you crawl.**
The default mode for anything behind a login is to capture what the user is
already browsing. Synthetic searches are opt-in, capped, and human-paced. The
LinkedIn account is an asset worth more than any single fetch cycle.

**6. Degrade, never fail.**
Laptop closed, API key missing, source blocked, no interview reports for a small
company — each of these reduces what the system can do without stopping it. This
is already the v1 pattern (`contact_finder` returns nothing rather than raising)
and it holds everywhere.

**7. Local-first and self-hosted.**
No third-party SaaS holds the user's profile, application history, or mailbox
credentials. Everything lives on the user's own VPS and their own machine.

---

## 5. The system in one picture

```
        ┌──────────────────── VPS (thinks) ────────────────────┐
        │  Postgres · Celery · LLM · matching · documents      │
        │  API-tier sources · interview corpus · analytics     │
        └───────┬─────────────────────────────────┬────────────┘
                │ BrowserTask queue               │ IMAP (beat)
                ▼                                 ▼
    ┌─── LAPTOP (acts as you) ───┐    ┌─── MAILBOX (observes) ───┐
    │ Extension: your sessions,  │    │ Confirmations, rejections│
    │ autofill, overlay, submit  │    │ interview invites, OA    │
    │ detection, passive harvest │    │ deadlines, replies,      │
    │                            │    │ bounces, recruiter mail  │
    │ Local agent: residential   │    │                          │
    │ IP, link resolution, long  │    │ → the outcome labels     │
    │ crawls, selector learning  │    │   that make §6.8 work    │
    └────────────────────────────┘    └──────────────────────────┘
```

---

## 6. What it does

### 6.1 Knows who you are

- **Profile** — experience, projects, skills, education, narrative answers *(v1)*
- **One-click import from your own LinkedIn** — replaces manual profile entry
- **Market-driven skill gaps** — "61% of your matched jobs want Kubernetes; you
  don't list it"

Immigration status is not a profile field. See §4a.

### 6.2 Finds jobs worth your time

**Discovery.** 25+ API sources *(v1)*, plus everything a datacenter IP can't
reach: LinkedIn, Handshake, Indeed, Dice, Wellfound, ZipRecruiter — through your
own browser, with your own session. Plus passive harvest of every job page you
browse, and in-browser resolution of aggregator redirect links, which feeds ATS
discovery and compounds into more API-tier sources over time.

**Quality gating.** Ghost-job detection (reposted, stale, no ATS ID, vague),
applicant counts, cross-board duplicate guard.

**Citizens-only filter.** One narrow, deterministic text filter removes postings
that *explicitly state* the role is restricted to US citizens — "must be a US
citizen", "citizens only", an active clearance requirement, ITAR / US Person
language. It reads the posting's own words and infers nothing about the user. The
triggering sentence is quoted back, and every match can be overridden in one click.

Deliberately out of scope: sponsorship inference, employer immigration datasets,
and any status-aware weighting. Postings that say they will not sponsor stay in
the list and are ranked on merit like everything else.

### 6.3 Tells you what to do first

- **Speed lane** — high match, posted within hours → notify now
- **Ranking** — LLM match score *(v1)* weighted by warm connections, and eventually
  a reranker learned from your own outcomes
- **Interview-loop cost** — round count and reported process length as a *ranking*
  signal, so you know a four-week loop before you spend an application on it

### 6.4 Applies with you, not for you

- **Tailored resume and cover letter** per role *(v1)*
- **Parseability verification** — the generated PDF is read back to confirm your
  skills survive text extraction. LaTeX PDFs can extract as mush and get you
  silently auto-rejected.
- **Capture what the ATS actually parsed** — Workday and Greenhouse prefill fields
  from your upload; reading those back is ground truth on parseability per ATS
- **Autofill** across every major ATS, including attaching the tailored PDF
- **LLM-drafted answers** to custom questions, inline, editable
- **Shadow queue** — the next five applications pre-open and pre-fill while you
  review the current one
- **JD archival at submit** — postings get pulled; you'll want the text at
  interview time
- **Workday credential vault** — account-per-tenant is the worst part of applying

**Never auto-submits.** Fills, highlights what it filled, waits for you.

### 6.5 Gets you a warm introduction

- **Contact discovery** — Hunter, GitHub orgs, team pages, JD text *(v1)*
- **Alumni finder** — LinkedIn's alumni tool needs a session. Alumni are the
  highest-response cold-outreach segment available.
- **Referral finder** — your 1st-degree connections joined against target
  companies. Needs your connection graph; no API will ever supply it.
- **Warm paths** — "you know Sam → Sam knows the hiring manager"
- **Drafted messages** with follow-up sequences *(v1)*, sent through Gmail for
  deliverability and threading, with a sending warmup ramp for a new mailbox
- **Priority order is explicit:** referral > alumni > recruiter > hiring manager >
  cold

### 6.6 Tracks everything without you

- **Submit detection** — the extension sees the confirmation page
- **Email-driven status** — ATS confirmations, rejections, interview invites parsed
  from your mailbox and applied to the tracker automatically, with the source email
  linked and one-click undo
- **Reply detection** — via stored `Message-ID` and inbound `In-Reply-To` headers.
  Deterministic, not heuristic. This closes a real v1 gap where auto-drafted
  follow-ups could nag someone who had already replied.
- **Bounce handling** — invalidates a guessed address *and* corrects the domain
  pattern for future guesses
- **OA deadlines** — "complete this by Friday" surfaced before it expires
- **Ghosting clock** — "34 days, no email of any kind" is a stronger signal than
  "no status change"
- **Recruiter inbound** — cold recruiter mail becomes a tracked opportunity with a
  drafted reply

### 6.7 Prepares you for the interview

- **Interview intelligence corpus** — published interview experiences aggregated
  per company and role from GeeksforGeeks, Reddit, GitHub collections, LeetCode
  Discuss, Glassdoor, and **LeetCode Premium company tags** (problems tagged by
  company with frequency and recency — the highest-quality source available)
- **Dossier fires automatically on the interview invite** — by the time you read
  the email, it exists
- **Output is a practice plan, not a summary** — specific problems ordered by
  reported frequency, clickable, with the gaps in your profile called out
- **Interviewer research** — their LinkedIn background, pulled from the invite
- **Mock interviews** using the actual reported questions in the reported format
- **Question bank** — what you were really asked, which over time validates which
  sources are accurate for which companies
- **Thank-you notes** drafted within the hour
- **Comp comparison** from salary ranges harvested from postings — a dataset
  filtered to your exact segment

Where a company has no published reports (small startups), the product says so and
falls back to JD analysis, the interviewer's background, and the company's
engineering blog. It does not invent a loop.

### 6.8 Learns

Outcome labels from §6.6 are the ground truth v1 never had.

- **Score calibration** — does an 85 actually convert better than a 72?
- **Rubric evolution** — extend model comparison from "which model emits clean
  JSON" to "which scoring rubric predicts real callbacks"
- **Dwell-time signals** — 90 seconds on a job page is interest; 3 seconds is not.
  A free label on every job, no application required.
- **Resume A/B** — response rate per variant
- **Source ROI** — which sources produce *interviews*, not just jobs
- **Personal reranker** — your revealed preferences, above the LLM score

---

## 7. Trigger matrix

Principle 1 in concrete form. Every row has both columns filled.

| Behaviour | Automatic trigger | Manual trigger |
|---|---|---|
| Fetch jobs | 5-hour beat | "Fetch now" — `/runs` *(v1)* |
| Fetch one source | — | "Fetch just this source" |
| Match / score jobs | After fetch | "Rescore" per job or in bulk |
| Citizens-only filter | During match | "Re-check" · **"Apply anyway"** override per job |
| Resolve aggregator link | During fetch | "Resolve this link now" |
| Generate documents | On match above threshold | "Generate" / "Regenerate with feedback" *(v1)* |
| Verify resume parseability | After generation | "Check parseability" |
| Discover contacts | After doc generation | "Find contacts" *(v1)* |
| Draft outreach message | After contact discovery | "Draft" / "Regenerate" *(v1)* |
| Draft follow-up | Beat, on the 4/7/10 schedule | "Draft follow-up now" |
| Send email | **Never automatic** | "Send" *(v1)* |
| Sync mailbox | Beat (IMAP poll) | "Sync mailbox now" |
| Application status from email | On matching mail | Manual status set · **undo** with evidence shown |
| Reply / bounce detection | On matching mail | "Mark replied" / "Mark bounced" *(v1)* |
| Submit detection | On ATS confirmation page | "Mark as applied" |
| Autofill application | On recognised ATS page | "Fill this form" in the extension |
| Submit application | **Never automatic** | You click submit, on the site |
| Build interview dossier | On interview invite email | **"Build dossier"** on any application |
| Refresh interview corpus | 30-day TTL per company | "Refresh corpus for this company" |
| Run mock interview | — | "Start mock interview" |
| Recalibrate scoring | Monthly beat | "Recalibrate now" |
| Dispatch task to laptop | When a browser-tier task is queued | "Run on laptop now" |

---

## 8. Surfaces

### Web app (VPS)

| Surface | Purpose | Status |
|---|---|---|
| **`/today`** | The daily loop landing page: speed-lane alerts, apply queue, follow-ups due, upcoming interviews, things needing attention | **New — the missing "what do I do right now" page** |
| `/jobs` | Matched and filtered jobs, with filter reasons | v1 |
| `/apps`, `/apps/{id}` | Application tracker, documents, contacts, timeline | v1 |
| `/outreach` | Contacts, drafts, threads, follow-up queue | v1 |
| `/profile` | Profile editor, narrative, prompt preview | v1 |
| `/runs` | Fetch history, source diagnostics, model comparison | v1 |
| `/settings` | Thresholds, schedule, sources, keys | v1 |
| **`/interviews`** | Dossiers, prep plans, question bank, mock interview | New |
| **`/insights`** | Funnel metrics, calibration, source ROI, resume A/B | New |
| **Agent status** | Laptop last-seen, queued tasks, engine health — extends `/runs` | New |

### Extension

| Surface | Purpose |
|---|---|
| **Overlay panel** | On any job page anywhere: match score, matched/missing skills, "already applied ✓ Mar 3", save / generate docs / find contacts |
| **Autofill widget** | On recognised ATS forms: fill, highlight what was filled, draft answers to custom questions |
| **Popup** | Agent status, quick capture of the current page, recent activity |
| **Options page** | VPS URL, bearer token, per-site enable/disable, harvest mode |

---

## 9. Data model additions

Beyond the v1 schema (`jobs`, `applications`, `application_documents`, `contacts`,
`outreach_messages`, `profiles`, `company_boards`, `fetch_runs`):

| Table / column | Purpose |
|---|---|
| `browser_tasks` | The laptop work queue — kind, payload, status, result, TTL |
| `outreach_messages.message_id` | Stored RFC Message-ID → deterministic reply matching |
| `jobs.restricted_flag` | Citizens-only verdict, with the triggering sentence in `filter_detail` |
| `interview_reports` | Company-scoped: source, URL, posted date, rounds, questions, outcome, difficulty, quality score |
| `interview_dossiers` | Cached per company × role, ~30-day TTL |
| `application_events` | Timeline of everything that happened, with evidence links — powers undo and the audit trail |
| `job_signals` | Dwell time, opens, and other implicit feedback |

---

## 10. Non-goals

- **Not a mass-apply tool.** No spray. Volume actively works against the funnel
  math this product is built on.
- **Never auto-submits an application or auto-sends a message to a person.**
- **Not a general-purpose scraper.** Every source exists to serve the funnel.
- **Not multi-user yet** — architected to allow it, not built for it.
- **Not a judgement replacement.** It ranks and prepares; you decide where to work.
- **Not a Chrome Web Store extension.** It will not survive review, and it does not
  need to — load unpacked.

---

## 11. How you know it's working

Measured on `/insights`, reviewed weekly.

| Metric | Target direction |
|---|---|
| Applications to citizens-only postings | → 0 |
| Median hours from posting to application | ↓ (target: same-day for speed-lane) |
| Share of applications with a warm connection | ↑ |
| Response rate, interview rate | ↑ |
| Status accuracy without manual updates | → 100% |
| Score calibration (score vs. actual callback) | correlation ↑ |
| Follow-ups sent to people who already replied | → 0 |
| Time spent per application | ↓ |
| Sources producing interviews (not just jobs) | identified and concentrated on |

**At 90 days**, success looks like: zero wasted applications, most submitted
within hours of posting, a meaningful share carrying a referral, a tracker that is
accurate without you touching it, a match score that predicts callbacks, and a
ranked view of which employers can carry you past month 12 and month 30.

---

## 12. Build order

Full detail in the spec. Summary: **the first three phases need no extension at
all** — they are server-side, small, and make the extension work more valuable
when it lands.

| Phase | Items |
|---|---|
| **1 — Filters** | Citizens-only filter · drop work authorization from the matcher rubric |
| **2 — Foundation** | API authentication (prerequisite for everything holding credentials) |
| **3 — Email loop** | `message_id` column · IMAP reply/bounce detection · Gmail SMTP + warmup ramp |
| **4 — Agent** | `BrowserTask` protocol · extension skeleton · link resolution · passive harvest |
| **5 — On-page** | Overlay · autofill · submit detection |
| **6 — Authenticated search** | LinkedIn · Handshake · Indeed · Dice (highest account risk — last) |
| **7 — Warm paths** | Alumni finder · referral finder |
| **8 — Interview** | Corpus (free sources → LeetCode Premium tags, Glassdoor) · dossier · mock · question bank |
| **9 — Learning** | Calibration · reranker · source ROI |

Phase 8's free sources have no dependency on any earlier phase and can be pulled
forward at any point interviews start arriving.
