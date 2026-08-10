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

### Stage 0 — Profile

| Feature | Tier | Notes |
|---|---|---|
| Import profile from your own LinkedIn | Extension | One click; kills the worst part of onboarding |
| Market-driven skill gaps | VPS | "61% of your 340 matched jobs want Kubernetes; you don't list it" |

**Immigration status is out of scope for the profile.** It is not stored as a
profile field, never appears in a generated resume or cover letter, and is never
passed to an LLM. See Stage 2.

### Stage 1 — Discovery

| Feature | Tier | Notes |
|---|---|---|
| API-tier sources | VPS | Existing — 25 adapters, unchanged |
| **Browser-tier sources** | Extension | LinkedIn (Voyager), Handshake, Indeed, Dice, Wellfound, ZipRecruiter — with your session |
| **Passive harvest** | Extension | Everything you browse is captured. Zero detection risk: you *are* the traffic. Default mode. |
| **Link resolution in-browser** | Engine B | Adzuna/Jooble/Careerjet interstitials that `link_resolver`'s httpx client can't follow |
| Selector-learning fallback | Engine C | Unknown career site → LLM figures out the structure once → cached as a generated adapter |

**Link resolution deserves priority out of proportion to its size.** Every
resolved interstitial feeds ATS auto-discovery: more Greenhouse/Lever/Ashby slugs
discovered → more jobs arriving through clean APIs that never need a browser
again. The browser tier is not just a scraper, it is a *bootstrapper for the API
tier*, and that flywheel compounds while everything else is a per-fetch cost.

### Stage 2 — Qualification (what the posting says about eligibility)

One deterministic scan of `Job.description`, producing **two tiers with different
consequences**. Both quote the posting's own sentence back; neither infers anything
about the user, and neither reaches an LLM.

#### Tier 1 — Blocking: citizenship-restricted

Postings that state outright the role is closed to non-citizens. Unwinnable
regardless of merit, so they are filtered, writing `filter_reason` /
`filter_detail` (existing columns).

- `must be a US citizen` / `US citizenship required` / `citizens only`
- `active security clearance` / `Secret` / `Top Secret` / `TS/SCI` — clearance is
  not grantable without citizenship, so the posting is closed either way
- `US Person` / `ITAR` / `EAR` / `export control`

Overridable per job in one click, like every other filter.

#### Tier 2 — Advisory: sponsorship statements

Postings that say something about sponsorship, **in either direction**. These are
**surfaced, never acted on** — the job stays in the list, keeps its score, and is
ranked exactly as if the statement were absent. The user reads it and decides.

Stored as the quoted sentence plus a direction, in `jobs.sponsorship_note`:

| Direction | Example phrasings |
|---|---|
| **Negative** | `will not sponsor`, `unable to sponsor`, `no visa sponsorship`, `without sponsorship now or in the future`, `must not require sponsorship` |
| **Positive** | `sponsorship available`, `we sponsor visas`, `visa sponsorship provided`, `open to sponsoring` |

Positive statements matter as much as negative ones — a posting that volunteers
"we sponsor" is useful information, and the same scan catches both for free.

**Where it shows.** As a small badge with the quoted sentence on hover: the `/jobs`
list, the job detail page, the `/today` apply queue, and — most importantly — the
extension overlay on the posting itself, which is where the decision actually gets
made.

**What it must never do.** Not a filter. Not a score input. Not a ranking input.
Not in any LLM prompt. Not in a generated document. It is a label on a card.

#### Immigration status never reaches an LLM

- Not stored on the profile
- Never written into a resume or cover letter
- Never included in a matching or scoring prompt
- Never used as a ranking input

The Tier 2 note is the posting's statement about itself, not a statement about the
user, which is what keeps it on the right side of this rule.

**This requires a change to existing v1 behaviour.** `matcher.py:286` currently
allocates 0–15 points to *"Location/remote/work-authorization compatibility"*.
Work authorization comes out of that criterion; the points go to location and
remote fit alone. Today the model is being asked to reason about a status it has
no information about, which can only produce noise.

#### Still out of scope

- **No employer immigration datasets** — no E-Verify lookup, no H-1B filing
  history, no PERM records, no cap-exempt classification
- **No timeline modelling** — no OPT dates, no clock, no status-aware weighting
- **No inference beyond the text** — if the posting says nothing about
  sponsorship, the system says nothing about it either

**Rationale.** Blocking is for doors the posting says are closed. Everything else
is information the user is better placed to weigh than a scoring rubric is —
surfacing it costs one regex pass and preserves the judgement call, where filtering
on it would silently discard jobs that are often still worth applying to.

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
| **Interview intelligence corpus** | Reported interview experiences, per company, per role. See below — this is the substrate for everything else in this stage. |
| Interview dossier | Reported rounds and questions + JD + your STAR stories from the profile narrative + the interviewer's background. Fires automatically when Stage 6 sees the invite. |
| Interviewer research | The invite names them → extension pulls their LinkedIn → "4 years there, ex-Stripe, posts about Rust" |
| Mock interview | Run the *actual reported questions* for that company in the reported format. Only possible with the corpus. |
| Thank-you notes | `thank_you` is already in `MESSAGE_KINDS` |
| Question bank | Capture questions actually asked; prep against real history instead of generic lists. Also validates the corpus — you learn which sources are accurate for which companies. |
| Offer / comp comparison | Against salary data harvested from postings (CA/NY/CO/WA mandate ranges — this becomes a comp dataset filtered to *your* exact segment, more relevant than levels.fyi) |

#### Interview intelligence corpus

People publish extremely detailed interview writeups — rounds, questions, timeline,
outcome. Aggregated per company and kept fresh, that turns interview prep from
generic grinding into a targeted, time-boxed plan.

**Triggered by the pipeline, and by hand.** Stage 6's IMAP poller detects the
interview invite and fires a research task automatically, so the dossier exists by
the time you read the email. It is also a button on any application — automation
decides when this *usually* happens, the user decides when it happens *now*. See
`docs/PRODUCT.md` §7 for the full trigger matrix; this rule applies to every
automatic behaviour in the system, not just this one.

**Sources, in build order (effort ascending, not value):**

| Source | Access | Notes |
|---|---|---|
| **GeeksforGeeks Interview Experiences** | Plain HTML, no auth, no blocking | One of the largest archives, already organised by company. Start here. |
| **Reddit** | Append `.json` to any URL — free, no key | r/leetcode, r/cscareerquestions, company subs |
| **GitHub interview repos** | API, token you already have | Large curated collections |
| **LeetCode Discuss** | `leetcode.com/graphql` — prefer over DOM | Cloudflare-walled from a datacenter IP → **Engine B or the extension**. This is the extension's value proposition again. |
| **LeetCode company tags** | Extension, Premium — **available** | Problems tagged by company with frequency and recency. The single highest-value dataset here, and the account has Premium, so treat it as a first-class source rather than a maybe. |
| **Glassdoor interview questions** | Extension (login wall, aggressive blocking) | Large per-company corpus |
| **Blind** | Extension, needs a verified work email | Optional — may not be available |

**Retrieval is the hard part, not ingestion.** Three things decide whether a
retrieved report is useful:

- **Recency, weighted hard.** Loops change. A 2019 report is noise; a report from
  last quarter is gold. Down-weight past ~18 months, discard past ~3 years unless
  there is nothing else. A report with no date is nearly worthless — require one.
- **Level and org match.** "Amazon SDE-1 University Grad" and "Amazon SDE-2
  lateral" are different loops. Some companies vary by org and site.
- **Company match** — fuzzy, via the existing `company_domain.company_key`.

**The output is a practice plan, not a summary.** Not *"they ask graph
problems"* but:

> *5 rounds: OA (2 problems, 90 min) → phone screen → 3 onsite. Reported
> questions map to 18 LeetCode problems; these 6 appeared more than once since
> January. Median process length 4 weeks. Two reports mention a system design
> round at your level — your profile has no distributed systems experience, so
> that's the gap to close.*

Problem titles get extracted from post text by the LLM and mapped to LeetCode
slugs, so the plan is clickable. With Premium, company tag frequency supplies the
same thing directly and more reliably.

**Useful before the interview too.** Round count, take-home vs. not, reported
process length, and "ghosted after final round" reports are all *ranking* signals
at Stage 3 — worth knowing which loops cost four weeks before you apply.

**Also feeds the OA path.** Stage 6 parses online-assessment deadlines; the corpus
usually says exactly what a given company's OA contains. Very targeted prep on a
tight clock.

**Storage.** A company-scoped `InterviewReport` table (`company_key`, `role`,
`level`, `source`, `source_url`, `posted_at`, `rounds` JSONB, `questions`,
`outcome`, `difficulty`, `quality_score`) — scoped to the company rather than to
an application, so it accumulates and is reused, mirroring the `Contact.company_key`
pattern. Plus a cached `InterviewDossier` per company × role on a ~30-day TTL.

Caching matters more than it looks: you interview at maybe twenty companies, so a
per-company cache means very low request volume and almost no detection surface.

**Honest limits:**

- **Coverage collapses for small companies.** A 30-person startup has zero posts.
  Degrade gracefully to JD analysis + interviewer LinkedIn + the company's
  engineering blog + role-based prep. Never fabricate a loop.
- **Forum posts are wildly variable** — from "got rejected :(" to 2000-word
  writeups. LLM-extract into the structured schema and score quality; store
  structured, never raw dumps.
- **Every claim must cite its source, with a link.** A hallucinated interview
  question is worse than no dossier, because it is acted on. Nothing enters the
  dossier without a traceable post behind it.
- **Reports are self-selected.** People who bombed post less than people who
  passed, and hard loops get written up more than easy ones. Treat difficulty
  signals as directional.

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

**Therefore the system optimizes for `early × warm × well-matched`, not raw
volume.** Every feature above serves one of those three:

- **Early** — Stages 1 and 3. Speed lane, fresh postings, applicant counts.
- **Warm** — Stage 5. Alumni, referrals, 2nd-degree paths, and outreach that
  actually gets delivered and actually gets followed up.
- **Well-matched** — Stages 2b, 3 and 8. Real matches, not ghost jobs, ranked by a
  score that has been calibrated against actual callbacks.

Stage 2 sits underneath all three as a cheap correctness check: don't spend an
application on a posting that says citizens only.

### The daily loop

**Morning, ~10 minutes.** Check speed-lane notifications — high match, posted in
the last two hours. These get applied to *now*, because applicant position is the
cheapest edge available.

**Apply session, ~30 minutes, 3× a week.** Work the shadow queue. Extension
pre-fills; you review, adjust the LLM's answers to custom questions, submit.
10–15 applications a session, each one actually tailored. Tracking is automatic —
no bookkeeping.

**Outreach, ~15 minutes daily.** Every application gets 1–2 contacts. Priority
order: **referral (you know someone) > alumni > recruiter > hiring manager >
cold**. Drafts are already written; you review and send. Follow-ups draft
themselves on the `4,7,10` schedule and stop automatically when someone replies.

**Weekly review, ~20 minutes.** The funnel numbers: applications sent, response
rate, interview rate, by source. This is where you find out that Handshake
produces interviews and Adzuna produces noise, or that your resume variant B is
doing twice as well.

**Monthly.** Recalibrate. Re-run scoring against realized outcomes, adjust the
rubric, prune dead sources.

### What good looks like at 90 days

- Most applications submitted within hours of posting rather than days
- A meaningful share carrying a referral or alumni connection
- Every application's status accurate without you ever clicking a status button
- A calibrated match score that actually predicts callbacks
- No applications spent on postings that stated citizens-only in the text

---

## Build order

Ordered by value-per-effort, not architectural tidiness. Items 1–5 need **no
extension at all** — they are server-side, small, and make the extension work more
valuable when it lands.

| # | Item | Effort | Why here |
|---|---|---|---|
| 1 | **Eligibility scan** (Stage 2) | hours | One pass, two tiers: filter citizens-only, surface sponsorship statements as advisory badges |
| 2 | **Drop work-auth from the matcher rubric** | minutes | `matcher.py:286` scores a status the model knows nothing about — pure noise |
| 3 | **Auth on the API** | small | Hard prerequisite — the app currently has none, and everything below adds credentials and personal history to it |
| 4 | **`message_id` column + IMAP reply/bounce detection** | small | Fixes a live nagging risk; repairs `reply_rate` |
| 5 | **`SMTP_HOST=smtp.gmail.com` + warmup ramp** | one line + small | Unblocks deliverability and threading |
| 6 | **Agent protocol + extension skeleton** | medium | `BrowserTask`, `/api/agent/*`, MV3 shell, options page |
| 7 | **Link resolution in-browser + passive harvest** | medium | ATS discovery flywheel; zero account risk |
| 8 | **On-page overlay** | medium | Match score, already-applied warning, save, generate — where it starts feeling like a product |
| 9 | **Autofill + submit detection** | large | The daily-hours saver |
| 10 | **Authenticated search** (LinkedIn/Handshake/Indeed/Dice) | large | Highest account risk; do it last, after the harvest-first habits are established |
| 11 | **Alumni + referral finder** | medium | Highest response rate of any channel available |
| 12a | **Interview corpus — free sources** (GfG, Reddit, GitHub) | small | No auth, no blocking, no extension needed. Jumps the queue entirely if interviews are already scheduled. |
| 12b | **Interview corpus — walled sources** (LeetCode Premium tags, Glassdoor) | medium | Needs the extension; add once 12a proves the retrieval and scoring work |
| 12c | **Dossier, mock interview, question bank** (rest of Stage 7) | medium | Only matters once interviews are arriving |
| 13 | **Calibration + reranker** (Stage 8) | medium | Needs accumulated outcome data first |
| 14 | **Local agent + selector-learning tier** | large | The long tail |

**Note on calibration data.** A retroactive mailbox backfill would supply hundreds
of labeled outcomes immediately — but only against a mailbox with history. The
dedicated job-search Gmail is new, so this depends on whether earlier applications
went to a prior address worth scanning read-only, one time. If not, item 13 is a
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
