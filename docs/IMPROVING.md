# How this system could be better

Written after a bug hunt across the whole pipeline, which is a good moment to
write one of these: the same reading that finds defects finds the places where
nothing is defective and the design is simply not reaching as far as it could.

The ground rules from `ROADMAP.md` hold throughout. LLM and API calls are free,
so nothing here trades quality for call volume. There is no security work.
Priorities, in order:

1. **full, correct, structured job data**
2. **find every job**

Every item below says which of those it serves, and items serving the same
priority are ordered by how much they move it. Each one names the files, says
what "it worked" looks like, and — where the answer is no — says plainly what
it is *not* claiming.

One thing worth stating before the list. This pipeline is in unusually good
shape on the mechanics: three dedupe layers with a documented invariant, a
merge that takes the better half of two sightings, a per-run history table
behind every panel, `manual_fields` protecting the user from every automatic
writer, an eval harness for scoring quality. The gaps are not sloppiness. They
are almost all the same *shape* of gap, and naming it is most of the value of
this document:

> **Nearly every number this system reports is a numerator with no denominator.**

Source yield says how many jobs a source contributed, not how many it had.
Enrichment says how many descriptions it filled, not how many were missing.
Extraction says how many fields it wrote, not how many it got right. The funnel
says where jobs went, not whether the ones that arrived were the ones that
existed. Each of those is a measurement that cannot distinguish "working well"
from "quietly broken", and both priorities are currently unfalsifiable because
of it.

So the highest-value work is not more machinery. It is denominators.

---

## 0. First: one live defect, not an enhancement

### Salary is stored without its period

`jobs.salary_min` and `salary_max` are floats, with a `salary_currency` beside
them and nothing recording *per what*. The extraction prompt asks the model for
"the annual figure when the posting gives one; if it quotes an hourly rate,
give the hourly number", so both land in the same column. `app/models/job.py`
says so out loud in a comment on `salary_label`: *"Hourly and annual figures
land in the same column."*

The display handles it — 65 renders as "$65" and 135000 as "$135k". The
**filter does not.** `app/routers/jobs.py:232`:

```python
func.coalesce(Job.salary_max, Job.salary_min) >= floor
```

A $100k floor therefore hides a posting that states $65/hour, which is about
$135k a year and one of the best-paying things in the database. And it admits a
posting stating €100,000, on a floor the user meant in dollars. The salary
filter is the one place this project has repeatedly said a wrong number is
worse than a missing one, and here it is comparing three incompatible kinds of
number as if they were one.

**Serves:** priority 1, directly. This is stated data being read wrongly.

**Build:**

- Migration: add `salary_period` (`year` | `month` | `week` | `day` | `hour`,
  nullable) and `salary_annual_min` / `salary_annual_max` (float, nullable,
  index on the min as today).
- `job_details._SYSTEM_PROMPT`: stop asking the model to pick a convention.
  Ask for the figures **exactly as the posting states them** plus the period as
  a separate key. A model asked to normalise is a model asked to do arithmetic
  silently; a model asked to transcribe is being asked what it is good at.
- `job_details.normalize`: derive the annual pair with fixed multipliers
  (2080 hours, 260 days, 52 weeks, 12 months) and a currency conversion only
  when a rate is configured. **No rate configured means no annual figure**, and
  no annual figure means the job is excluded from a floor rather than admitted
  to it — the same direction the existing "jobs stating no salary are excluded
  from a floor" rule already goes.
- `deduplication.enrich_from`: `salary_period` joins the band as part of the
  same unit. A period from one source over figures from another is exactly the
  "range nobody stated" the merge already refuses.
- `Job.salary_label` renders the stated figure with its period ("$65/hr",
  "$120k–160k"), which is also more honest than what it shows today.
- The filter and the matcher prompt read the annual columns.

**It worked when:** a `salary_period = 'hour'` posting appears above a $100k
floor, and `select count(*) from jobs where salary_min is not null and
salary_annual_min is null` is only the rows with an unconvertible currency.

**Not claimed:** currency conversion will be a static table, not live rates.
For a filter with a round-number floor that is fine, and a stale rate is a much
smaller error than the 2000× one being made now.

---

## Priority 1 — full, correct, structured job data

### 1. Make the model quote its evidence

Nine fields come out of every description through one model call
(`app/services/job_details.py`). The prompt's rule is exactly right — *null
unless the posting states it* — and it is entirely unverified. Nothing anywhere
checks whether a stated `salary_min` of 180000 corresponds to any sentence in
the posting. `match_eval` (3.4) does this for *scoring*, against the user's own
verdicts. There is no equivalent for extraction, and extraction is upstream of
scoring, the salary filter, the seniority gate and every generated document.

The fix is available precisely because calls are free, and it needs no labelled
data at all.

**Serves:** priority 1. This is the difference between "structured" and
"structured and correct".

**Build:**

- Change the extraction reply shape so every non-null field carries a verbatim
  span from the posting:

  ```json
  {"salary_min": {"value": 180000, "quote": "The base salary range is
    $180,000 - $220,000"}, "required_years": {"value": null, "quote": null}}
  ```

- In `normalize`, verify each quote against the description with whitespace-
  and case-insensitive containment. **A field whose evidence is not in the text
  becomes null.** Not logged and kept — null. A hallucinated salary is the
  single most expensive error this pipeline can make, because it is the one
  the user filters on and the one the cover letter cites.
- Store the quotes. A new `job_field_evidence` table (`job_id`, `field`,
  `quote`, `verified`, `extracted_at`) makes the job detail page able to show
  *why* it thinks a job pays what it says, and makes the failure rate per field
  a number rather than a feeling.
- A second call, on a sample rather than everything, as the honest check on the
  first: give a different provider from the failover chain the description and
  the extracted row and ask it to name every field it disagrees with. Log the
  disagreements. This is the extraction equivalent of `_deep_score`, and the
  same reasoning applies — one model agreeing with itself is not evidence.

**It worked when:** the panel can say "of 4,120 extractions this week, 61
fields were dropped for unverifiable evidence" — and that number is nonzero,
because a zero means the check is not checking.

**Do this one first.** Everything downstream of extraction inherits its errors,
and right now the error rate is not merely unknown, it is unknowable.

### 2. A completeness panel: make priority 1 a number that moves

There is a funnel panel, a harvest health panel, an enrichment panel, a source
ROI table, a score distribution, a sweep table. There is nothing that answers
"how good is the job data?" — which is the stated first priority. The Aug 2026
audit answered it once, by hand, with `scripts/db_report.sql`, and the answers
(Adzuna truncates at 500 chars; LinkedIn is 90% description-less) drove the
whole of Phase 1. That was the most valuable single artefact in the project's
history and it has never been repeated on a schedule.

**Serves:** priority 1, by making it observable. Nothing on this list is worth
building if we cannot tell whether it helped.

**Build:**

- `app/services/data_health.py` — one query per cut, no new writes:
  - overall and **per source**: share of jobs with a description over
    `THIN_DESCRIPTION_CHARS`, with a salary, with `required_years`, with
    non-empty `required_skills`, with `posted_at`, with an `apply_url` that is
    not a known tracker host, with `details_extracted_at` set.
  - the same cuts for **jobs that reached `matched`**, which is the population
    that actually matters — thin data on a job nobody would want costs nothing.
  - a "regressions" row: any per-source figure more than 10 points below the
    same figure 30 days ago. That is what catches an adapter whose upstream
    changed a field name, which is the failure mode that has hit this project
    most often and always been found late.
- A panel on `/funnel` reading it, and a `data_health_runs` table so the
  30-day comparison exists at all — the same per-run-history pattern every
  other panel here uses.

**It worked when:** you can point at one source and say "this one contributes
8,000 jobs a cycle and 4% of them state pay", and then fix that adapter instead
of guessing which one to look at.

### 3. The missing entity: a `companies` table

Every job carries a company *string*. `company_boards` holds a company's ATS
slug and its yield history. Between them there is no company **record**, so a
whole layer of structured data has nowhere to live and consequently is not
collected:

- size band, industry, funding stage
- the careers domain, canonically, rather than per-board
- whether this employer's postings historically state visa sponsorship — which
  is exactly what `eligibility.scan` decides per posting and then forgets
- how many times the user has applied here, and what happened
- the normalized name, which `deduplication.normalize_company` recomputes from
  scratch on every comparison

**Serves:** both. Priority 1 because it is structured data the job rows cannot
hold; priority 2 because a company row is a board waiting to be polled (see
item 8, which needs this table to exist).

**Build:**

- `companies` table keyed on `normalize_company(name)` with the raw display
  name beside it. Populated lazily: the first time a company name is stored,
  enqueue a fill.
- The fill is one model call plus one careers-page sniff (`ats_sniffer.py`
  already does the second half). Free, so it can run for every company in the
  database, not just new ones.
- `jobs.company_id`, nullable, backfilled — but **keep `jobs.company`**. The
  string is what the source said, and replacing it with a foreign key would
  make a company rename rewrite history.
- Aggregate the per-posting eligibility verdicts up: a company whose last
  twelve postings all said "must be authorised to work without sponsorship" is
  a company worth deprioritising *before* the scoring call, not after.

**Not claimed:** this does not improve matching on its own. It is the place the
next three things would each otherwise invent for themselves.

### 4. Store the requirements, not just the posting

The description is the input to the keyword filter, the skill count, the
seniority gate, the scoring call, the deep-scoring call, the detail extraction
and all six document-generation calls. Every one of them receives the whole
page and is asked to find the part it cares about, and the part it cares about
is almost always the requirements section.

`enrichment` already fetches the fuller text; `job_details` already reads the
posting once. Splitting it costs nothing extra.

**Serves:** priority 1. Same data, structured instead of prose.

**Build:**

- Extend the extraction call to return the posting split into
  `responsibilities`, `requirements`, `benefits` and `about_the_company`, each
  as a verbatim slice of the description — verbatim so the same containment
  check as item 1 applies, and a model that paraphrases is caught.
- Store on the job. `MATCH_DESCRIPTION_CHARS` truncation then falls on the
  boilerplate first instead of wherever the 16,000th character happens to be —
  which today is the bug `test_a_long_description_is_no_longer_cut_at_4000_chars`
  was written about, one order of magnitude further out.
- `_count_skill_matches` reads the requirements section when there is one. A
  skill named in "about us" is marketing; a skill named in the requirements is
  a requirement, and the `few_skills` rejection is currently unable to tell the
  difference.

**It worked when:** `few_skills` rejections drop and the jobs that stop being
rejected are, on inspection, ones a person would also have kept.

### 5. Share facts across near-duplicates without merging them

The dedupe hash catches exact-normalized matches. What slips through is the
cross-post with a cosmetic title difference, and
`find_duplicate_application_job` already computes exactly that similarity — but
only for jobs that already carry an application, and only to *reject* the
second one.

Last session I deliberately declined to promote fuzzy title matching to an
ingest merge layer, and that judgement stands: "Software Engineer, Payments"
and "Software Engineer, Platform" score about 0.87, and merging them deletes a
job, which is the worst thing this system can do under priority 2.

But **enriching does not delete anything.** If two rows are 0.9-similar on
normalized title at the same normalized company, they are very likely the same
posting, and the cost of being wrong is that a Payments role gains the Platform
role's salary band — from the same company, in the same week, for the same
title stem. That is a small error in exchange for a large amount of filled-in
data, and it runs in the direction the merge rules already accept as safe:
fill blanks only, never overwrite, never over `manual_fields`.

**Serves:** priority 1, using data already in the database.

**Build:**

- A scheduled pass, not an ingest hook — this wants the whole table, not one
  row's neighbours. Group candidates by `normalize_company`, compare titles
  within the group, and for each pair over the threshold call the existing
  `enrich_from` **in both directions**.
- Record it: a `shared_from_job_id` list, or an `agent_events`-style row, so a
  salary that appeared without a source is traceable. Data that arrives from
  nowhere is data nobody can trust.
- Never touch `location` (it is a third of the hash) or `description` (length
  is not similarity, and this is the one case where the two rows genuinely
  differ).

**Not claimed:** this does not reduce the row count and is not meant to. Two
rows is the correct answer when we are 0.9 sure, and this is about the facts,
not the cardinality.

---

## Priority 2 — find every job

### 6. The recall probe: the denominator that does not exist

This is the most important item in the document.

Nothing in this system can answer "what fraction of the jobs that exist do we
have?" Source yield, board yield, harvest yield, sweep pages — all numerators.
So a source that quietly starts returning page one only, an adapter whose
upstream renamed a field, a board whose pagination changed: each of them shows
up as "fewer new jobs this cycle", which is indistinguishable from a quiet week
in the job market. Every failure this project has actually had in production
looked exactly like that.

The trick is that **ground truth is obtainable for a small panel.** An ATS
board API returns a company's *complete* current openings in one request. That
is not a sample, it is the list. So for a fixed panel of companies we can
compute true recall, and — much more usefully — enumerate the specific postings
we are missing and look at why.

**Serves:** priority 2, by making it measurable for the first time.

**Build:**

- Pick a panel: 40–60 companies the user actually cares about, spread across
  ATSes (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable) and
  across sizes. Store it as a flag on `companies` (item 3) — `in_recall_panel`
  — so it is data rather than a constant in a file.
- A weekly task: for each panel company, fetch its board directly, and for each
  posting returned ask whether we hold it, by URL, then by
  `source` + `source_job_id`, then by `dedupe_hash` — the same three layers
  `find_existing_job` uses, so the probe measures the system's real answer and
  not a fourth opinion.
- Store a row per probe in `coverage_runs`: company, ATS, postings on the
  board, postings held, and **the missing ones as a list of URLs**. That list is
  the whole point. A recall number tells you there is a problem; five URLs tell
  you what kind.
- Classify each miss automatically, because the classes want completely
  different fixes: *never fetched* (a coverage gap), *fetched and archived*
  (working as intended), *fetched and filtered* (a matching question, not a
  coverage one), *held under a different hash* (a dedupe split — the miss is
  ours). Only the first is a priority-2 failure, and today all four are
  invisible together.
- A panel on `/runs`, and one number on it: recall, over the panel, this week.

**It worked when:** that number exists, and moves when an adapter breaks. If
recall over the panel is 95%, most of the rest of this section is not worth
building and the effort belongs in Priority 1. If it is 55%, everything below
is urgent. **Nobody currently knows which of those is true**, and that is the
strongest argument for doing this before anything else on this list.

### 7. The cheap denominator: reconcile against what each board says

Item 6 needs a panel chosen by hand. This one needs nothing, because the number
is already being stored and thrown away.

`company_boards.last_job_count` is how many postings a board returned on its
last poll. So for every board, every cycle, we already have both halves of
"the board offered N" and "we hold M for this company". The comparison is one
query and it has never been made.

**Serves:** priority 2, for every board rather than a panel of them.

**Build:**

- After each board poll, store `held_count` beside `last_job_count` — jobs in
  `jobs` for that company, not closed, plus the archived ones.
- Flag any board where held is materially below offered and has been for two
  consecutive cycles. Two cycles because one is noise: a posting fetched
  minutes before a snapshot is legitimately absent.
- Show it in the existing board registry view, sorted by the size of the gap.
  A board offering 214 and holding 187 is worth a look; a board offering 214
  and holding 12 is a broken adapter, and today it appears in the yield table
  as a board that "contributed 12", which reads as a small company.

**It worked when:** the first pass finds at least one board with a gap nobody
knew about. It will.

### 8. Company-first discovery, instead of link-first

Board discovery today is reactive: configured slugs, a verified seed list, and
boards found in links inside jobs we already fetched, plus careers pages we
sniffed. Every path starts from *a job we already have*, which means the
companies we have never seen a posting from are structurally invisible. Those
are the majority of employers, and they include most of the interesting ones,
because the companies that flood aggregators are not usually the companies the
user most wants.

Calls are free. That changes what "enumerate the market" costs.

**Serves:** priority 2, at the top of the funnel where a fix multiplies.

**Build:**

- **Competitor expansion.** For every company the user has applied to or
  favourited, ask a model for 20–30 companies that compete with it or hire the
  same roles. Insert each into `companies` (item 3) unresolved, then run the
  existing pipeline at it: `company_domain` for the domain, `ats_sniffer` for
  the board, `ats_validation` to confirm the slug is real. Two hundred known
  companies become several thousand candidates, and the ones that resolve to a
  board are boards nothing else would ever have found.
- **List ingestion.** Note what the existing adapters do and do not do here:
  `ycombinator.py` walks YC's fixed role-page taxonomy, and `hnhiring.py` reads
  a thread — both harvest *jobs*, and the companies behind them are a by-product
  that gets thrown away after the board-from-link discovery has had its look. A
  list of companies is the better input, and there are many of them
  (accelerator batches, "who is hiring" threads read for their employers rather
  than their postings, public funding announcements). Each one becomes a
  `companies` row and goes through the sniffer, whether or not it was
  advertising a job that day — which is the whole difference, because a company
  with nothing open this week will have something open next month and we would
  currently never learn it exists.
- **Rank the candidates before spending on them.** A model call per candidate
  scoring "would this company plausibly hire for the user's target roles" is
  free and keeps `ats_validation` from spending its budget probing dentists.
- Feed the result through `ats_validation` as it stands. The lesson recorded
  there — that `greenhouse/linkedin` and `greenhouse/appcast` were polled for
  months on a guess — applies with much more force at ten times the volume.

**It worked when:** the recall probe from item 6 improves, or the board registry
gains boards whose `origin` is the new one and whose yield is not zero. Note
that this item is unfalsifiable *without* item 6, which is why it comes after.

### 9. A query grid that learns which queries work

Aggregator sources are queried by (role × location). `expand_search_queries`
turns the target roles into the titles recruiters actually post, caches them on
the profile, and the fetcher searches under all of them — good, and the title
gate was correctly taught to accept them too.

What is missing is feedback. No query is ever retired for contributing nothing,
no query is ever promoted for contributing a lot, and no new query is ever
generated from evidence — and the evidence is right there in the database, in
the titles of the jobs that actually scored well.

**Serves:** priority 2.

**Build:**

- A `search_queries` table, replacing the `search_query_cache` blob on the
  profile: text, source, first used, times used, jobs fetched, **jobs that were
  new**, jobs that reached `matched`.
- Attribute at insert time. The fetcher knows which query produced which raw
  job; it currently drops that on the floor. This is the only part of the item
  that touches the hot path, and it is one extra column on the raw dict.
- Retire on evidence: a query with 500 fetched and 0 new over ten cycles is
  pure request spend, and request budget is the one thing that genuinely is
  scarce here.
- Grow on evidence: once a quarter, feed the titles of the last 50 jobs that
  scored over the threshold back to the model and ask for query variants. A
  title that produced a good match is a much better prompt than a target role.
- Widen the grid deliberately: the location axis is currently the user's
  preferences, which is right for filtering and too narrow for *finding* —
  a remote job posted against a city we did not ask about is a job we want and
  do not see.

**It worked when:** the table shows queries being retired and the total new-jobs
count per cycle does not fall.

### 10. Per-source pagination depth

`db_learn.sql` section G asks whether the browser crawl ever got past page one,
which was the right question to ask of the browser tier and is just as good a
question to ask of every API source. Nothing records it. A source with a
hardcoded page cap, or one whose `results_per_page` the upstream silently
lowered, loses jobs with no symptom other than a slightly smaller number.

The Tsenta sweep already has the right shape for this — it measures the page
size actually *served* rather than trusting the one it asked for, precisely
because an API that caps `limit=100` at 20 answers 200 OK and reads as "the
list ended". Nothing else in `sources/` does that.

**Serves:** priority 2, cheaply.

**Build:**

- Every adapter that paginates records, per run: pages requested, pages
  returned, rows on the last page, and whether it stopped because the page was
  short or because it hit its own ceiling. Store in the existing per-source
  stats dict that already flows into `fetch_runs`.
- Show "stopped at its own ceiling" prominently. That is a source telling you it
  has more to give, and it is currently a silence.
- Apply the served-page-size rule generally: compare each page against the size
  page one came back at, never against the size requested.

**It worked when:** at least one source turns out to be stopping at a ceiling,
and raising it produces jobs.

---

## Deliberately not doing

Worth writing down, because each of these is a reasonable idea that would make
the system worse, and the reasoning is the part that would otherwise be lost.

- **Merging near-duplicate rows at ingest.** Item 5 shares facts and keeps both
  rows on purpose. A merge deletes a job, and under "find every job" a duplicate
  is a small cost while a deletion is the largest one available. 0.87 similarity
  is not a fact.
- **Deleting anything.** Archiving moves rows and keeps the three columns
  deduplication reads, which is the only reason archiving is safe. Any future
  cleanup has to keep that property or it re-fetches, re-scores and re-archives
  the same postings forever.
- **A bigger `MATCH_DESCRIPTION_CHARS`.** The problem was never the ceiling, it
  was that the truncation fell on the requirements. Item 4 fixes the actual
  thing; raising the ceiling only makes the wasted half bigger.
- **Automatic profile changes from matching outcomes.** The user's rejections
  are the only ground truth this system has about its own scoring
  (`match_eval` builds its fixture from them). A loop that edited the profile
  from those verdicts would be training on its own output and would destroy the
  fixture in the process.
- **Off-box backups.** Already decided: on the VPS and nowhere else. The gap is
  real and documented in `docs/BACKUPS.md` — this survives a bad migration, not
  the loss of the machine — and it is a decision, not an oversight.
- **Rate-limit or spend engineering.** Calls are free. The only scarce resource
  is outbound request budget against boards that will block us for volume, and
  that is already handled by pacing and the browser tier.

---

## Order of work, and why

The ordering follows one rule: **build the thing that tells you whether the next
thing worked, first.**

1. **Salary period** (§0). A live defect in stated data, hiding the
   best-paying jobs in the database from the user's own filter.
2. **Quote-grounded extraction** (§1). Everything downstream inherits
   extraction's errors, and the error rate is currently unknowable rather than
   merely unknown. It also needs no new tables and no labelled data.
3. **The recall probe** (§6). The answer decides whether the rest of Priority 2
   is urgent or already fine. Doing items 8, 9 or 10 before this one means
   building coverage machinery with no way to tell if it helped.
4. **The completeness panel** (§2) and **board reconciliation** (§7). Both are
   query-only, both are a day's work, and between them they turn most of
   Priority 1 and a useful slice of Priority 2 into numbers on a page.
5. **The `companies` table** (§3), then **company-first discovery** (§8), which
   needs it.
6. **Requirements sections** (§4), **near-duplicate fact sharing** (§5),
   **the query grid** (§9), **pagination depth** (§10) — in whatever order the
   panels from step 4 say is worst.

Steps 1–4 are the ones that change what is knowable. Everything after them is
ordinary work, and much easier to prioritise once those numbers exist.
