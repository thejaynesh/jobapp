# How other job-search systems find every posting, and what we should copy

Research done 2026-09-28. Scope: **finding US job postings**, meaning the
sources, how companies are discovered, and how boards are polled. Matching,
documents and outreach are out of scope.

This builds on two existing documents rather than repeating them:
`docs/JOB_SOURCE_AUDIT.md` (what is broken in our intake today) and
`docs/IMPROVING.md` §6–§10 (recall probe, board reconciliation,
company-first discovery, query grid, pagination depth). Where this document
agrees with those, it adds evidence and a concrete method.

**Labels used below.** *Measured* means I ran it on 2026-09-28 against live
data or our code. *Reported* means a cited source says so and I did not
reproduce it.

---

## Status: what has been built from this (2026-09-28)

Each item below was checked against a live site before it was written, and
has tests from the captured response shapes.

| Recommendation | Built | Commit subject |
|---|---|---|
| 1. Poll more Workday tenants | Tenant cap 30 → 150 and workers 8 → 16, both on the settings page. Detail requests go to titles the matcher wants. Every Workday company's other career sites come from its robots.txt (Salesforce's `Futureforce_NewGradRoles`). 565 verified sites of large US employers are seeded. | "Poll five times the Workday tenants…", "Find every Workday site a company runs…" |
| 2. SimplifyJobs as a source | `simplify` source; listings files mined for boards uncapped and named; dead list dropped. | "Ingest SimplifyJobs' postings…" |
| 3. Oracle Recruiting Cloud | `oracle` board adapter plus enrichment reader. | "Read Oracle, SuccessFactors, Phenom and Eightfold…" |
| 4. Common Crawl discovery | `services.commoncrawl`, walked on an hourly due-check across 19 ATS hosts. | "Find company boards in Common Crawl's URL index", "Recognise the posting links discovery was dropping" |
| 5. LinkedIn apply link | Not possible any more; see §6. | – |
| 6. Eightfold, Phenom, SuccessFactors | All three, plus iCIMS careers-home (Jibe) sites. | as 3, and "Read iCIMS careers-home sites…" |
| 7. Greenhouse list-first | Not done. It needs a measurement of where board time goes first, which needs production. | – |
| 8. Work-authorization data | Not done. It only matters if sponsorship matters to you. | – |
| 9. Commercial feed as a yardstick | Not done. It needs an account. | – |
| 10. Long tail | Rippling, Pinpoint, Taleo, Paylocity and JazzHR (every open posting, from its own sitemaps), plus Amazon's, TikTok's and Apple's own searches. UKG and Dayforce postings are read by enrichment only: UKG's robots.txt disallows its search endpoint and Dayforce's search refuses plain requests, so neither gets a board poller. Avature remains (its feeds are off or hold test postings). | "Read Rippling and Pinpoint boards", "Read Amazon's careers search…", "Read TikTok's…", "Read Apple's…", "Read JazzHR…", "Read Taleo career sections", "Read Paylocity boards, and UKG and Dayforce postings" |

### Recall against SimplifyJobs, measured

The 1,983 active new-grad and internship postings SimplifyJobs listed over
the 60 days to 2026-09-28, by whether the host they link to is one we read:

| | Recognised | Share |
|---|---|---|
| Before this work's second round | 1,359 | 69% |
| After discovery fixes (Workday sites named "careers" or "2", `myworkdaysite.com`, EU Greenhouse and Lever, `jibeapply.com`) | 1,569 | 79% |
| After TikTok, Apple and JazzHR | 1,790 | 90% |

What remains, largest first: ByteDance (77; its API answers only its own
page, so it is left alone), Tesla (11; bot-protected), Taleo sections without
a search portal, and a long tail of employer domains. 257 of those domains
wrap Greenhouse behind `?gh_jid=`, and the careers-site sniffer now finds the
board for 212 of them ("Find the Greenhouse board behind employers' own
careers sites"), 191 of them boards we had no other way to learn.

## Round three: other job projects, and the extensions (2026-09-28)

A survey of open-source job projects on GitHub not covered in §3, with the
browser extensions this time. The GitHub search API is not reachable from
this environment, so projects were found by web search and read from
`raw.githubusercontent.com`. Licences were checked before anything was reused:
**data** was taken only from MIT-licensed projects; from the rest, and from
the GPL-3.0 ones, only ideas.

| Project | What it is | What we took | Commit subject |
|---|---|---|---|
| [Feashliaa/job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) (MIT) | 1M+ postings from 20k+ companies, daily on GitHub Actions; per-ATS company lists harvested from Common Crawl. | Its lists, read by the board harvest: **46,644 boards** we had no other way to know (11,233 Workday sites). New boards are also probed hourly now, so the backlog drains in about a week. | "Harvest other projects' board registries…" |
| [haoawake/career-radar](https://github.com/haoawake/career-radar) (no licence) | 603 employers' ATS APIs, seniority and visa signals with evidence. | Retiring postings missing from a *complete* board read: nine full-feed adapters now close a posting the moment its board stops listing it. Its visa classifier we already have (`services.eligibility`). | "Close postings the moment their board stops listing them" |
| [berellevy/job_app_filler](https://github.com/berellevy/job_app_filler) (BSD-3), [nikhil-ghind/autograph](https://github.com/nikhil-ghind/autograph) (GPL-3.0), [andrewmillercode/Autofill-Jobs](https://github.com/andrewmillercode/Autofill-Jobs), [ankitsharma38/Workday-Autofill-Assistant](https://github.com/ankitsharma38/Workday-Autofill-Assistant) | Autofill extensions for Workday, Greenhouse, Lever and up to 21 ATSes. | Ideas only: radio-button questions, custom listbox dropdowns, following a multi-step Workday form as it renders, and a store of answered questions. Written afresh in `extension/autofill.js`, with the answer store on the user's server, plus EEO declines. Its browser tests found two old bugs (the inverted sponsorship question, and "ethnicity" matching the location rule). | "Autofill radio questions, custom dropdowns, EEO declines and remembered answers" |
| [seancampbell3161/job-aggregator](https://github.com/seancampbell3161/job-aggregator) (Apache-2.0) | Self-hosted alert pipeline, 15 ATS families, discovery from YC and VC portfolio lists. | The YC idea, through [yc-oss](https://github.com/yc-oss/api): 1,478 hiring YC companies' websites looked behind for their boards. It also declines to work around hiring.cafe disallowing its search endpoint — the same line this project draws. | "Look behind the websites of hiring YC companies…" |
| [FastApply/job-aggregator](https://github.com/FastApply/job-aggregator) (no licence) | 5.1M live jobs across 88k companies from ~25 ATS adapters. | Nothing reusable without a licence. Its note that ingestion stops when the crawling laptop sleeps is the argument for our server-side scheduling. | – |
| [andriuskleinas/job-tracker](https://github.com/andriuskleinas/job-tracker) (MIT), [Eugene-Mokrushin/job-grabber](https://github.com/Eugene-Mokrushin/job-grabber), [str58290/Job_Application_Tracker_Extension](https://github.com/str58290/Job_Application_Tracker_Extension) | Extensions that clip a posting from the page, or walk a search's result cards. | Nothing new: our harvest reads the page's own API responses rather than its markup, and the panel's "Save" reads JSON-LD first. | – |
| [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), AIHawk, [GodsScion/Auto_job_applier_linkedIn](https://github.com/GodsScion/Auto_job_applier_linkedIn) | Agents that submit applications unattended. | Deliberately nothing. The fill here stops short of submitting, and the reasons in the extension README stand. | – |

Still open from this round:

- **Greenhouse descriptions on demand.** `?content=true` is 5.4 MB for
  Stripe's board against 0.44 MB without (measured). career-radar lists
  first and fetches descriptions later. Every board job is stored today,
  descriptions included, so a job that would match a role added later is
  already whole; trimming that is a trade-off to measure, not a clear win.
- **Rate-limit cooldown per Workday cluster.** career-radar cools a whole
  `wdN` host for a minute after a 429, because most tenants share `wd1` and
  `wd5`. We stop an adapter on a blocking status; per-cluster cooldown would
  let the other clusters carry on.
- **The panel on more ATS hosts.** autograph fills 21 ATSes; the panel runs on
  seven. Adding hosts widens the permission it asks for, so existing installs
  would lose the panel until they grant it again. That needs a migration in
  the options page first.

## The short version

The systems that come closest to "every job" (hiring.cafe, Fantastic.jobs,
TheirStack, the larger open-source aggregators) all do the same three things:

1. **They read employers' ATS boards directly** rather than job boards. The
   board is the complete list for that company, with full descriptions and the
   real apply link. Job boards are a secondary input.
2. **They keep a very large company-to-board registry** and build it
   deliberately: Common Crawl's URL index, curated lists, and careers pages
   crawled at scale. They don't wait for links to turn up in jobs they already
   hold.
3. **They poll that registry cheaply and often.** They fetch the listing
   first, fetch descriptions only for jobs worth keeping, and run many
   requests per ATS at once.

We do (1) well for small and mid-size companies. We are weak on (1) for large
US employers, and weak on (2) and (3). The ranked list:

| # | Change | Why (evidence) | Effort |
|---|---|---|---|
| 1 | **Poll far more Workday tenants per cycle** | Workday is the #1 apply host for US new-grad postings (27%, measured) and 30–38% of big employers (reported). The SimplifyJobs data alone names 1,724 distinct Workday career sites (measured). The audit found ~1,100 registered Workday boards never polled, and we poll at most 30 a cycle. | S–M |
| 2 | **Ingest SimplifyJobs `listings.json` as a job source**, not only as a slug list | 7,575 active postings across the new-grad and internship files, 722 posted in the last 7 days, ~89% US, dated, with direct ATS apply URLs. One request per file. We currently only regex its README for company slugs. | S |
| 3 | **Add an Oracle Recruiting Cloud adapter** | 479 active new-grad and intern postings apply through Oracle (measured), with 176 distinct Oracle hosts across the files' full history. 7–10% of large US employers (reported). It is an open JSON endpoint, and we have no adapter. | M |
| 4 | **Discover boards from Common Crawl's URL index** | One aggregator found ~95,000 company identifiers this way (reported). The first 3,000 index rows for one Greenhouse host held 212 distinct boards (measured). | M |
| 5 | ~~Read LinkedIn's employer apply link~~ | Checked live: LinkedIn no longer exposes it to logged-out visitors (§6). Dropped. | – |
| 6 | **Adapters for the big-employer ATSes**: Eightfold (PCSX), Phenom (`/widgets`), SuccessFactors (`sitemal.xml`) | Together ~30% of S&P 500 careers sites (reported). Each has a documented public JSON or XML route; SuccessFactors' feed even carries full descriptions. Covers Microsoft, PayPal, Morgan Stanley, Cisco, Mastercard and others. | M each |
| 7 | **Fetch descriptions after screening, not up front**, for Greenhouse | We request `content=true` (every description) from every Greenhouse board on every poll. Other systems list first and describe later. This is likely where board-run time goes; measure first. | S |
| 8 | **Employer work-authorization data** (USCIS H-1B Employer Data Hub, DOL LCA disclosures, E-Verify) | Public, downloadable, US-specific. Shows sponsorship history per employer and doubles as a company list. SimplifyJobs' own sponsorship flag is useless (99.7% "Other", measured). *Only if sponsorship matters to you.* | M |
| 9 | **A commercial feed as a measuring stick**, not a source | Fantastic.jobs is ~$1 per 1,000 jobs from 200k+ career sites. A sample gives the denominator `IMPROVING.md` §6 says we lack. | S |
| 10 | Long-tail ATS adapters: Rippling, UKG, Dayforce, Paylocity, Taleo, Avature, Gem, Pinpoint | Each shows up in the new-grad data at 10–35 postings (measured). Add them in the order the recall probe says they cost us. | S each |

The first two alone would probably add more US jobs than everything else here.
Both reuse code we already have.

---

## 1. What we already do well (don't rebuild it)

For calibration, since several open-source projects do *less* than this:

- ~40 source adapters: keyed APIs (Adzuna, JSearch, Jooble, USAJOBS),
  public feeds, ATS boards (Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, Recruitee, Workday, iCIMS, BambooHR, Teamtailor, Jobvite,
  Personio), Google Jobs via SerpApi, and the LinkedIn guest API with
  descriptions.
- A board registry with validation, rotation (a quarter of each capped
  selection reserved for the oldest-polled boards) and retirement.
- Board discovery from job links, from careers-page sniffing, and from
  community lists.
- A browser-extension harvest for sites that challenge servers (LinkedIn,
  Indeed, Glassdoor, ZipRecruiter, WTTJ), which reads responses the page
  already fetched.
- Three-layer dedupe, liveness sweeps, and enrichment that fills descriptions
  from ATS APIs and JSON-LD.

The gaps are coverage of large-employer ATSes, the size of the registry, and
polling throughput. They are not in the architecture.

---

## 2. How the systems that get "everything" do it

**hiring.cafe** (reported). A crawler visits company career pages directly,
covering Greenhouse, Lever, Workday, Workable and dozens more ATSes, "multiple
times per day". A language model normalizes the results into one index. Third
parties describe it as 2.8M+ listings from 46 ATS platforms. It was built by
ex-Meta/DoorDash/Rippling engineers and grew to 1.3M monthly users without
marketing. The lesson: ATS-direct at scale, and an LLM for normalization rather
than for discovery.

**Fantastic.jobs** (reported). 3M+ jobs a month pulled directly from 200k+
company career sites across 54 ATS platforms, refreshed hourly, plus 11M
job-board jobs. Priced from ~$1 per 1,000 jobs.

**TheirStack** (reported). 315k+ sources including 16k+ ATS boards, deduped
across sources. Free tier of 200 API credits a month.

**Google for Jobs** is built from the `JobPosting` structured data that nearly
every board and careers site publishes. That is also why our JSON-LD reader
matters so much.

The shared pattern is **company first, board direct, poll often**. None of
them rely on keyword searches of job boards for completeness.

---

## 3. Open-source projects and what each one teaches

| Project | What it does | Lesson for us |
|---|---|---|
| [JobSpy](https://github.com/speedyapply/JobSpy) | Scrapes LinkedIn, Indeed, Glassdoor, ZipRecruiter and Google into one DataFrame. | Board mechanics (see §6). It is the most-copied reference for job boards, but board scraping caps out at ~1,000 results per search (reported). |
| [jobscraper_hourly](https://github.com/smresponsibilities/jobscraper_hourly) | Polls **up to 8,000 of 13,700 ATS boards every 20 minutes** on GitHub Actions. | Our "boards" group averaged ~4 hours a run with per-ATS caps in the hundreds (audit). Its methods: a weekly Common Crawl sweep, bulk tenant imports, careers-domain detection, slug probing, Workday sites found from `robots.txt`, a SuccessFactors sitemap, dedupe **by requisition ID, never by date**, descriptions fetched *after* screening, and boards auto-dropped after three days of failures. |
| [job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) | 1M+ positions from 20k+ companies, refreshed daily. | Found **~95,000 company identifiers** by scanning Common Crawl for 20+ ATS URL patterns. Workers per ATS: 50 for Workday; 30 for Greenhouse, Lever and iCIMS. Its company lists are in `data/*_companies.json` (datasets CC BY-NC). |
| [Job-Watch](https://github.com/Panchal-Sahil/Job-Watch) | 23 ATS families. | A catalogue of ATSes we don't read: Oracle HCM, SuccessFactors, Phenom, Eightfold, Radancy/TalentBrew, Rippling, UKG, Dayforce, Avature, Gem, JazzHR, Yello, ZohoRecruit. |
| [OpenPostings extraction guide](https://github.com/Masterjx9/OpenPostings/discussions/16) | Endpoint notes for about 20 ATSes. | Concrete request shapes for Taleo, Rippling, UKG, Dayforce, Paylocity, ADP, Pinpoint and Gem (see §4). |
| [career-ops](https://github.com/career-ops-hq/career-ops) | A widely used open-source job-search tool with 55+ provider modules. | Its PRs are a good record of which employer APIs are stable. For example, it limits Google Careers to page one because `robots.txt` disallows `page=`, and it treats Meta's rotating GraphQL `doc_id` as too brittle. |
| [SimplifyJobs](https://github.com/SimplifyJobs/New-Grad-Positions) | Curated US new-grad and intern postings with machine-readable data. | See §5. The single best free US early-career source. |
| [state-of-ats-2026](https://github.com/Kayvan-Zahiri/state-of-ats-2026) | 738 large employers with their ATS and careers host (MIT). | A ready seed list for big US employers (551 rows include the apply host). |

---

## 4. ATS coverage for US employers: where the jobs are and what we can read

### Where postings live

| Segment | Workday | Greenhouse | SuccessFactors | Oracle HCM | iCIMS | Phenom | Eightfold | Other |
|---|---|---|---|---|---|---|---|---|
| Fortune 500, 704 verified (reported, state-of-ats-2026) | 37.9% | 12.5% | 9.7% | 7.0% | 5.5% | – | listed | Avature, SmartRecruiters, Taleo, Ashby |
| S&P 500, 288 detected (reported, atsresumeai) | 30.2% | 8.0% | 12.8% | 10.1% | 9.0% | 13.5% | 5.9% | Taleo 3.1%, Avature 2.8% |
| 12k+ companies (reported) | 15.9% | 19.3% | – | – | 15.3% | – | – | Lever 16.6% |
| **Active new-grad postings, SimplifyJobs, ~89% US (measured)** | **27%** | 12% | – | **5.2%** | 4.2% | – | 0.9% | Ashby 7%, Lever 6.5%, SmartRecruiters 6.4%, other 26% |

For an early-career US search, **Workday and Oracle are the two gaps that
matter most**: Workday because we under-poll it, Oracle because we can't read
it at all.

### How each missing ATS is read

Every entry below was found in open-source code or documentation (reported).
None needs a login. Each still needs a live check on a sample tenant before we
build on it.

- **Oracle Recruiting Cloud (HCM).**
  `GET https://{pod}.fa.{region}.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&finder=findReqs;siteNumber={CX_n},limit=25,offset=N,sortBy=POSTING_DATES_DESC`.
  Pages with `offset`; the site number comes from the careers URL
  (`…/CandidateExperience/en/sites/CX_1`). Titles, locations and posting dates
  come in the listing. Oracle's own docs mark the resource "for Oracle internal
  use", but it is what every Oracle careers page calls.
- **Eightfold.** The older `/api/apply/v2/jobs` now answers 403 "Not authorized
  for PCSX" on many tenants. `GET {careers-host}/api/pcsx/search?domain={group}&start=N`
  is allowed by the sites' own `robots.txt` and needs no token. It returns 10
  per call whatever page size you ask for. Covers Microsoft, PayPal and Morgan
  Stanley.
- **Phenom.** `POST {careers-host}/widgets` with `ddoKey: "refineSearch"` lists
  jobs, and `ddoKey: "jobDetail"` reads one. No CSRF token. Page size is capped
  at 500 and results stop at 10,000. Listings carry no descriptions. Covers
  Cisco, HPE, Mastercard and eBay.
- **SuccessFactors (Recruiting Marketing / Career Site Builder).**
  `{careers-host}/sitemal.xml` (the real name, found through a typo of
  "sitemap"). It is an undocumented RSS feed of **every** job on the site
  **with full descriptions**, the same on every instance. One request replaces
  the per-job pass: 1 GET against 789 on BASF, in one project's measurement.
  Legacy portals use `{host}/career?…&resultType=XML`. The oldest
  `career*.successfactors.com` portals are JavaScript-only.
- **Taleo.** `POST https://{company}.taleo.net/careersection/rest/jobboard/searchjobs`.
- **Rippling.** `GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page=0&pageSize=100`.
- **UKG / UltiPro.** `POST https://recruiting.ultipro.com/{code}/JobBoard/{board}/JobBoardView/LoadSearchResults`.
- **Dayforce.** `POST https://jobs.dayforcehcm.com/api/geo/{company}/jobposting/search`.
- **Paylocity.** A `window.pageData` JSON blob embedded in the careers page.
- **Pinpoint.** `GET https://{company}.pinpointhq.com/postings.json`.
- **Gem.** `POST https://jobs.gem.com/api/public/graphql/batch`.
- **iCIMS "careers-home" (Jibe) sites.** `GET {host}/api/jobs` JSON. This is a
  second iCIMS shape beside the iframe listing we read today, used by larger
  employers' branded careers sites. career-ops and Job-Watch both ship a
  reader for it.

### Workday: the one to fix first

- Measured in our code: up to 40 requests per tenant (20 listing, 20 detail).
  The per-cycle cap is 30 tenants (`TOTAL_SLUG_CAPS`), discovery adds at most
  15, and the pool is 8 workers.
- Reported elsewhere: 50 workers for Workday (job-board-aggregator); extra
  career sites per tenant found from `robots.txt` (jobscraper_hourly); and page
  size must stay at 20, because asking for more returns zero rows with no
  error.
- **Suggested change.** Raise the Workday tenant cap and its concurrency
  together. Keep the detail budget per tenant, but spend it only on jobs that
  pass the title gate, as LinkedIn already does. Record per-tenant time so the
  cap can be set from data. All three knobs (`ATS_MAX_SLUGS_PER_ATS`,
  `ATS_BOARD_FETCH_WORKERS`, the per-ATS caps) are environment-only today. Per
  `CLAUDE.md` they should move to the settings page when touched.

---

## 5. US early-career sources

### SimplifyJobs `listings.json` (measured 2026-09-28)

Each repo keeps a machine-readable file at `.github/scripts/listings.json`
beside its README. The README is what we parse today, for slugs only.

| | New-Grad-Positions | Summer2026-Internships |
|---|---|---|
| Rows (all history) | 19,651 | 16,902 |
| Active and visible | 3,062 | 4,513 |
| Posted in the last 30 days / 7 days | 1,036 / 210 | 2,943 / 512 |
| With a US-looking location | ~89% | ~89% |
| File size | 13 MB | 12.7 MB |

- **Fields:** `company_name`, `title`, `locations[]`, `url` (the ATS apply
  link), `date_posted` and `date_updated` (epoch), `active`, `is_visible`,
  `category` (AI/ML/Data, Software, Hardware, Quant, Product), `degrees[]`,
  `sponsorship`, and `terms[]` for internships.
- **The `sponsorship` field is not usable:** 3,053 of 3,062 active new-grad
  rows say "Other". Don't build a sponsorship filter on it.
- **As a job source:** every active row becomes a job with a real date and a
  direct link. Descriptions come later from the existing enrichment, which
  already reads Workday, Greenhouse, Lever and Ashby by API.
- **As board discovery,** across all history: 1,724 distinct Workday sites,
  840 Greenhouse, 659 Ashby, 333 Lever, 321 iCIMS hosts, 176 Oracle hosts. That
  is several times what the README regex yields, because the README hides
  inactive rows.
- **Housekeeping.** `ReaVNaiL/New-Grad-2025` in `SLUG_HARVEST_URLS` now returns
  404, which costs a failed request and a warning every cycle. `pittcsc/…` and
  `Ouckah/…` appear to mirror the Simplify and vanshb03 lists. The setting is
  environment-only; per `CLAUDE.md` it belongs on the settings page.

### Other lists

- **jobright-ai repos** (`2026-Software-Engineer-New-Grad` and siblings).
  Hundreds of jobs a week, but only the last 7 days are kept, and links go to
  jobright.ai rather than the employer. Low value next to Simplify; the
  browser harvest of jobright.ai already covers it.
- **speedyapply** (`2026-SWE-College-Jobs`, `2026-AI-College-Jobs`). README
  tables with direct links. Keep them for slug discovery.

### Big-tech employers (reported)

- **Amazon:** `https://www.amazon.jobs/en/search.json?base_query=…&loc_query=…&offset=…&result_limit=…`
  returns `total_hits` and `jobs[]`. We only reach Amazon through the browser
  crawl today. A server adapter is cheap.
- **Microsoft, PayPal, Morgan Stanley:** Eightfold PCSX (§4).
- **Apple:** server-rendered search pages with a hydration blob.
- **Google Careers:** `robots.txt` disallows `page=`, so compliant readers take
  page one only. Our browser crawl queues pages 2+ for it; worth reconsidering.
- **Meta:** GraphQL with a rotating `doc_id`. Too brittle for the server; the
  browser harvest is the right route.

### Work-authorization data (only if sponsorship matters to you)

- **USCIS H-1B Employer Data Hub:** CSV downloads of approvals and denials by
  employer, FY2009 to FY2026 Q3. It answers "does this employer sponsor, and
  how often".
- **DOL OFLC LCA disclosure files:** every H-1B labor condition application,
  with employer, job title, SOC code, worksite and wage. That covers
  sponsorship history, a salary benchmark by employer, title and city, and a
  company list. For example, every US employer that filed for a software
  developer is a company-first discovery candidate for `IMPROVING.md` §8.
- **E-Verify:** STEM OPT requires an E-Verify employer. The public search only
  lists employers that report five or more employees.
- **How it fits:** a `companies` table (`IMPROVING.md` §3) carrying
  `h1b_filings_last_3y`, `h1b_approval_rate` and `e_verify`, shown on job
  cards. This is more reliable than reading "we sponsor" out of prose, which is
  what `eligibility.py` does today and what `SYSTEM_REVIEW.md` #8 found error
  prone.

---

## 6. Job boards: what works from a server, and where the line is

- **LinkedIn guest API.** We already use it. JobSpy reports rate limiting
  "around the 10th page with one IP", and requests are capped at `start` 1000.
  Our adapter's pacing and detail budget are already tuned around that.
  - **The employer apply link is no longer there (checked 2026-09-28).**
    JobSpy parses `<code id="applyUrl">` from the public job page, and
    OSApplyTrack reads the `?url=` inside it. Six live postings, fetched both
    through the `jobs-guest/jobs/api/jobPosting/{id}` fragment we use and as
    full `/jobs/view/{id}` pages, carried no `applyUrl`. The offsite Apply
    button now opens a sign-in modal. Logged out, the link is gone; don't build
    on it.
- **Google Jobs.** Now read through SerpApi. JobSpy scrapes Google's results
  page and its `async/callback:550` pagination directly: keyless arrays,
  fragile, and quickly blocked. SerpApi is the right trade.
- **Indeed, ZipRecruiter, Glassdoor.** JobSpy reads Indeed through Indeed's
  iOS-app GraphQL API, sending the app's key and identifying as the app, and
  says it has "no rate limiting". It reads ZipRecruiter through its mobile-app
  API and Glassdoor through GraphQL with a scraped CSRF token.

### Deliberately not recommended

Impersonating a vendor's mobile app with its embedded key, or forging
`Origin`/`Referer` to use a site's front-end search key (this session declined
to do that for WTTJ's Algolia index). These work until they don't, and they
put the deployment on the wrong side of those sites' terms.

The repo has already made that call once, for Dice's public web key. Doing the
same for Indeed would be the same decision at much larger scale and exposure.
It is yours to make explicitly, not something to slip in. The browser harvest
already reads these boards as a person browsing.

---

## 7. Architecture lessons worth copying

- **List first, describe later.** Fetch listings cheaply, run the title and
  location gates, then fetch descriptions only for survivors, or leave them to
  enrichment. We do this for LinkedIn but not for Greenhouse (`content=true` on
  every board) or Workday (details spent in listing order). Measure per-board
  bytes and time before and after.
- **Concurrency per ATS, not one pool for all.** Different ATSes tolerate
  different rates. Ours runs each ATS in turn with 8 workers.
- **Requisition ID as identity.** jobscraper_hourly never matches by date,
  because Workday exposes only relative dates and employers bump timestamps.
  This speaks to the open audit item S12 (requisition-aware dedupe).
- **Rotation for quiet boards.** Poll active boards often and quiet ones on a
  slower rotation. We reserve a quarter of each cap for the oldest-polled;
  with bigger caps, a slower tier for boards that have been empty for weeks
  would free the budget.
- **Measure recall against something outside ourselves.** A small commercial
  sample (Fantastic.jobs, TheirStack's free tier) for the user's target
  companies is an independent denominator. It complements the recall panel in
  `IMPROVING.md` §6, which measures against boards we already chose.

---

## 8. Suggested order of work

Following `IMPROVING.md`'s rule of building the thing that tells you whether
the next thing worked first:

1. **Measure where board time goes.** Per-ATS and per-board time and bytes are
   already recorded per source; add per-tenant time for Workday. This decides
   how far items 2 and 7 can go.
2. **SimplifyJobs `listings.json`:** a job source plus full-history board
   discovery. Drop the dead list, and move `SLUG_HARVEST_URLS` to the settings
   page.
3. **Workday throughput:** caps and concurrency as tunables, details only for
   jobs past the title gate, and `robots.txt` site discovery.
4. **LinkedIn `applyUrl`:** verify on one live response, then extract.
5. **Oracle Recruiting Cloud adapter**, then an **Amazon** server adapter.
6. **Common Crawl discovery** (weekly, one ATS host per run, through
   `ats_validation`) and the **state-of-ats-2026 seed** (MIT) for large
   employers.
7. **Eightfold, Phenom and SuccessFactors adapters.**
8. **Work-authorization data** into a `companies` table, if wanted.
9. **The long tail** (Rippling, UKG, Dayforce, Paylocity, Taleo, Avature, Gem,
   Pinpoint), in the order the recall probe shows they cost us.

---

## Sources

**Aggregators and feeds**
- hiring.cafe: [About](https://hiringcafe.com/about); reviews at [Dreamwork](https://www.dreamworkhq.com/blog/hiring-cafe-review), [Remote100k](https://remote100k.com/blog/is-hiringcafe-legit) and [Remote Job Assistant](https://www.remotejobassistant.com/blog/hiringcafe-review); ATS count from [Apify listing](https://apify.com/blackfalcondata/hiringcafe-scraper).
- Fantastic.jobs: [About](https://fantastic.jobs/about), [API](https://fantastic.jobs/api), [Active Jobs DB on RapidAPI](https://rapidapi.com/fantastic-jobs-fantastic-jobs-default/api/active-jobs-db).
- TheirStack: [Pricing](https://theirstack.com/en/pricing), [Job posting APIs compared](https://theirstack.com/en/blog/best-job-posting-apis).

**Open-source projects**
- JobSpy: [README](https://github.com/speedyapply/JobSpy); scraper sources for [Indeed](https://github.com/speedyapply/JobSpy/blob/main/jobspy/indeed/__init__.py), [Google](https://github.com/speedyapply/JobSpy/blob/main/jobspy/google/__init__.py), [ZipRecruiter](https://github.com/speedyapply/JobSpy/blob/main/jobspy/ziprecruiter/__init__.py), [Glassdoor](https://github.com/speedyapply/JobSpy/blob/main/jobspy/glassdoor/__init__.py) and [LinkedIn](https://github.com/speedyapply/JobSpy/blob/main/jobspy/linkedin/__init__.py); [WTTJ PR #358](https://github.com/speedyapply/JobSpy/pull/358).
- [jobscraper_hourly](https://github.com/smresponsibilities/jobscraper_hourly) · [job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) · [Job-Watch](https://github.com/Panchal-Sahil/Job-Watch) · [OpenPostings extraction guide](https://github.com/Masterjx9/OpenPostings/discussions/16).
- career-ops: [repo](https://github.com/career-ops-hq/career-ops), [PR #4077](https://github.com/career-ops-hq/career-ops/pull/4077), [PR #4078](https://github.com/career-ops-hq/career-ops/pull/4078), [PR #4297 (Eightfold PCSX)](https://github.com/career-ops-hq/career-ops/pull/4297).
- Common Crawl discovery: [jdrakes/job-search PR #14](https://github.com/jdrakes/job-search/pull/14), [startups-board](https://github.com/danki1337/startups-board), [careerscout](https://github.com/Prafull37/careerscout).
- LinkedIn apply link: [OSApplyTrack PR #234](https://github.com/CryptoJones/OSApplyTrack/pull/234), [linkedin-mcp-server issue #1031](https://github.com/stickerdaniel/linkedin-mcp-server/issues/1031).
- Eightfold: [claude-job-hunt PR #540](https://github.com/dominiquevienne/claude-job-hunt/pull/540), [ever-jobs PR #145](https://github.com/MakeDeeply/ever-jobs/pull/145), [trackeir PR #11](https://github.com/oconnesp/trackeir/pull/11).
- Workday page-size pitfall: [DEV article](https://dev.to/juancarlosguti/ask-workdays-public-api-for-100-jobs-and-you-get-zero-with-no-error-54b2).
- SuccessFactors `sitemal.xml`: [cetteup.com write-up](https://cetteup.com/158/sap-successfactors-recruiting-marketings-hidden-rss-job-feed/), [headstart issue #535](https://github.com/sarthakjain004/headstart/issues/535).
- Phenom `/widgets`: [headstart PR #501](https://github.com/sarthakjain004/headstart/pull/501).

**ATS market data**
- [state-of-ats-2026](https://github.com/Kayvan-Zahiri/state-of-ats-2026) · [atsresumeai S&P 500 index](https://www.atsresumeai.com/blog/which-ats-fortune-500-uses) · [Pin ATS market share report](https://www.pin.com/blog/ats-market-share-report/) · [jobhire.ai Fortune 500 report](https://jobhire.ai/blog/fortune-500-use-ats).
- Oracle documentation: [Recruiting CE Job Requisitions REST](https://docs.oracle.com/en/cloud/saas/human-resources/farws/api-recruiting-ce-job-requisitions.html).

**US data**
- SimplifyJobs: [New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions), [Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships). Figures measured from `.github/scripts/listings.json`.
- [jobright-ai/2026-Software-Engineer-New-Grad](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad).
- Amazon search JSON: [AmazonJobs scraper](https://github.com/Snala/AmazonJobs).
- Work authorization: [USCIS H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub), [DOL OFLC performance data](https://www.dol.gov/agencies/eta/foreign-labor/performance), [OFLC disclosure data on data.gov](https://catalog.data.gov/dataset/office-of-foreign-labor-certification-oflc-case-disclosure-data), [E-Verify and STEM OPT](https://www.e-verify.gov/faq/am-i-required-to-participate-in-e-verify-in-order-to-hire-f-1-students-who-seek-a-stem-opt).
