# Job Application Automator v2 — Design Spec
**Date:** 2026-08-10
**Status:** Draft — for discussion

---

## Why v2

v1 works: 25+ sources, LLM matching, tailored LaTeX documents, contact discovery,
drafted outreach. But it has three structural limits that no amount of scraper
tuning will fix.

**1. The VPS has the wrong identity.** A datacenter IP with no logged-in sessions
gets challenged by Wellfound, Indeed, Dice, Glassdoor and ZipRecruiter, and using
`LINKEDIN_SESSION_COOKIE` from that ASN is the exact pattern LinkedIn restricts
accounts for (`config.py:41` already says so). The blocked sources are not a
scraping problem; they are an egress problem.

**2. Nothing observes outcomes.** `matcher.py` assigns `llm_score` and nothing
ever learns whether it was right. `model_compare.py` compares models against each
other, never against reality. `reply_rate` (`outreach.py:1136`) is only as
accurate as manual button-clicking. The system is open-loop.

**3. The pipeline stops at `applied`.** `ApplicationStatus.interviewing` exists
and leads nowhere. `MESSAGE_KINDS` contains `thank_you` and `referral_request` —
modeled, never used. The schema is ahead of the features.

v2 adds two execution tiers and one feedback tier to fix all three.

---

## Architecture

Three tiers, one brain.

```
┌─ VPS (brain) — always on ──────────────────────────────────────┐
│  Postgres · Celery · LLM matching · doc generation · outreach   │
│  API-tier sources (Greenhouse, Lever, Ashby, Adzuna, ...)       │
│  Decides what work exists; never blocks on the other tiers.     │
└───────────────┬──────────────────────────┬─────────────────────┘
                │ BrowserTask queue        │ IMAP poll (Celery beat)
                │ /api/agent/*             │
┌───────────────▼──────────────┐  ┌────────▼───────────────────────┐
│ LAPTOP (hands)               │  │ MAILBOX (ears)                 │
│                              │  │                                │
│ Engine A: MV3 extension      │  │ Application confirmations      │
│   your real sessions —       │  │ Rejections / interview invites │
│   LinkedIn, Handshake,       │  │ Replies (Message-ID matching)  │
│   autofill, submit detection │  │ Bounces, OA deadlines          │
│                              │  │ Recruiter inbound              │
│ Engine B: local Playwright   │  │                                │
│   residential IP — Indeed,   │  │ This is the ground truth that  │
│   Dice, link resolution      │  │ makes tier 4 (learning) work.  │
│                              │  └────────────────────────────────┘
│ Engine C: LLM-driven browser │
│   unknown sites; learns and  │
│   caches selectors as new    │
│   adapters                   │
└──────────────────────────────┘
```

### The contract

One `BrowserTask` model (`kind`, `payload`, `status`, `result`, `expires_at`) and
an authenticated `/api/agent/*` router. The laptop long-polls for work, executes,
posts results. The VPS does not care which engine ran the task.

**The fetch cycle must never block on the laptop.** Enqueue and move on. Results
arrive whenever the laptop is awake, land in the existing normalize → `dedupe_hash`
→ save path, and `tasks/match.py` picks up `status=new` on its next beat. The
existing architecture already tolerates this because matching is decoupled from
fetching.

### Why the extension, specifically

Not "a browser" — *your* browser. Three things come free:

- **Residential IP** — the bot walls stop firing
- **Real logged-in sessions** — no cookie copy-paste, no ASN mismatch
- **Real fingerprint** — TLS/JA3, canvas, the whole stack

### The technique that matters most

Do not scrape the DOM. **Intercept the page's own JSON.** A `world: "MAIN"`
content script patches `window.fetch` and `XMLHttpRequest`; when LinkedIn's page
calls `/voyager/api/voyagerJobsDashJobCards`, we read the complete structured
response.

- JSON schemas change far slower than CSS classes (the `_CARD_TITLE_RE` regexes in
  `sources/linkedin.py` rot every redesign; the API fields do not)
- Zero additional requests — nothing to rate-limit, nothing to detect
- Fields the DOM never renders: full description, applicant count, salary

For LinkedIn this removes a documented ceiling. The guest API returns 10 cards per
page and forces a separate description fetch per job, making
`LINKEDIN_MAX_DETAIL_FETCHES: 200` the source's real yield cap. Voyager returns
descriptions inline. The cap disappears.

---

## The funnel

Features are organized by where they sit in the path from "job exists somewhere"
to "offer signed."

### Stage 0 — Profile & eligibility

| Feature | Tier | Notes |
|---|---|---|
| Import profile from your own LinkedIn | Extension | One click; kills the worst part of onboarding |
| **Work-authorization model** | VPS | F-1 OPT → STEM OPT, with dates. See below. |
| Precomputed ATS answers | VPS | "Authorized to work? yes / Require sponsorship? eventually" — fixed for you, never think about them again |
| Market-driven skill gaps | VPS | "61% of your 340 matched jobs want Kubernetes; you don't list it" |
| Resume states work auth explicitly | VPS | *"Authorized through [date] under F-1 STEM OPT; no sponsorship required at this time"* — preempts the most common silent rejection |

### Stage 1 — Discovery

| Feature | Tier | Notes |
|---|---|---|
| API-tier sources | VPS | Existing — 25 adapters, unchanged |
| **Browser-tier sources** | Extension | LinkedIn (Voyager), Handshake, Indeed, Dice, Wellfound, ZipRecruiter — with your session |
| **Passive harvest** | Extension | Everything you browse is captured. Zero detection risk: you *are* the traffic. Default mode. |
| **Link resolution in-browser** | Engine B | Adzuna/Jooble/Careerjet interstitials that `link_resolver`'s httpx client can't follow |
| Selector-learning fallback | Engine C | Unknown career site → LLM figures out the structure once → cached as a generated adapter |
| Cap-exempt employer seeding | VPS | Universities, affiliated nonprofits, research orgs — see Stage 2 |

**Link resolution deserves priority out of proportion to its size.** Every
resolved interstitial feeds ATS auto-discovery: more Greenhouse/Lever/Ashby slugs
discovered → more jobs arriving through clean APIs that never need a browser
again. The browser tier is not just a scraper, it is a *bootstrapper for the API
tier*, and that flywheel compounds while everything else is a per-fetch cost.

### Stage 2 — Qualification (work authorization)

This is a two-stage filter on two different clocks. A binary "does this company
sponsor" test would be wrong — it would discard three years of perfectly viable
jobs.

**Gate 1 — can I legally start here? (hard reject, today)**

Detected by pattern match over `Job.description`, writing `filter_reason` /
`filter_detail` (existing columns):

- **`"without sponsorship now or in the future"`** — the important one, and it is
  everywhere. Fine today, excluded anyway. Note that plain "must be authorized to
  work in the US" is *acceptable*; a naive filter drops both.
- **US Person / ITAR / EAR / export control / security clearance** — requires
  citizenship or permanent residency. A large, invisible share of US engineering
  roles: defense, aerospace, gov contractors, some hardware and semiconductor.
- **Third-party placement / consultancy** — STEM OPT requires a bona fide
  employer-employee relationship and an employer-signed I-983 training plan.
  Bench-and-place staffing shops are a bad fit and often refuse the paperwork.

**Gate 2 — will I still be here in year 3? (ranking signal, not a reject)**

Two cliffs, all data public and free:

| Cliff | When | Data source | Signal |
|---|---|---|---|
| **E-Verify** | month ~12 | USCIS E-Verify employer list (downloadable) | STEM OPT extension *requires* employer E-Verify enrollment. Not enrolled → the job caps at 12 months. Startups frequently aren't. |
| **H-1B** | month ~30 | USCIS H-1B Employer Data Hub | **Initial vs. continuing approvals.** A company with 400 continuing and 2 initial is transferring existing holders, not sponsoring newcomers. LCA data alone hides this distinction, and it's the one that predicts the outcome. |
| Long-term | year 6+ | DOL PERM disclosure | Green-card sponsorship history — commitment past the H-1B ceiling |

**Cap-exempt employers get an explicit scoring bonus.** Universities,
university-affiliated nonprofits, and nonprofit/government research organizations
are exempt from the H-1B lottery entirely — no 25% coin flip, file any time of
year. For someone on a hard clock this is worth a large boost, and Boston is
unusually dense with them. Almost nobody filters for this.

Employer-name matching across these datasets reuses `company_domain.company_key`,
which already normalizes "Acme, Inc." and "Acme Inc" into one company.

**Timeline-aware, not static.** Store the OPT start date and the system reasons
about the actual clock:

> *"STEM OPT window opens in 8 months. Anduril: ITAR — hard reject. Ramp:
> E-Verify ✓, 12 initial H-1B approvals FY25 ✓. Northeastern Research Computing:
> cap-exempt, no lottery."*

The weighting shifts as the clock runs down — E-Verify matters enormously at month
6 and is settled by month 20.

Feed the structured status into the matcher. `matcher.py:286` currently allocates
0–15 points to "work-authorization compatibility" while telling the model nothing
about the actual status; those points become meaningful once it knows.

*(Timing-sensitive immigration details should be confirmed with a DSO. Rules shift
and individual circumstances govern.)*

### Stage 2b — Qualification (job quality)

| Feature | Signal |
|---|---|
| Ghost-job detection | Reposted 3+ times, 45+ days old, no ATS ID, vague JD |
| Applicant count | LinkedIn renders it; deprioritize the 800-applicant postings |
| "Actively reviewing applicants" | LinkedIn renders this too |
| Cross-board duplicate guard | Same role on Greenhouse and LinkedIn → don't submit twice. `dedupe_hash` dedupes *jobs*, not *applications to the same company+role*. |

### Stage 3 — Ranking

| Signal | Source |
|---|---|
| LLM match score | Existing |
| Work-auth timeline fit | Stage 2 |
| **Referral / warm path** | Extension — largest single multiplier available |
| **Alumni at company** | Extension — LinkedIn alumni tool |
| Speed lane | `posted_at` — high match + under 2h old → notify immediately |
| Learned reranker | Stage 7 outcomes + dwell time |

### Stage 4 — Applying

| Feature | Tier | Notes |
|---|---|---|
| Tailored resume / cover letter | VPS | Existing |
| **Resume parseability check** | VPS | Extract text back out of the generated PDF; assert claimed skills survive. LaTeX PDFs can extract as mush — ligatures fused, columns interleaved. Silent auto-rejection you'd never find out about. |
| **Capture what the ATS actually parsed** | Extension | Workday/Greenhouse/iCIMS prefill fields from your uploaded PDF. Reading those back is *literal ground truth on parseability, per ATS*. Only possible from inside the browser. |
| Autofill | Extension | Detect ATS from URL → pull profile → fill. `DataTransfer` + `input.files` attaches the tailored PDF programmatically. |
| LLM answers for custom questions | Extension + VPS | "Why do you want to work here?" → JD + profile → drafted inline |
| Shadow queue | Extension | Pre-open and pre-fill the next 5 jobs in background tabs while you review the current one |
| JD archival at submit | Extension | Postings get pulled; three weeks later you interview with no idea what you applied to |
| Workday credential vault | Extension | Account-per-tenant is the most demoralizing part of applying |
| Resume A/B by variant | VPS | `ApplicationDocument` already has `version` and `is_current` |

**Never auto-submit.** Fill, highlight what was filled, let the human review. Auto-submit
is how 200 companies receive garbage.

### Stage 5 — Outreach

| Feature | Tier | Notes |
|---|---|---|
| Contact discovery | VPS | Existing — Hunter, GitHub org, team pages, JD text |
| LinkedIn people search | Extension | Replaces `contact_finder._scrape_people`, which drives Playwright with your cookie from a datacenter IP |
| **Alumni finder** | Extension | LinkedIn's alumni tool requires a session. Alumni are the highest-response cold-outreach segment that exists. |
| **Referral finder** | Extension | Join your 1st-degree connections against `Job.company`. Needs your connection graph — no API will ever provide it. |
| Warm paths (2nd degree) | Extension | "You know Sam → Sam knows the hiring manager" |
| Message drafting | VPS | Existing |
| Prefilled LinkedIn compose | Extension | Extension opens the thread and fills the box; **you** press send |
| Send via Gmail SMTP | VPS | Config change — see below |
| Follow-up sequences | VPS | Existing (`4,7,10` days) — but see the reply-detection gap |

### Stage 6 — Tracking (closing the loop)

**The reply-detection gap is a live bug.** Everything is in place —
`MESSAGE_STATUSES` has `replied`, `CLOSED_MESSAGE_STATUSES` stops follow-ups on
it, `outreach.py:1007` guards drafting with
`if any(m.status == "replied" ...)`, and `reply_rate` is computed from it. But
`mark_replied` is only reachable through `set_message_status` — a button you
click. Nothing observes the mailbox.

`OUTREACH_AUTO_DRAFT_FOLLOWUPS` defaults to **true**. The guard against nagging
someone who already answered — which `outreach.py:945` calls "the worst outcome
the feature can produce" — depends entirely on remembering to click.

**The fix is half-built.** `outreach_sender.py:101` does
`mail["Message-ID"] = make_msgid()` — generates the ID, puts it on the wire, and
throws it away. There is no `message_id` column on `OutreachMessage`. Store it,
and reply detection becomes deterministic via inbound `In-Reply-To` / `References`
headers. No text heuristics, no false positives.

| Feature | Tier | Notes |
|---|---|---|
| Submit detection | Extension | Confirmation-page heuristics → `status=applied`, `applied_at` |
| Application confirmations | IMAP | Every ATS sends one, from stable senders |
| Rejection / interview-invite parsing | IMAP | Auto-advance `ApplicationStatus` |
| **Reply detection** | IMAP | Message-ID matching — fixes the above |
| Bounce handling | IMAP | Invalidates a guessed address *and* the domain pattern → feeds `company_domain` learning. `email_status=invalid` finally set from evidence. |
| OA deadlines | IMAP | "Complete this HackerRank by Friday" — missed deadlines lose the whole pipeline |
| Interview scheduling | IMAP | Calendly/Greenhouse links → calendar → triggers the Stage 7 dossier |
| Recruiter inbound | IMAP | Higher conversion than anything outbound, currently 100% outside the system |
| Ghosting clock | IMAP | "34 days, no email of any kind" is stronger than "no status change" |

**Read mail over IMAP with an app password, not via the extension and not via the
Gmail API.**

- *Not the extension*: a content script on `mail.google.com` only works while
  Chrome is open on that tab. Tracking that pauses when the laptop closes is not
  tracking. Email is the one capability that should not live in the browser tier.
- *Not the Gmail API, for now*: `gmail.readonly` is a **restricted scope**.
  Publishing needs a CASA security assessment; staying in "Testing" mode avoids
  that but expires refresh tokens **every 7 days**. Revisit only if this ships to
  other people.
- *IMAP*: app password, never expires, no OAuth dance, runs from the VPS on a beat.
  `.env.example:63` already documents the app-password pattern for SMTP. IMAP is
  the symmetric other half.

**Send through `smtp.gmail.com`.** One config line, three benefits: real sender
reputation (cold mail from an unwarmed domain lands in spam and you conclude
outreach doesn't work), sent mail lands in your Sent folder, and replies thread
correctly — which is what makes Message-ID matching work.

**Warm up the new mailbox.** A brand-new Gmail has zero sending reputation.
`OUTREACH_MAX_SENDS_PER_DAY: 20` is too aggressive on day one; start around 5/day
and ramp over several weeks. A flat cap invites hitting it immediately — this
should be a ramp.

### Stage 7 — Interview and beyond

Currently nonexistent. `ApplicationStatus.interviewing` leads nowhere.

| Feature | Notes |
|---|---|
| Interview dossier | Likely questions from the JD, your STAR stories from the profile narrative mapped to their stated requirements, questions to ask, red flags. Every input already exists. |
| Interviewer research | The invite names them → extension pulls their LinkedIn → "4 years there, ex-Stripe, posts about Rust" |
| Thank-you notes | `thank_you` is already in `MESSAGE_KINDS` |
| Question bank | Capture questions actually asked; prep against real history instead of generic lists |
| Offer / comp comparison | Against salary data harvested from postings (CA/NY/CO/WA mandate ranges — this becomes a comp dataset filtered to *your* exact segment, more relevant than levels.fyi) |

### Stage 8 — Learning (what makes it compound)

The outcome labels from Stage 6 are the ground truth v1 never had.

| Feature | Notes |
|---|---|
| **Score calibration** | "85+ scored jobs got 12% response; 70–75 got 11%. The score isn't discriminating." Brutal and currently undiscoverable. |
| Prompt / rubric evolution | Extend `model_compare` from "which model emits cleaner JSON" to "which rubric correlates with real callbacks." The `matcher.py:286` weights stop being guesses. |
| Dwell-time signal | Read for 90s → interested. Closed in 3s → not. A free label on every job, no application required. |
| Resume A/B | Response rate per variant |
| Source ROI | Which sources produce *interviews*, not just jobs. Retire the rest. |
| Personal reranker | Outcomes + dwell → reranks above the LLM score |

---

## How to actually use this to get a job

The tooling is not the strategy. The strategy is **shifting effort from volume to
leverage**, and the tooling exists to make that shift possible.

### The math that drives everything

- Cold application → roughly 2–5% response
- Referred application → several times better, consistently the largest single
  multiplier in the whole funnel
- Alumni outreach → far higher response than cold outreach to a stranger
- Being in the first ~25 applicants → materially better than applicant #400

So 100 cold applications and 20 well-chosen warm ones land in the same place — but
the warm path costs a fifth of the effort and produces conversations rather than
form submissions.

**Therefore the system optimizes for `eligible × early × warm`, not raw volume.**
Every feature above serves one of those three:

- **Eligible** — Stage 2. Not wasting a single application on an ITAR role, a
  "no sponsorship ever" posting, or a company that has never filed an initial
  H-1B.
- **Early** — Stages 1 and 3. Speed lane, fresh postings, applicant counts.
- **Warm** — Stage 5. Alumni, referrals, 2nd-degree paths, and outreach that
  actually gets delivered and actually gets followed up.

### The daily loop

**Morning, ~10 minutes.** Check speed-lane notifications — high match, posted in
the last two hours, work-auth clean. These get applied to *now*, because applicant
position is the cheapest edge available.

**Apply session, ~30 minutes, 3× a week.** Work the shadow queue. Extension
pre-fills; you review, adjust the LLM's answers to custom questions, submit.
10–15 applications a session, each one actually tailored. Tracking is automatic —
no bookkeeping.

**Outreach, ~15 minutes daily.** Every application gets 1–2 contacts. Priority
order: **referral (you know someone) > alumni > recruiter > hiring manager >
cold**. Drafts are already written; you review and send. Follow-ups draft
themselves on the `4,7,10` schedule and stop automatically when someone replies.

**Weekly review, ~20 minutes.** The funnel numbers: applications sent, response
rate, interview rate, by source and by work-auth tier. This is where you find out
that Handshake produces interviews and Adzuna produces noise, or that your resume
variant B is doing twice as well.

**Monthly.** Recalibrate. Re-run scoring against realized outcomes, adjust the
rubric, prune dead sources, refresh the E-Verify and H-1B datasets.

### What good looks like at 90 days

- Zero applications wasted on roles you're not legally eligible for
- Most applications submitted within hours of posting rather than days
- A meaningful share carrying a referral or alumni connection
- Every application's status accurate without you ever clicking a status button
- A calibrated match score that actually predicts callbacks
- A ranked, evidence-backed view of which companies can carry you past month 12
  and month 30

---

## Build order

Ordered by value-per-effort, not architectural tidiness. Items 1–6 need **no
extension at all** — they are server-side, small, and make the extension work more
valuable when it lands.

| # | Item | Effort | Why here |
|---|---|---|---|
| 1 | **JD hard-reject filter** (Gate 1) | hours | Immediate waste reduction on every cycle. Best ratio on the list. |
| 2 | **Resume work-auth line + precomputed ATS answers** | hours | Trivial; removes the most common silent rejection |
| 3 | **Auth on the API** | small | Hard prerequisite — the app currently has none, and everything below adds credentials and personal history to it |
| 4 | **`message_id` column + IMAP reply/bounce detection** | small | Fixes a live nagging risk; repairs `reply_rate` |
| 5 | **`SMTP_HOST=smtp.gmail.com` + warmup ramp** | one line + small | Unblocks deliverability and threading |
| 6 | **E-Verify + H-1B + PERM datasets, cap-exempt bonus** (Gate 2) | medium | The ranking layer; all data is free and public |
| 7 | **Agent protocol + extension skeleton** | medium | `BrowserTask`, `/api/agent/*`, MV3 shell, options page |
| 8 | **Link resolution in-browser + passive harvest** | medium | ATS discovery flywheel; zero account risk |
| 9 | **On-page overlay** | medium | Match score, already-applied warning, save, generate — where it starts feeling like a product |
| 10 | **Autofill + submit detection** | large | The daily-hours saver |
| 11 | **Authenticated search** (LinkedIn/Handshake/Indeed/Dice) | large | Highest account risk; do it last, after the harvest-first habits are established |
| 12 | **Alumni + referral finder** | medium | Highest response rate of any channel available |
| 13 | **Interview stage** (Stage 7) | medium | Only matters once interviews are arriving |
| 14 | **Calibration + reranker** (Stage 8) | medium | Needs accumulated outcome data first |
| 15 | **Local agent + selector-learning tier** | large | The long tail |

**Note on calibration data.** A retroactive mailbox backfill would supply hundreds
of labeled outcomes immediately — but only against a mailbox with history. The
dedicated job-search Gmail is new, so this depends on whether earlier applications
went to a prior address worth scanning read-only, one time. If not, item 14 is a
spring project.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| **No auth on the API** | Bearer token, HTTPS-only via the existing nginx, CORS scoped to `chrome-extension://<id>`. Hard prerequisite for anything holding mail credentials — item 3, not optional. |
| **LinkedIn ToS / account restriction** | Passive harvest by default; human-paced synthetic searches; low caps; per-site kill switches mirroring `WELLFOUND_ENABLED`. Never bulk-automate DMs or invites. The account is the asset. |
| **MV3 service-worker lifetime** (~30s idle) | Keep work in tabs (content scripts live as long as the tab); a connected `runtime.Port` or open WebSocket holds the worker alive; `chrome.offscreen` for genuinely persistent work. Long unattended crawls go to Engine B. |
| **Laptop offline** | Task TTL and queue drain on reconnect. API tier unaffected. "Agent last seen 3h ago" on the runs page. |
| **Selector rot** | Prefer JSON interception over DOM. Version adapters. Extend `source_diagnostics` / `describe_page` telemetry into the extension so the runs page says *"indeed: 0 cards, page looked like a login wall."* On parse failure, ship trimmed HTML to the VPS and let the LLM extract the jobs **and propose new selectors** — self-healing, using infra that already exists. |
| **Chrome Web Store review** | An extension touching LinkedIn will not pass. Don't publish — load unpacked. No auto-update, so build a version check against the VPS. |
| **Whole inbox reaching an LLM** | Filter by sender/subject server-side first; only matching mail goes to a model. Cheaper too. Largely moot with a dedicated mailbox. |
| **Misparsed status changes** | Record the evidence ("marked rejected from this email", linked) and make it one click to undo. A silent wrong rejection on an active application is a bad, invisible failure. |
| **Auto-replying to a human** | Never. Draft for review only — consistent with the philosophy `outreach_sender` is already built on. |
| **Becoming a mass-apply tool** | Quality gate; never auto-submit. The strategy is leverage, not volume — a spray tool actively works against the funnel math above. |

---

## Strategic note: this is also the only architecture that scales

Pushing execution to the user's own browser is framed here as a workaround for a
blocked VPS. It is also the thing that would make this shippable to other people.

A hosted job-search product dies on scraping economics: one shared IP pool, every
board blocking it, an enormous residential-proxy bill, and no legitimate way to
hold users' LinkedIn sessions. With a laptop tier, **the blocked-IP problem stops
existing, because there is no shared IP** — each user brings their own egress and
their own sessions.

And it compounds: every user's link resolutions enrich the shared board registry.
`company_board` already ranks boards by yield; federating that means ATS discovery
improves for everyone with each new user — a network effect on the asset that is
hardest to build.

Worth knowing now, because it argues for keeping the agent protocol
multi-tenant-shaped from day one, even while there is only one tenant.
