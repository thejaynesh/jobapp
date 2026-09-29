"""
Settings you'd change while looking at your results, editable from the UI.

The settings page used to write `profile.data["settings"]` and nothing read it,
so all three of its fields were theatre — the match score you actually changed
was the one on the *skills* tab, under a different key, and the other two were
env-only. This is the fix, and the shape is chosen so it can't happen again:
every tunable is declared once here, the UI renders that declaration, and every
consumer reads through `value()` or the `effective_settings()` overlay. A field
that isn't wired up can't exist, because the wiring is the declaration.

Not everything belongs here. API keys, session cookies and connection URLs stay
in the environment — the test is whether you'd change it to see a different set
of jobs, not whether it happens to be configurable.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.config import settings
from app.services.model_catalog import DEFAULT_NIM_MODELS

logger = logging.getLogger(__name__)

# Where the overrides live on the profile. Kept as the key the settings page
# already wrote, so values saved before any of this worked start taking effect.
STORE_KEY = "settings"


@dataclass(frozen=True)
class Tunable:
    key: str                        # form field and storage key
    env: str                        # the matching app.config attribute
    kind: str                       # int | float | bool | choice | text
    label: str
    help: str
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] = field(default_factory=list)
    # Read once at process start, so a change needs a restart to bite. Said out
    # loud in the UI rather than left for the user to discover.
    restart_required: bool = False
    # Choices that depend on runtime state rather than on this file. A model
    # role's options come from which providers have keys, so the list built at
    # import is a snapshot — validating against it would silently discard a
    # pinned model whenever that snapshot and the present disagree, which is
    # the one moment the setting most needs to survive.
    dynamic: bool = False
    # An older key at the top level of the profile that's been the live value.
    # It wins on read until the next save writes both.
    legacy_key: str | None = None
    group: str = "Matching"
    # A provider whose model list (edited under "Model lists") supplies the
    # choices, for a provider's default model. Implies `dynamic`.
    catalog: str = ""


TUNABLES: list[Tunable] = [
    Tunable(key="outcome_mode", env="OUTCOME_MODE", kind="choice", choices=["off", "shadow", "assist"], group="Intelligence", label="Outcome ordering experiment", help="Shadow reports a chronological outcome comparison. Assist adds a small ordering signal to Today only after enough mature labels and a better independent holdout; it never claims calibrated interview probabilities."),
    Tunable(key="record_decisions_enabled", env="RECORD_DECISIONS_ENABLED", kind="bool", group="Intelligence", label="Record ranking decisions and impressions", help="Store bounded observations for ranking evaluation. Deleting observations on Today also pauses collection until you enable this again. Application history remains available."),
    Tunable(key="match_evidence_mode", env="MATCH_EVIDENCE_MODE", kind="choice", choices=["shadow", "assist"], group="Intelligence", label="Evidence-assisted matching", help="Shadow keeps the existing scoring prompt and records inspectable evidence separately. Assist supplies approved achievements to both scoring passes. Compare a frozen quality benchmark before switching."),
    Tunable(key="document_bullets_per_entry", env="DOCUMENT_BULLETS_PER_ENTRY", kind="int", minimum=1, maximum=8, group="Documents", label="Achievement bullets per entry", help="Select this many approved achievements per employer or project before tailoring; prefer requirement coverage and avoid repetition. Final PDF fitting still enforces one page."),
    Tunable(key="intelligence_interval_hours", env="INTELLIGENCE_INTERVAL_HOURS", kind="int", minimum=1, maximum=168, group="Schedule", label="Intelligence maintenance every (hours)", help="Prune old observations, evaluate confirmed outcomes and refresh an enabled semantic experiment in bounded batches."),
    Tunable(key="plan_application_minutes", env="PLAN_APPLICATION_MINUTES", kind="int", minimum=3, maximum=90, group="Intelligence", label="Estimated application time (minutes)", help="Time reserved per application in Today. Adjust it to your actual pace."),
    Tunable(key="plan_followup_minutes", env="PLAN_FOLLOWUP_MINUTES", kind="int", minimum=1, maximum=60, group="Intelligence", label="Estimated follow-up time (minutes)", help="Time reserved for an existing application's next action in Today."),
    Tunable(key="match_evidence_chars", env="MATCH_EVIDENCE_CHARS", kind="int", minimum=1000, maximum=20000, group="Intelligence", label="Candidate evidence budget (characters)", help="Maximum achievement evidence sent to each matching pass. Higher includes more facts but costs more tokens."),
    Tunable(key="today_minutes", env="TODAY_MINUTES", kind="int", minimum=5, maximum=240, group="Intelligence", label="Daily plan (minutes)", help="Default time available for the Today plan. The plan stops when this budget is filled."),
    Tunable(key="today_exploration_percent", env="TODAY_EXPLORATION_PERCENT", kind="int", minimum=0, maximum=30, group="Intelligence", label="Explore overlooked opportunities (%)", help="Share of application suggestions reserved for plausible jobs outside the usual shortlist. Zero disables exploration."),
    Tunable(key="outcome_maturity_days", env="OUTCOME_MATURITY_DAYS", kind="int", minimum=7, maximum=90, group="Intelligence", label="Outcome observation window (days)", help="Compare applications only after this follow-up window. Silence remains unknown rather than a rejection."),
    Tunable(key="outcome_min_labels", env="OUTCOME_MIN_LABELS", kind="int", minimum=20, maximum=1000, group="Intelligence", label="Minimum outcome labels for an experiment", help="A learned outcome ranker stays in shadow mode until this many confirmed outcomes exist and its holdout improves."),
    Tunable(key="decision_retention_days", env="DECISION_RETENTION_DAYS", kind="int", minimum=30, maximum=1095, group="Intelligence", label="Decision and impression retention (days)", help="Old ranking observations are removed on the daily maintenance pass. Application milestones are retained until you delete the application."),
    Tunable(key="application_mail_review", env="APPLICATION_MAIL_REVIEW", kind="bool", group="Applications", label="Suggest application updates from mail", help="Read-only mailbox polling can propose milestones for review. It never changes application status without your confirmation."),
    Tunable(key="answer_expiry_days", env="ANSWER_EXPIRY_DAYS", kind="int", minimum=1, maximum=365, group="Applications", label="Remember availability answers for (days)", help="Time-sensitive saved answers expire after this interval; employer-specific answers only apply to the saved site."),
    Tunable(key="adaptive_work_enabled", env="ADAPTIVE_WORK_ENABLED", kind="bool", group="Capacity", label="Protect interactive work", help="Pause admission of background work when observed latency, queue age or host pressure is high. Interactive actions and recovery tasks continue."),
    Tunable(key="adaptive_latency_ms", env="ADAPTIVE_LATENCY_MS", kind="int", minimum=250, maximum=30000, group="Capacity", label="Page latency threshold (milliseconds)", help="A slow recent page-latency sample defers background work. Higher tolerates more contention."),
    Tunable(key="adaptive_queue_seconds", env="ADAPTIVE_QUEUE_SECONDS", kind="int", minimum=10, maximum=3600, group="Capacity", label="Interactive queue age limit (seconds)", help="Stop admitting background work when a queued user request has waited this long."),
    Tunable(key="adaptive_pressure_percent", env="ADAPTIVE_PRESSURE_PERCENT", kind="int", minimum=10, maximum=100, group="Capacity", label="Host pressure threshold (%)", help="Sustained CPU, CPU steal, or memory pressure above this value triggers reduced work. Telemetry unavailable on a host is shown as unknown."),
    Tunable(key="adaptive_cooldown_seconds", env="ADAPTIVE_COOLDOWN_SECONDS", kind="int", minimum=30, maximum=3600, group="Capacity", label="Recovery cooldown (seconds)", help="Keep background admission paused for this long after pressure is detected, avoiding rapid stop/start cycles."),
    Tunable(key="adaptive_source_enabled", env="ADAPTIVE_SOURCE_ENABLED", kind="bool", group="Capacity", label="Adapt source polling to useful yield", help="Low-yield sources are polled less often after enough observations. A periodic probe preserves coverage."),
    Tunable(key="adaptive_source_max_hours", env="ADAPTIVE_SOURCE_MAX_HOURS", kind="int", minimum=1, maximum=168, group="Capacity", label="Maximum source probe gap (hours)", help="Even a low-yield source is admitted after this long. Lower preserves freshness at greater cost."),
    Tunable(key="semantic_mode", env="SEMANTIC_MODE", kind="choice", choices=["off", "shadow", "assist"], group="Intelligence", label="Semantic retrieval experiment", help="Off makes no embedding calls. Shadow records comparison results; assist adds cached semantic relevance to Today suggestions without hiding jobs."),
    Tunable(key="semantic_model", env="SEMANTIC_MODEL", kind="text", group="Intelligence", label="Embedding model", help="Model served by the configured compatible embedding endpoint. Changing it invalidates cached vectors; credentials stay in the environment."),
    Tunable(key="semantic_batch_size", env="SEMANTIC_BATCH_SIZE", kind="int", minimum=1, maximum=100, group="Intelligence", label="New embeddings per maintenance pass", help="Bounds remote work per pass; only changed active postings are embedded."),
    Tunable(key="semantic_active_days", env="SEMANTIC_ACTIVE_DAYS", kind="int", minimum=1, maximum=90, group="Intelligence", label="Semantic active window (days)", help="Embed recent candidate postings and favourites rather than the historical corpus."),
    Tunable(
        key="min_match_score", env="MIN_MATCH_SCORE", kind="int",
        minimum=0, maximum=100, legacy_key="min_match_score",
        label="Minimum match score",
        help="Jobs the model scores below this are filtered out. Also editable "
             "on the profile's skills tab — the two stay in sync.",
    ),
    Tunable(
        key="prescreen_min_similarity", env="PRESCREEN_MIN_SIMILARITY", kind="int",
        minimum=0, maximum=100,
        label="Similarity pre-screen (0 = off)",
        help="Jobs whose text reads less like your profile than this (0-100) are "
             "filtered before the model is asked to score them, saving that call. "
             "Off at 0. The matching report shows, on your own decisions, how many "
             "calls each value would save and how many jobs you applied to it "
             "would have dropped: set it from there, not by guess.",
    ),
    Tunable(
        key="match_report_interval_hours", env="MATCH_REPORT_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=720, group="Schedule",
        label="Matching report to the log every (hours)",
        help="How often a one-line summary of the matching report (agreement with "
             "your decisions, and any better minimum score) is written to the log. "
             "The report page itself is always current.",
    ),
    Tunable(
        key="followup_after_days", env="FOLLOWUP_AFTER_DAYS", kind="int",
        minimum=1, maximum=60, group="Applications",
        label="Follow up after (days)",
        help="When an application is marked applied, its next action is a follow-up "
             "due this many days later, unless you set your own. Shorter nags sooner; "
             "longer leaves more time for a reply before you are reminded.",
    ),
    Tunable(
        key="reminder_interval_hours", env="REMINDER_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Schedule",
        label="Application reminders every (hours)",
        help="How often the next actions due today or earlier are written to the log "
             "as one reminder, which the Log badge counts.",
    ),
    Tunable(
        key="for_you_retrain_hours", env="FOR_YOU_RETRAIN_HOURS", kind="int",
        minimum=1, maximum=720, group="Schedule",
        label="For you ranking: retrain every (hours)",
        help="How often the ranking learned from your applications and dismissals "
             "is retrained. It takes under a second; more often only matters when "
             "you are deciding on many jobs a day.",
    ),
    Tunable(
        key="min_keyword_skills", env="MIN_KEYWORD_SKILLS", kind="int",
        minimum=0, maximum=20,
        label="Minimum skill matches",
        help="How many of your skills must appear in a description before it's "
             "worth an LLM call. Raise it to spend fewer calls, lower it if "
             "good jobs are being filtered as \"too few skills\".",
    ),
    Tunable(
        key="nvidia_nim_model", env="NVIDIA_NIM_MODEL", kind="choice",
        # The NIM list on this page, editable under "Model lists" — see
        # `model_catalog`. Dynamic so a model added there is accepted here.
        choices=list(DEFAULT_NIM_MODELS),
        dynamic=True,
        label="Matching model",
        help="Which NIM model scores your jobs. Add newly released models under "
             "\"Model lists\" below; compare candidates on the runs page first — "
             "the count that matters there is unreadable replies.",
    ),
    Tunable(
        key="match_concurrency", env="MATCH_CONCURRENCY", kind="int",
        minimum=1, maximum=16, group="Models",
        label="Jobs scored at once",
        help="How many jobs have their model calls in flight together. Calls "
             "still start no faster than the provider allows (NIM: 40 a "
             "minute), so what changes is that one slow reply no longer holds "
             "up the jobs behind it. 1 scores one job at a time, as before.",
    ),
    Tunable(
        key="max_job_age_days", env="MAX_JOB_AGE_DAYS", kind="int",
        minimum=0, maximum=365, group="Filtering",
        label="Maximum job age (days)",
        help="Postings older than this are dropped at fetch time. 0 disables "
             "the check. Only applies to jobs whose source reports a posting "
             "date, and only to new fetches — it won't clear what's stored.",
    ),
    Tunable(
        key="h1b_history_enabled", env="H1B_HISTORY_ENABLED", kind="bool",
        group="Filtering", label="Show H-1B filing history per employer",
        help="Each job shows how many H-1B labor condition applications its "
             "employer had certified lately, from the Department of Labor's "
             "public disclosure files, and how many were for computer "
             "occupations. If your screening answers say you will need "
             "sponsorship, an employer with none on file under its name is "
             "marked too. Shown, never filtered or scored. Off stops the "
             "daily check for new DOL files and hides it.",
    ),
    Tunable(
        key="h1b_history_quarters", env="H1B_HISTORY_QUARTERS", kind="int",
        minimum=1, maximum=12, group="Filtering",
        label="H-1B history: fiscal quarters counted",
        help="How many of the latest federal fiscal quarters the counts cover. "
             "4 is a year, which takes in the March filing season whenever it "
             "falls; 1 is only the newest quarter, which can miss it. More "
             "quarters means an older quarter is downloaded the first time.",
    ),
    Tunable(
        key="dashboard_max_age_days", env="DASHBOARD_MAX_AGE_DAYS", kind="int",
        minimum=0, maximum=3650, group="Filtering",
        label="Hide jobs older than (days)",
        help="How far back the jobs list looks, counted from when the job was "
             "fetched — not from the posting date, which most sources don't "
             "report. Nothing is deleted, and anything you applied to or "
             "starred stays visible however old it is. The list says how many "
             "it's hiding and links to show them. 0 shows everything.",
    ),
    Tunable(
        key="filter_senior_titles", env="FILTER_SENIOR_TITLES", kind="bool",
        group="Filtering",
        label="Skip senior-titled jobs while junior",
        help="Drops Senior/Staff/Principal/Lead titles before they cost an LLM "
             "call, unless the word appears in one of your target roles. Only "
             "active below the junior threshold below.",
    ),
    Tunable(
        key="filter_by_language", env="FILTER_BY_LANGUAGE", kind="bool",
        group="Filtering",
        label="Skip postings not in your languages",
        help="Arbeitnow and friends return German listings under English "
             "titles, so the title gate passes them and a model is then asked "
             "to score a description you could not act on. A posting whose "
             "language could not be read is always kept. Which languages count "
             "is Languages you read, below.",
    ),
    Tunable(
        key="junior_max_years", env="JUNIOR_MAX_YEARS", kind="float",
        minimum=0, maximum=30, group="Filtering",
        label="Junior threshold (years)",
        help="Below this much total experience you count as junior. Your total "
             "is worked out from the dates on your experience entries — see the "
             "profile's AI prompt tab for what it came to.",
    ),
    Tunable(
        key="linkedin_recency_hours", env="LINKEDIN_RECENCY_HOURS", kind="int",
        minimum=0, maximum=2160, group="LinkedIn",
        label="LinkedIn recency window (hours)",
        help="Asks LinkedIn for postings newer than this. 0 disables it. It's a "
             "hint to their ranker rather than a guarantee, so the age filter "
             "above is what actually enforces freshness.",
    ),
    Tunable(
        key="linkedin_max_pages", env="LINKEDIN_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="LinkedIn",
        label="LinkedIn pages per search",
        help="10 results a page. Deeper pages return looser matches and more "
             "undated postings, so more isn't always better.",
    ),
    Tunable(
        key="dice_enabled", env="DICE_ENABLED", kind="bool", group="Sources",
        label="Dice",
        help="Dice through its public search API (the browser scrape only if "
             "the API refuses). Off skips it entirely.",
    ),
    Tunable(
        key="wellfound_enabled", env="WELLFOUND_ENABLED", kind="bool",
        group="Sources", label="Wellfound",
        help="Startup jobs from Wellfound's role pages, read over plain HTTP "
             "with employer, pay and full descriptions. Off skips it.",
    ),
    Tunable(
        key="wellfound_roles", env="WELLFOUND_ROLES", kind="text",
        group="Sources", label="Wellfound: role pages",
        help="Comma-separated Wellfound role slugs, as in wellfound.com/role/"
             "<slug> — e.g. software-engineer, data-engineer. Each is one "
             "page of about fifty listings per run.",
    ),
    Tunable(
        key="builtin_enabled", env="BUILTIN_ENABLED", kind="bool",
        group="Sources", label="Built In",
        help="US tech jobs from Built In's public search, with the card's "
             "posting age, pay band and seniority. No key. Off skips it.",
    ),
    Tunable(
        key="builtin_max_pages", env="BUILTIN_MAX_PAGES", kind="int",
        minimum=1, maximum=10, group="Sources",
        label="Built In: pages per search",
        help="25 postings a page, read for every role twice — its main search "
             "and its remote one. Paging stops early at a page with nothing "
             "new. 1 is the first page only; higher reaches older postings.",
    ),
    Tunable(
        key="jsearch_date_posted", env="JSEARCH_DATE_POSTED", kind="choice",
        choices=["today", "3days", "week", "month", "all"], group="Sources",
        label="JSearch: posted within",
        help="How far back each JSearch search reaches. \"today\" misses "
             "anything from a day the fetch did not run; wider windows return "
             "more repeats, which dedupe merges.",
    ),
    Tunable(
        key="jsearch_num_pages", env="JSEARCH_NUM_PAGES", kind="int",
        minimum=1, maximum=5, group="Sources",
        label="JSearch: pages per search",
        help="Each page is one call against a small monthly quota, for every "
             "role and location you search — so 2 doubles the spend.",
    ),
    Tunable(
        key="simplify_enabled", env="SIMPLIFY_ENABLED", kind="bool",
        group="Sources", label="SimplifyJobs lists",
        help="Curated US new-grad postings from SimplifyJobs' GitHub list — "
             "dated, with the employer's own apply link. About two hundred new "
             "ones a week. Off skips it.",
    ),
    Tunable(
        key="simplify_listings_urls", env="SIMPLIFY_LISTINGS_URLS", kind="text",
        group="Sources", label="SimplifyJobs: listing files",
        help="Comma-separated listings.json URLs. New-grad by default; add "
             "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/"
             "dev/.github/scripts/listings.json for internships.",
    ),
    Tunable(
        key="amazon_enabled", env="AMAZON_ENABLED", kind="bool",
        group="Sources", label="Amazon Jobs",
        help="Amazon's own careers search, read by the server with full "
             "descriptions — independent of the browser crawl. Searched per "
             "role in your countries (the US when none). Off skips it.",
    ),
    Tunable(
        key="amazon_max_pages", env="AMAZON_MAX_PAGES", kind="int",
        minimum=1, maximum=10, group="Sources",
        label="Amazon Jobs: pages per search",
        help="100 postings a page, newest first, for every role. Amazon posts "
             "hundreds of matching roles, so 2 reaches the last week or two.",
    ),
    Tunable(
        key="tiktok_enabled", env="TIKTOK_ENABLED", kind="bool",
        group="Sources", label="TikTok Careers",
        help="TikTok's own careers search, read by the server with full "
             "descriptions — one of the largest employers on the new-grad "
             "lists. Searched per role in your countries (the US when none); "
             "a country with no TikTok office costs one request. Off skips it.",
    ),
    Tunable(
        key="tiktok_max_pages", env="TIKTOK_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="Sources",
        label="TikTok Careers: pages per search",
        help="100 postings a page for every role. TikTok has no newest-first "
             "order, so a search cut short misses postings at random rather "
             "than the oldest ones. About 1,800 US roles in all, 441 for "
             "\"software engineer\": 5 reads every role search to its end.",
    ),
    Tunable(
        key="apple_enabled", env="APPLE_ENABLED", kind="bool",
        group="Sources", label="Apple Jobs",
        help="Apple's own careers search, read by the server. Searched per "
             "role in your countries (the US when none; Apple is searched in "
             "the US, Canada, UK, Germany, Ireland, Singapore and Poland). "
             "Off skips it.",
    ),
    Tunable(
        key="apple_max_pages", env="APPLE_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="Sources",
        label="Apple Jobs: pages per search",
        help="20 postings a page, newest first. Apple lists thousands of US "
             "roles, so 3 reaches back a few days for each role; raise it if "
             "fetches run less than daily.",
    ),
    Tunable(
        key="apple_max_details", env="APPLE_MAX_DETAILS", kind="int",
        minimum=0, maximum=100, group="Sources",
        label="Apple Jobs: full descriptions per search",
        help="Apple's search shows only a summary, mostly its standard "
             "introduction. This many postings per search — the titles "
             "matching your roles first — get one more request for the full "
             "description and qualifications; one already read is not read "
             "again. The rest are filled in by the enrichment pass. 0 leaves "
             "them all to it.",
    ),
    Tunable(
        key="jazzhr_enabled", env="JAZZHR_ENABLED", kind="bool",
        group="Company boards", label="JazzHR",
        help="Small and mid-sized US employers on *.applytojob.com. JazzHR "
             "publishes an index of every open posting (about 90,000), so no "
             "company list is needed: the companies with a new posting whose "
             "title matches your roles are read, one request each. Read on "
             "board cycles. Off skips it.",
    ),
    Tunable(
        key="jazzhr_max_companies", env="JAZZHR_MAX_COMPANIES", kind="int",
        minimum=0, maximum=2000, group="Company boards",
        label="JazzHR: companies per cycle",
        help="Companies read per board cycle, those with the most new matching "
             "postings first. A posting already read is not read again for a "
             "week, so after the first few cycles only new ones are pursued. "
             "About 740 companies match typical engineering roles at once.",
    ),
    Tunable(
        key="recall_window_days", env="RECALL_WINDOW_DAYS", kind="int",
        minimum=3, maximum=120, group="Company boards",
        label="Coverage check: days looked back over",
        help="Each day the runs page measures how many of the open postings "
             "SimplifyJobs listed in this many days another of our sources "
             "found too, and what each source found that no other did. "
             "Shorter follows a reader that just broke more closely; longer "
             "evens out quiet weeks.",
    ),
    Tunable(
        key="avature_max_details", env="AVATURE_MAX_DETAILS", kind="int",
        minimum=0, maximum=500, group="Company boards",
        label="Avature: posting pages per portal per cycle",
        help="Avature portals (Bloomberg, Koch, Harman, TotalEnergies…) list "
             "every open posting in a sitemap, with its title. New postings "
             "whose titles match your roles are read, newest first, up to this "
             "many per portal each cycle; one already stored is not read "
             "again, so the rest arrive over the next cycles. 0 reads none, "
             "which leaves the portals doing nothing but closing postings "
             "that disappear.",
    ),
    Tunable(
        key="ats_sniff_career_sites", env="ATS_SNIFF_CAREER_SITES", kind="bool",
        group="Company boards", label="Look behind employer careers sites",
        help="Many postings live on an employer's own site that wraps "
             "Greenhouse, Lever or Ashby (stripe.com/jobs?gh_jid=…). Reads the "
             "posting and the careers page for the board behind it, and asks "
             "Greenhouse to confirm a guessed board holds the posting. Each "
             "site is looked at once; a miss is retried after 30 days.",
    ),
    Tunable(
        key="ats_sniff_max_hosts_per_cycle", env="ATS_SNIFF_MAX_HOSTS_PER_CYCLE",
        kind="int", minimum=0, maximum=1000, group="Company boards",
        label="Careers sites looked behind per cycle",
        help="New employer sites examined per fetch cycle, each a few "
             "requests. The lists alone name about 260 that wrap Greenhouse; "
             "40 works through them in a day of board cycles. 0 pauses it.",
    ),
    Tunable(
        key="slug_harvest_urls", env="SLUG_HARVEST_URLS", kind="text",
        group="Company boards", label="Community lists to mine for boards",
        help="Comma-separated URLs of job lists whose links name company "
             "boards (Greenhouse, Workday, Oracle…). A .json URL is read as a "
             "SimplifyJobs listings file, including rows it no longer shows; "
             "an <ats>_companies.json file, or any URL written ats=URL, as a "
             "JSON list of that ATS's board names. Every board found is probed "
             "before it is polled. Read on board cycles only.",
    ),
    Tunable(
        key="workday_site_discovery", env="WORKDAY_SITE_DISCOVERY", kind="bool",
        group="Company boards", label="Find every Workday site a company runs",
        help="A Workday company often keeps new-grad and internship roles on "
             "their own site (Salesforce's Futureforce_NewGradRoles) that no "
             "posting ever links to. Reads each company's robots.txt once a "
             "month and registers every site it lists.",
    ),
    Tunable(
        key="commoncrawl_enabled", env="COMMONCRAWL_ENABLED", kind="bool",
        group="Company boards", label="Find boards in the Common Crawl index",
        help="Walks Common Crawl's public URL index for Greenhouse, Ashby, "
             "Workday, Oracle and other ATS hosts, and registers every board it "
             "lists — companies no posting of ours has ever linked to. Each is "
             "probed before it is polled.",
    ),
    Tunable(
        key="commoncrawl_pages_per_run", env="COMMONCRAWL_PAGES_PER_RUN", kind="int",
        minimum=1, maximum=500, group="Company boards",
        label="Common Crawl: index pages per walk",
        help="About 15,000 URLs a page, one request each, with a pause between. "
             "A full walk of every ATS host is a few hundred pages; each walk "
             "resumes where the last one stopped.",
    ),
    Tunable(
        key="commoncrawl_interval_hours", env="COMMONCRAWL_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=720, group="Company boards",
        label="Common Crawl: walk every (hours)",
        help="Checked hourly. The index changes monthly, so once a walk has "
             "finished the crawl, later ones cost one request until a new crawl "
             "is published.",
    ),
    Tunable(
        key="workday_max_tenants", env="WORKDAY_MAX_TENANTS", kind="int",
        minimum=0, maximum=2000, group="Company boards",
        label="Workday companies per cycle",
        help="Workday hosts about a quarter of US new-grad postings and most "
             "large employers. Each company costs up to ~40 requests. Higher "
             "reaches more companies per boards run and makes the run longer; "
             "a quarter of the slots always go to the least recently polled.",
    ),
    Tunable(
        key="ats_max_slugs_per_ats", env="ATS_MAX_SLUGS_PER_ATS", kind="int",
        minimum=10, maximum=5000, group="Company boards",
        label="Boards per ATS per cycle",
        help="How many Greenhouse, Lever, Ashby… boards a boards run polls for "
             "each ATS (Workday has its own setting above). Most cost one "
             "request each. Higher reaches the registry's long tail sooner.",
    ),
    Tunable(
        key="ats_board_fetch_workers", env="ATS_BOARD_FETCH_WORKERS", kind="int",
        minimum=1, maximum=64, group="Company boards",
        label="Boards fetched at once",
        help="Concurrent requests per ATS during a boards run. Each ATS is its "
             "own host, so this is politeness per host, not load on this "
             "server. Lower if an ATS starts answering 429.",
    ),
    Tunable(
        key="fetch_source_concurrency", env="FETCH_SOURCE_CONCURRENCY", kind="int",
        minimum=1, maximum=16, group="Company boards",
        label="Sources read at once",
        help="How many sources a fetch cycle reads side by side: Greenhouse, "
             "Workday, JazzHR and the rest each read their own sites, so none "
             "waits on another and a cycle takes about as long as its slowest "
             "source rather than all of them added up. Each still keeps its own "
             "per-site limits. 1 reads them one after another, as before.",
    ),
    Tunable(
        key="ats_board_validate_per_cycle", env="ATS_BOARD_VALIDATE_PER_CYCLE",
        kind="int", minimum=0, maximum=5000, group="Company boards",
        label="New boards checked per cycle",
        help="Newly found boards are probed once before they are polled. Each "
             "probe is one small request. Higher gets thousands of list-found "
             "boards polling within days rather than weeks; 0 stops probing, "
             "and unprobed boards are never polled.",
    ),
    Tunable(
        key="workday_rate_limit_cooldown", env="WORKDAY_RATE_LIMIT_COOLDOWN",
        kind="int", minimum=0, maximum=300, group="Company boards",
        label="Workday rest after a rate limit (seconds)",
        help="Most Workday tenants share a few servers (wd1, wd5…), so a 429 "
             "to one is a warning for all of them. The server then rests this "
             "long, or as long as it asks, while the others carry on; one that "
             "keeps refusing is left until the next cycle. 0 ignores rate "
             "limits and moves on, as before.",
    ),
    Tunable(
        key="greenhouse_descriptions_on_demand", env="GREENHOUSE_DESCRIPTIONS_ON_DEMAND",
        kind="bool", group="Company boards", label="Greenhouse descriptions on demand",
        help="Read each Greenhouse board without its posting text, and fetch "
             "the text only for postings not already stored with it. Every "
             "posting still arrives whole; this stops downloading the same "
             "text every cycle, which is twelve times the bytes of the list "
             "(99 MB against 8 MB across 35 boards). Off reads every board "
             "with its text, every time.",
    ),
    Tunable(
        key="yc_discovery_enabled", env="YC_DISCOVERY_ENABLED", kind="bool",
        group="Company boards", label="Look behind the sites of hiring YC companies",
        help="Y Combinator's directory lists about 1,500 companies as hiring, "
             "each with its website. Most run a Greenhouse, Lever or Ashby "
             "board no posting of ours has linked to; each website is looked "
             "behind once for it, on the hourly tick, and the list is re-read "
             "weekly.",
    ),
    Tunable(
        key="yc_discovery_per_hour", env="YC_DISCOVERY_PER_HOUR", kind="int",
        minimum=0, maximum=500, group="Company boards",
        label="YC sites looked behind per hour",
        help="A few requests each. 40 works through the list in about a day and "
             "a half; 0 pauses it.",
    ),
    Tunable(
        key="ats_board_validate_hourly", env="ATS_BOARD_VALIDATE_HOURLY",
        kind="int", minimum=0, maximum=5000, group="Company boards",
        label="New boards checked per hour",
        help="The same probe, on the hourly discovery tick rather than only on "
             "board cycles. The community lists name about 50,000 boards; at "
             "400 a board cycle they would take weeks to start polling, and "
             "300 an hour takes about a week. 0 leaves probing to board "
             "cycles.",
    ),
    Tunable(
        key="google_jobs_enabled", env="GOOGLE_JOBS_ENABLED", kind="bool",
        group="Sources", label="Google Jobs",
        help="Google's job results through SerpApi — postings from boards and "
             "careers sites nothing else here reads. Needs SERPAPI_API_KEY in "
             "the environment; without one this does nothing. Off skips it.",
    ),
    Tunable(
        key="google_jobs_max_searches", env="GOOGLE_JOBS_MAX_SEARCHES", kind="int",
        minimum=1, maximum=200, group="Sources",
        label="Google Jobs: searches per run",
        help="Every page of every role/location search is one search of your "
             "SerpApi quota (250 a month on the free plan). First pages for all "
             "searches go before any second page. Times runs a month, this is "
             "your spend — 8 once a day is about 240.",
    ),
    Tunable(
        key="google_jobs_pages", env="GOOGLE_JOBS_PAGES", kind="int",
        minimum=1, maximum=5, group="Sources",
        label="Google Jobs: pages per search",
        help="10 results a page. Deeper pages only run once every search has had "
             "its first, and all count against the searches-per-run cap above.",
    ),
    Tunable(
        key="google_jobs_interval_hours", env="GOOGLE_JOBS_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Sources",
        label="Google Jobs: at most every (hours)",
        help="The API sources run every few hours; Google Jobs sits out the runs "
             "inside this gap so the quota lasts the month. A source picked by "
             "hand on the Runs page ignores it. 1 runs with every API run.",
    ),
    Tunable(
        key="browse_parallel_sites", env="BROWSE_PARALLEL_SITES", kind="int",
        minimum=1, maximum=4, group="Browser agent",
        label="Sites at once",
        help="How many different sites the extension works on side by side. "
             "Each site still gets one page at a time with its usual pause, so "
             "raising this crawls more boards per hour without visiting any one "
             "board faster. 1 is one window at a time; each extra site is one "
             "more minimized window.",
    ),
    Tunable(
        key="browse_paused_hosts", env="BROWSE_PAUSED_HOSTS", kind="text",
        group="Browser agent", label="Paused sites",
        help="Sites the browser extension must not open pages on, comma-"
             "separated (e.g. linkedin.com, indeed.com). For a site that has "
             "warned you about the volume: nothing is queued there until it "
             "is removed. Takes effect within a minute.",
    ),
    # The three scheduled fetch groups. Beat ticks every few minutes and each
    # group checks its interval against its own last run, so a change here
    # takes effect on the next tick — no restart. The single "fetch interval"
    # this replaced was read by nothing: the schedule had moved to these three
    # environment variables and the setting on this page stayed behind.
    Tunable(
        key="fetch_api_interval_hours", env="FETCH_API_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Schedule",
        label="API sources: every (hours)",
        help="Keyed APIs and public feeds (Adzuna, LinkedIn guest, RemoteOK…). "
             "Minutes per run. Lower catches postings sooner and spends more of "
             "each provider's quota.",
    ),
    Tunable(
        key="fetch_boards_interval_hours", env="FETCH_BOARDS_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Schedule",
        label="Company boards: every (hours)",
        help="Greenhouse, Lever, Workday and the rest of the board registry. "
             "A run can take hours; one that is still going when the next is "
             "due is skipped rather than doubled.",
    ),
    Tunable(
        key="fetch_browser_interval_hours", env="FETCH_BROWSER_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Schedule",
        label="Browser tier: every (hours)",
        help="The Playwright scrapers — the most expensive tier and the least "
             "productive per minute, so the least often.",
    ),
    # Backups. The directory is infrastructure and stays in the environment;
    # how often and how many are preferences.
    Tunable(
        key="backup_enabled", env="BACKUP_ENABLED", kind="bool", group="Backups",
        label="Nightly database backups",
        help="A compressed pg_dump on the server's storage volume. Off means "
             "nothing can recover the database if it is lost. The Back up now "
             "button on the Runs page works either way.",
    ),
    Tunable(
        key="backup_interval_hours", env="BACKUP_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Backups",
        label="Back up every (hours)",
        help="Checked hourly, so a change takes effect within the hour. Lower "
             "loses less on a bad day and writes a full dump each time.",
    ),
    Tunable(
        key="backup_keep", env="BACKUP_KEEP", kind="int",
        minimum=1, maximum=90, group="Backups",
        label="Backups to keep",
        help="Older ones are deleted after each verified new backup — never "
             "before. Each is a full compressed copy, so this times the newest "
             "one's size is the disk it takes.",
    ),
]

def _model_role_tunables() -> list[Tunable]:
    """
    One selector per LLM role, generated from `model_roles`.

    Generated rather than written out, because the list of roles belongs to
    that module and two copies would drift — the symptom being a role the code
    uses that the settings page cannot show, which is the state this replaced.
    """
    from app.services.model_roles import ROLES, choices, tunable_key

    return [
        Tunable(
            key=tunable_key(role.key),
            # No environment variable behind these. The default is "auto",
            # which is a decision about how to decide rather than a value, and
            # it lives in `model_roles.resolve` where the deciding happens.
            env="",
            kind="choice",
            choices=choices(role.key),
            dynamic=True,
            label=role.label,
            help=role.help,
            group="Models",
        )
        for role in ROLES
    ]


TUNABLES.extend(_model_role_tunables())
TUNABLES.extend([
    Tunable(
        key="compare_timeout_seconds", env="COMPARE_TIMEOUT_SECONDS", kind="int",
        minimum=15, maximum=900, group="Models",
        label="Model comparison: time limit per call (seconds)",
        help="How long one scoring call in a comparison on the Runs page may "
             "take. Tried once, with no silent retries. Reasoning models can "
             "need a few minutes; lower gives up on slow ones sooner.",
    ),
    Tunable(
        key="compare_give_up_after", env="COMPARE_GIVE_UP_AFTER", kind="int",
        minimum=0, maximum=50, group="Models",
        label="Model comparison: drop a model after this many failures in a row",
        help="A model that times out or errors this many times running is "
             "skipped for the rest of the comparison, and the table says so. "
             "0 never gives up — every job is tried, however long it takes.",
    ),
])

# ---------------------------------------------------------------------------
# Everything below was environment-only until the settings page could reach
# it. Grouped by what it changes; each group's intervals sit with it rather
# than under Schedule, because that is where somebody looking for them goes.
# ---------------------------------------------------------------------------
TUNABLES.extend([
    # -- Matching -----------------------------------------------------------
    Tunable(
        key="deep_match_enabled", env="DEEP_MATCH_ENABLED", kind="bool",
        label="Second opinion on close calls",
        help="Jobs scored inside the band below are scored again by the "
             "strongest configured model, because that is where accept and "
             "reject flip. Skipped anyway when nothing stronger than the first "
             "model is configured. Off keeps every first score.",
    ),
    Tunable(
        key="deep_match_band_low", env="DEEP_MATCH_BAND_LOW", kind="int",
        minimum=0, maximum=100,
        label="Second opinion: from score",
        help="The bottom of the close-call band. Lower sends more jobs for a "
             "second opinion and spends more calls; at the top of the band "
             "nothing is re-scored.",
    ),
    Tunable(
        key="deep_match_band_high", env="DEEP_MATCH_BAND_HIGH", kind="int",
        minimum=0, maximum=100,
        label="Second opinion: up to score",
        help="The top of the close-call band. A score above it is taken as it "
             "is. Set it at or below the bottom and no job gets a second "
             "opinion.",
    ),
    Tunable(
        key="deep_match_max_per_cycle", env="DEEP_MATCH_MAX_PER_CYCLE", kind="int",
        minimum=0, maximum=5000,
        label="Second opinions per matching cycle",
        help="A ceiling on second-opinion calls, shared by every batch of the "
             "cycle. Past it, jobs keep their first score. 0 means no ceiling.",
    ),
    Tunable(
        key="match_max_jobs_per_task", env="MATCH_MAX_JOBS_PER_TASK", kind="int",
        minimum=1, maximum=500,
        label="Jobs per matching batch",
        help="A matching pass works in batches that queue the next one, so a "
             "restart loses at most one batch and document generation is not "
             "stuck behind a long pass. Higher holds a worker longer; 1 "
             "re-queues after every job.",
    ),
    Tunable(
        key="match_description_chars", env="MATCH_DESCRIPTION_CHARS", kind="int",
        minimum=2000, maximum=100000,
        label="Description sent for scoring (characters)",
        help="How much of a posting the scoring prompt carries. Far longer "
             "than a real posting by default, as a guard against a page that "
             "cleaned badly. Too low and the model judges the job on its "
             "marketing paragraphs rather than its requirements.",
    ),
    # -- Filtering ----------------------------------------------------------
    Tunable(
        key="match_languages", env="MATCH_LANGUAGES", kind="text", group="Filtering",
        label="Languages you read",
        help="Two-letter language codes, comma-separated (en, de, fr). With "
             "the language filter on, a posting written in any other language "
             "is set aside before it costs a model call.",
    ),
    # -- Models -------------------------------------------------------------
    Tunable(
        key="match_primary", env="MATCH_PRIMARY", kind="choice", group="Models",
        choices=["nim", "freeinference", "gemini", "anthropic"],
        label="Provider that scores first",
        help="Which provider a job is scored by before any failover. Another "
             "provider moves NIM to the end of the chain rather than removing "
             "it; a provider with no key falls back to NIM. FreeInference here "
             "spends the free daily credit document writing prefers.",
    ),
    Tunable(
        key="nvidia_nim_rpm", env="NVIDIA_NIM_RPM", kind="int",
        minimum=1, maximum=1000, group="Models",
        label="NIM: requests per minute allowed",
        help="Your NIM account limit. Matching paces its calls to it; set it "
             "above the real limit and calls start failing with 429s, below "
             "it and matching is slower than it needs to be.",
    ),
    Tunable(
        key="nim_match_max_tokens", env="NIM_MATCH_MAX_TOKENS", kind="int",
        minimum=256, maximum=16000, group="Models",
        label="Scoring reply limit (tokens)",
        help="The ceiling on a scoring reply. Reasoning models think before "
             "they answer, so too low cuts the answer off and the score is "
             "lost. Costs nothing unused: only tokens produced are generated.",
    ),
    Tunable(
        key="max_paid_match_calls_per_cycle", env="MAX_PAID_MATCH_CALLS_PER_CYCLE",
        kind="int", minimum=0, maximum=10000, group="Models",
        label="Paid scoring calls per cycle",
        help="A ceiling on scoring calls to providers that bill, for when the "
             "free ones are down. Past it the remaining jobs stay new and are "
             "tried next cycle. 0 means no ceiling.",
    ),
    Tunable(
        key="freeinference_max_concurrency", env="FREEINFERENCE_MAX_CONCURRENCY",
        kind="int", minimum=0, maximum=16, group="Models",
        label="FreeInference: calls at once",
        help="The endpoint accepts one request at a time, so calls queue for "
             "it. 0 lets every caller through at once — only if that limit is "
             "ever lifted.",
    ),
    Tunable(
        key="freeinference_model", env="FREEINFERENCE_MODEL", kind="choice",
        dynamic=True, catalog="freeinference", group="Models",
        label="FreeInference: writing model",
        help="What FreeInference writes documents with when a model role is "
             "on auto. The list is edited under Model lists below.",
    ),
    Tunable(
        key="freeinference_match_model", env="FREEINFERENCE_MATCH_MODEL", kind="choice",
        dynamic=True, catalog="freeinference", group="Models",
        label="FreeInference: scoring model",
        help="What FreeInference scores jobs with — the faster sibling, since "
             "scoring is high-volume JSON.",
    ),
    Tunable(
        key="anthropic_model", env="ANTHROPIC_MODEL", kind="choice",
        dynamic=True, catalog="anthropic", group="Models",
        label="Anthropic: writing model",
        help="What Anthropic writes documents with when a model role is on "
             "auto. The strongest costs the most per application.",
    ),
    Tunable(
        key="anthropic_match_model", env="ANTHROPIC_MATCH_MODEL", kind="choice",
        dynamic=True, catalog="anthropic", group="Models",
        label="Anthropic: scoring model",
        help="What Anthropic scores jobs with when it is in the failover "
             "chain. Cheap by design: scoring is by far the higher volume.",
    ),
    Tunable(
        key="gemini_model", env="GEMINI_MODEL", kind="choice",
        dynamic=True, catalog="gemini", group="Models",
        label="Gemini: model",
        help="What Gemini is called with, for writing and for scoring.",
    ),
    # -- Documents ----------------------------------------------------------
    Tunable(
        key="self_review_enabled", env="SELF_REVIEW_ENABLED", kind="bool",
        group="Documents",
        label="Read each draft back before compiling",
        help="The model reads a draft resume or letter as the recruiter would "
             "and fixes what it finds. One more call per document; off sends "
             "the first draft.",
    ),
    Tunable(
        key="answer_draft_words", env="ANSWER_DRAFT_WORDS", kind="int",
        minimum=30, maximum=600, group="Documents",
        label="Drafted answers: length (words)",
        help="What the extension aims for when it drafts an answer to a long "
             "question on an application form. A form that states its own "
             "character limit always wins. Longer is more to edit down; the "
             "draft is never put in the form until you press to put it there.",
    ),
    Tunable(
        key="doc_description_chars", env="DOC_DESCRIPTION_CHARS", kind="int",
        minimum=2000, maximum=100000, group="Documents",
        label="Description used for writing (characters)",
        help="How much of the posting document writing reads. Too low and the "
             "requirements it should tailor to are below the cut.",
    ),
    Tunable(
        key="doc_refresh_enabled", env="DOC_REFRESH_ENABLED", kind="bool",
        group="Documents",
        label="Rewrite documents when the full posting arrives",
        help="Documents written from a teaser are rewritten once the real "
             "description is fetched — only for applications you have not "
             "acted on. Off leaves them as first written.",
    ),
    Tunable(
        key="doc_refresh_interval_hours", env="DOC_REFRESH_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Documents",
        label="Look for documents to rewrite every (hours)",
        help="How often the rewrite above looks for work.",
    ),
    Tunable(
        key="doc_refresh_max_per_run", env="DOC_REFRESH_MAX_PER_RUN", kind="int",
        minimum=1, maximum=500, group="Documents",
        label="Documents rewritten per run",
        help="Higher clears a backlog sooner, but the documents for the job "
             "you are looking at now wait behind them.",
    ),
    Tunable(
        key="generation_stuck_minutes", env="GENERATION_STUCK_MINUTES", kind="int",
        minimum=10, maximum=240, group="Documents",
        label="Treat a generation as stuck after (minutes)",
        help="A generation running this long lost its worker and is queued "
             "again; the same interval is how often that is looked for. Kept "
             "above a generation's own time limit, so one still running is "
             "never started twice.",
    ),
    Tunable(
        key="generation_sweep_max_per_run", env="GENERATION_SWEEP_MAX_PER_RUN",
        kind="int", minimum=1, maximum=1000, group="Documents",
        label="Stuck or unqueued generations picked up per run",
        help="Higher clears a pile-up at once and queues all of it together, "
             "which is the pile-up this exists to prevent.",
    ),
    # -- Descriptions -------------------------------------------------------
    Tunable(
        key="enrich_enabled", env="ENRICH_ENABLED", kind="bool", group="Descriptions",
        label="Fetch descriptions the source left out",
        help="Goes back to the employer for the text an aggregator truncated "
             "or never sent, so jobs are scored on the real posting. Off "
             "scores them on whatever the source sent.",
    ),
    Tunable(
        key="enrich_interval_minutes", env="ENRICH_INTERVAL_MINUTES", kind="int",
        minimum=5, maximum=1440, group="Descriptions",
        label="Look for missing descriptions every (minutes)",
        help="How often a pass starts on its own. Passes also chain while the "
             "backlog lasts (below), so this matters most once it is drained.",
    ),
    Tunable(
        key="enrich_max_per_run", env="ENRICH_MAX_PER_RUN", kind="int",
        minimum=1, maximum=5000, group="Descriptions",
        label="Descriptions fetched per pass",
        help="A pass with no ceiling holds a worker for hours while the jobs "
             "it already rescued wait to be scored.",
    ),
    Tunable(
        key="enrich_on_fetch", env="ENRICH_ON_FETCH", kind="bool", group="Descriptions",
        label="Fetch descriptions at the end of each fetch",
        help="So the jobs that just arrived are scored on their real text "
             "rather than the stub the aggregator sent.",
    ),
    Tunable(
        key="enrich_max_per_fetch", env="ENRICH_MAX_PER_FETCH", kind="int",
        minimum=0, maximum=5000, group="Descriptions",
        label="Descriptions fetched at the end of a fetch",
        help="Smaller than a scheduled pass: the fetch is already long, and "
             "the backlog is the scheduled pass's job.",
    ),
    Tunable(
        key="enrich_workers", env="ENRICH_WORKERS", kind="int",
        minimum=1, maximum=32, group="Descriptions",
        label="Descriptions fetched at once",
        help="Across all sites. Each site still gets its own limit below.",
    ),
    Tunable(
        key="enrich_per_host", env="ENRICH_PER_HOST", kind="int",
        minimum=1, maximum=16, group="Descriptions",
        label="Requests at once to one site",
        help="The real politeness budget. Higher is faster on a backlog that "
             "is mostly one site, and likelier to be refused by it.",
    ),
    Tunable(
        key="enrich_host_delay_ms", env="ENRICH_HOST_DELAY_MS", kind="int",
        minimum=0, maximum=10000, group="Descriptions",
        label="Gap between requests to one site (ms)",
        help="The minimum pause between two requests to the same site. 0 "
             "sends them back to back.",
    ),
    Tunable(
        key="enrich_chain_passes", env="ENRICH_CHAIN_PASSES", kind="bool",
        group="Descriptions",
        label="Start the next pass as soon as one fills up",
        help="Instead of idling until the next scheduled pass while a backlog "
             "waits.",
    ),
    Tunable(
        key="enrich_max_chained_passes", env="ENRICH_MAX_CHAINED_PASSES", kind="int",
        minimum=1, maximum=500, group="Descriptions",
        label="Passes chained back to back at most",
        help="A ceiling on one chain, so a fault cannot make it permanent.",
    ),
    Tunable(
        key="enrich_retry_days", env="ENRICH_RETRY_DAYS", kind="int",
        minimum=1, maximum=90, group="Descriptions",
        label="Retry a failed description after (days)",
        help="A site refusing us this week may not next week. Too short and "
             "the same failures sit at the head of the queue.",
    ),
    Tunable(
        key="enrich_max_browser_outstanding", env="ENRICH_MAX_BROWSER_OUTSTANDING",
        kind="int", minimum=0, maximum=5000, group="Descriptions",
        label="Descriptions waiting for the browser at most",
        help="The browser reads these at a person's pace, so queueing more "
             "than it can drain only builds a backlog that expires unread. 0 "
             "hands none to the browser.",
    ),
    Tunable(
        key="enrich_paused_host_daily", env="ENRICH_PAUSED_HOST_DAILY", kind="int",
        minimum=0, maximum=1000, group="Descriptions",
        label="Descriptions a day from a paused site",
        help="A paused site is not crawled, but reading the description of a "
             "job you might apply to is a different act. About a person "
             "reading adverts by default; 0 asks a paused site for nothing.",
    ),
    Tunable(
        key="enrich_host_memory_days", env="ENRICH_HOST_MEMORY_DAYS", kind="int",
        minimum=1, maximum=90, group="Descriptions",
        label="Remember how a site answered for (days)",
        help="The window over which a site that rarely gives a description is "
             "judged not worth asking. Its evidence ages out, so a site that "
             "improves is tried again.",
    ),
    Tunable(
        key="enrich_host_min_attempts", env="ENRICH_HOST_MIN_ATTEMPTS", kind="int",
        minimum=1, maximum=10000, group="Descriptions",
        label="Attempts before judging a site",
        help="How many tries in that window before a site can be set aside.",
    ),
    Tunable(
        key="enrich_host_min_success_rate", env="ENRICH_HOST_MIN_SUCCESS_RATE",
        kind="float", minimum=0.0, maximum=1.0, group="Descriptions",
        label="Set a site aside below this success rate",
        help="A fraction: 0.02 is two in a hundred. A rate rather than a count, "
             "because the best site fails most often by volume. 0 never sets "
             "one aside.",
    ),
    Tunable(
        key="rescore_max_per_run", env="RESCORE_MAX_PER_RUN", kind="int",
        minimum=0, maximum=20000, group="Descriptions",
        label="Jobs sent back for scoring per pass",
        help="Jobs rejected for a thin description go back to matching once "
             "the real one arrives. No request and no model call to send them; "
             "the ceiling is how fast matching can take them.",
    ),
    # -- Closed postings ----------------------------------------------------
    Tunable(
        key="liveness_enabled", env="LIVENESS_ENABLED", kind="bool",
        group="Closed postings",
        label="Check whether matched postings have closed",
        help="Matched jobs are re-checked against the employer page, and one "
             "that has closed wears a badge. Only a 404, a page saying the "
             "role is closed, or a known ATS bouncing to its index counts.",
    ),
    Tunable(
        key="liveness_interval_hours", env="LIVENESS_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Closed postings",
        label="Check every (hours)",
        help="How often a sweep runs. Each sweep checks only verdicts that "
             "are due, so a short interval costs little when nothing is.",
    ),
    Tunable(
        key="liveness_max_per_cycle", env="LIVENESS_MAX_PER_CYCLE", kind="int",
        minimum=10, maximum=10000, group="Closed postings",
        label="Postings checked per sweep at most",
        help="A ceiling, not a quota: only postings whose verdict is due are "
             "checked, highest-scored first among those not yet applied to. "
             "Too low and the lowest-scored verdicts go stale; the log says "
             "when.",
    ),
    Tunable(
        key="liveness_workers", env="LIVENESS_WORKERS", kind="int",
        minimum=1, maximum=32, group="Closed postings",
        label="Postings checked at once",
        help="Parallel checks. Most postings are on a few ATS sites, so higher "
             "is more requests at once to the same hosts.",
    ),
    Tunable(
        key="liveness_recheck_days", env="LIVENESS_RECHECK_DAYS", kind="int",
        minimum=1, maximum=60, group="Closed postings",
        label="A verdict stands for (days)",
        help="How long an open verdict is trusted before the posting is "
             "checked again. Shorter catches closures sooner and multiplies "
             "the checks.",
    ),
    # -- Outreach -----------------------------------------------------------
    Tunable(
        key="outreach_enabled", env="OUTREACH_ENABLED", kind="bool", group="Outreach",
        label="Find people to contact for each application",
        help="Looks for recruiters and engineers at the company. Off stops "
             "discovery; nothing is ever sent without a click either way.",
    ),
    Tunable(
        key="outreach_max_contacts_per_app", env="OUTREACH_MAX_CONTACTS_PER_APP",
        kind="int", minimum=1, maximum=50, group="Outreach",
        label="Contacts kept per application",
        help="More than a handful is noise — the point is two or three good "
             "people.",
    ),
    Tunable(
        key="outreach_use_linkedin", env="OUTREACH_USE_LINKEDIN", kind="bool",
        group="Outreach",
        label="Search LinkedIn for people",
        help="An authenticated scrape from a server, which is what LinkedIn "
             "restricts accounts for. The deep links reach the same profiles "
             "with no account risk; understand that before switching this on.",
    ),
    Tunable(
        key="outreach_linkedin_max_searches", env="OUTREACH_LINKEDIN_MAX_SEARCHES",
        kind="int", minimum=0, maximum=10, group="Outreach",
        label="LinkedIn people searches per run",
        help="The account risk scales with volume, so this stays low.",
    ),
    Tunable(
        key="outreach_use_github", env="OUTREACH_USE_GITHUB", kind="bool",
        group="Outreach",
        label="Look at the company GitHub organisation",
        help="Public members are real engineers, often with a published "
             "address. Needs a GitHub token in the environment.",
    ),
    Tunable(
        key="outreach_use_team_pages", env="OUTREACH_USE_TEAM_PAGES", kind="bool",
        group="Outreach",
        label="Read the company team and about pages",
        help="For profile links and published addresses. No key, no quota.",
    ),
    Tunable(
        key="outreach_target_titles", env="OUTREACH_TARGET_TITLES", kind="text",
        group="Outreach",
        label="Titles to look for",
        help="Comma-separated, most wanted first.",
    ),
    Tunable(
        key="outreach_guess_emails", env="OUTREACH_GUESS_EMAILS", kind="bool",
        group="Outreach",
        label="Guess likely addresses",
        help="first.last@domain and similar, when nobody publishes a real one. "
             "Guesses are marked as guesses and never sent automatically.",
    ),
    Tunable(
        key="outreach_verify_emails", env="OUTREACH_VERIFY_EMAILS", kind="bool",
        group="Outreach",
        label="Verify addresses with Hunter",
        help="Spends one Hunter verifier credit per address found.",
    ),
    Tunable(
        key="outreach_followup_days", env="OUTREACH_FOLLOWUP_DAYS", kind="text",
        group="Outreach",
        label="Follow up after (days)",
        help="Comma-separated, one per step: 4,7,10 follows up four days after "
             "sending, then seven, then ten. Empty sends no follow-ups.",
    ),
    Tunable(
        key="outreach_auto_draft_followups", env="OUTREACH_AUTO_DRAFT_FOLLOWUPS",
        kind="bool", group="Outreach",
        label="Draft due follow-ups automatically",
        help="Drafts only — nothing is sent without a click.",
    ),
    Tunable(
        key="outreach_followup_interval_hours", env="OUTREACH_FOLLOWUP_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Outreach",
        label="Look for due follow-ups every (hours)",
        help="How soon after it falls due a follow-up is drafted.",
    ),
    Tunable(
        key="outreach_send_enabled", env="OUTREACH_SEND_ENABLED", kind="bool",
        group="Outreach",
        label="Allow sending email",
        help="Also needs a mail server in the environment. Every message is "
             "still sent only when you click send.",
    ),
    Tunable(
        key="outreach_max_sends_per_day", env="OUTREACH_MAX_SENDS_PER_DAY", kind="int",
        minimum=0, maximum=500, group="Outreach",
        label="Emails sent per day at most",
        help="Counted over the last 24 hours. 0 sends nothing.",
    ),
    Tunable(
        key="outreach_attach_documents", env="OUTREACH_ATTACH_DOCUMENTS", kind="bool",
        group="Outreach",
        label="Attach the resume and cover letter",
        help="The current versions, as PDFs.",
    ),
    Tunable(
        key="imap_enabled", env="IMAP_ENABLED", kind="bool", group="Outreach",
        label="Read the mailbox for replies and bounces",
        help="So a follow-up is never drafted to someone who already answered. "
             "Also needs mailbox credentials in the environment.",
    ),
    Tunable(
        key="imap_lookback_days", env="IMAP_LOOKBACK_DAYS", kind="int",
        minimum=1, maximum=365, group="Outreach",
        label="First mailbox read looks back (days)",
        help="Later reads resume where the last stopped, so this only bounds "
             "the first scan.",
    ),
    Tunable(
        key="imap_max_messages_per_poll", env="IMAP_MAX_MESSAGES_PER_POLL", kind="int",
        minimum=1, maximum=5000, group="Outreach",
        label="Messages read per mailbox check",
        help="The rest are read on the next check.",
    ),
    Tunable(
        key="imap_poll_interval_minutes", env="IMAP_POLL_INTERVAL_MINUTES", kind="int",
        minimum=1, maximum=1440, group="Outreach",
        label="Check the mailbox every (minutes)",
        help="How soon a reply is noticed.",
    ),
    # -- Browser agent ------------------------------------------------------
    Tunable(
        key="browse_enabled", env="BROWSE_ENABLED", kind="bool", group="Browser agent",
        label="Let the extension open pages on its own",
        help="The extension opens job boards in a hidden window so they are "
             "read without you visiting each one. Off leaves only the pages "
             "you open yourself.",
    ),
    Tunable(
        key="browse_gap_seconds", env="BROWSE_GAP_SECONDS", kind="int",
        minimum=0, maximum=600, group="Browser agent",
        label="Gap between pages (seconds)",
        help="The minimum pause between one page closing and the next opening "
             "on a site — the single most important number here. Rhythm is "
             "what anti-automation watches, and the account is what is lost.",
    ),
    Tunable(
        key="browse_settle_seconds", env="BROWSE_SETTLE_SECONDS", kind="int",
        minimum=0, maximum=60, group="Browser agent",
        label="Leave a page open after it loads (seconds)",
        help="Some boards fetch the posting after the page reports loaded; "
             "closing sooner harvests nothing.",
    ),
    Tunable(
        key="browse_max_queued", env="BROWSE_MAX_QUEUED", kind="int",
        minimum=1, maximum=1000, group="Browser agent",
        label="Pages per run",
        help="About an hour of browsing at the default pace.",
    ),
    Tunable(
        key="browse_retry_days", env="BROWSE_RETRY_DAYS", kind="int",
        minimum=1, maximum=365, group="Browser agent",
        label="Reopen a posting after (days)",
        help="A posting page does not change, so it is not opened again "
             "sooner than this.",
    ),
    Tunable(
        key="browse_search_retry_hours", env="BROWSE_SEARCH_RETRY_HOURS", kind="int",
        minimum=1, maximum=720, group="Browser agent",
        label="Reopen a search page after (hours)",
        help="A search page is only ever the postings that exist now, so it "
             "is worth reopening far sooner than a posting.",
    ),
    Tunable(
        key="browse_search_reserve", env="BROWSE_SEARCH_RESERVE", kind="int",
        minimum=0, maximum=500, group="Browser agent",
        label="Pages per top-up kept for searching",
        help="Reserved for crawling boards rather than fetching descriptions. "
             "0 and searching never happens, since the description backlog is "
             "never empty.",
    ),
    Tunable(
        key="browse_search_pages", env="BROWSE_SEARCH_PAGES", kind="int",
        minimum=1, maximum=50, group="Browser agent",
        label="Result pages per search",
        help="About twenty-five postings a page. Each page is a visit, so this "
             "multiplies the length of a run.",
    ),
    Tunable(
        key="browse_scroll_passes", env="BROWSE_SCROLL_PASSES", kind="int",
        minimum=1, maximum=200, group="Browser agent",
        label="Screens scrolled per page",
        help="For boards with no opinion of their own; infinite-scroll boards "
             "go deeper. The extension caps the time either way.",
    ),
    Tunable(
        key="browse_scroll_pause_seconds", env="BROWSE_SCROLL_PAUSE_SECONDS", kind="int",
        minimum=0, maximum=60, group="Browser agent",
        label="Pause between scrolls on a site that objected (seconds)",
        help="Only on a board that has asked us to slow down before; 0 "
             "everywhere else.",
    ),
    Tunable(
        key="browse_ratelimit_rest_minutes", env="BROWSE_RATELIMIT_REST_MINUTES",
        kind="int", minimum=1, maximum=1440, group="Browser agent",
        label="Rest after a site asks us to slow down (minutes)",
        help="A rate limit means not this fast, not never, so minutes.",
    ),
    Tunable(
        key="browse_challenge_backoff_hours", env="BROWSE_CHALLENGE_BACKOFF_HOURS",
        kind="int", minimum=1, maximum=720, group="Browser agent",
        label="Rest after a human check nobody passed (hours)",
        help="The first rest; it doubles each time the check comes back, up "
             "to the ceiling below.",
    ),
    Tunable(
        key="browse_challenge_max_backoff_hours", env="BROWSE_CHALLENGE_MAX_BACKOFF_HOURS",
        kind="int", minimum=1, maximum=8760, group="Browser agent",
        label="Longest rest after repeated human checks (hours)",
        help="Still tried occasionally at the ceiling, since a site can relent.",
    ),
    Tunable(
        key="browse_topup_interval_minutes", env="BROWSE_TOPUP_INTERVAL_MINUTES",
        kind="int", minimum=5, maximum=1440, group="Browser agent",
        label="Check whether the browser needs work every (minutes)",
        help="It does nothing unless the queue is nearly empty, so this sets "
             "responsiveness rather than volume.",
    ),
    Tunable(
        key="browse_topup_below", env="BROWSE_TOPUP_BELOW", kind="int",
        minimum=0, maximum=500, group="Browser agent",
        label="Top up when fewer pages than this are waiting",
        help="Refilling a queue that is still working would outrun the "
             "browser.",
    ),
    Tunable(
        key="browse_agent_stale_hours", env="BROWSE_AGENT_STALE_HOURS", kind="int",
        minimum=1, maximum=720, group="Browser agent",
        label="Treat the extension as gone after (hours)",
        help="No work is queued for an extension that has not checked in this "
             "long — it would expire unread on a closed laptop.",
    ),
    Tunable(
        key="browse_greenhouse_feed", env="BROWSE_GREENHOUSE_FEED", kind="text",
        group="Browser agent",
        label="Greenhouse job-seeker search addresses",
        help="Set the filters on my.greenhouse.io, copy the address, and put "
             "{q} where the keyword is. Without {q} a page is crawled as it "
             "is. Comma-separated for several; empty crawls none.",
    ),
    Tunable(
        key="browse_tsenta_feed", env="BROWSE_TSENTA_FEED", kind="text",
        group="Browser agent",
        label="Tsenta recommendations address",
        help="Tsenta keeps its filters in its own state, so set them on the "
             "site and paste the page you land on. Empty crawls none.",
    ),
    Tunable(
        key="agent_task_ttl_hours", env="AGENT_TASK_TTL_HOURS", kind="int",
        minimum=1, maximum=720, group="Browser agent",
        label="Queued browser work expires after (hours)",
        help="Resolving a job link matters today and not next week.",
    ),
    Tunable(
        key="agent_link_resolve_max_queued", env="AGENT_LINK_RESOLVE_MAX_QUEUED",
        kind="int", minimum=0, maximum=5000, group="Browser agent",
        label="Links handed to the browser per cycle",
        help="Aggregator links the server could not follow. 0 hands none over.",
    ),
    Tunable(
        key="harvest_samples_enabled", env="HARVEST_SAMPLES_ENABLED", kind="bool",
        group="Browser agent",
        label="Keep samples of pages nothing could read",
        help="Trimmed copies to write a reader from. They come from a logged-in "
             "session and can carry names, so they are capped and expired.",
    ),
    Tunable(
        key="harvest_samples_per_host", env="HARVEST_SAMPLES_PER_HOST", kind="int",
        minimum=0, maximum=100, group="Browser agent",
        label="Samples kept per site",
        help="0 keeps none.",
    ),
    # -- Sources ------------------------------------------------------------
    Tunable(
        key="browser_tier_enabled", env="BROWSER_TIER_ENABLED", kind="bool",
        group="Sources",
        label="Browser tier (Playwright scrapers)",
        help="The most expensive part of the pipeline and the least "
             "productive per minute. Off skips it entirely.",
    ),
    Tunable(
        key="hiringcafe_enabled", env="HIRINGCAFE_ENABLED", kind="bool", group="Sources",
        label="HiringCafe",
        help="Indexes ATS boards directly, so its postings carry full "
             "descriptions and link to the employer.",
    ),
    Tunable(
        key="yc_enabled", env="YC_ENABLED", kind="bool", group="Sources",
        label="Y Combinator jobs",
        help="The public role pages of Y Combinator companies.",
    ),
    Tunable(
        key="yc_roles", env="YC_ROLES", kind="text", group="Sources",
        label="Y Combinator: role pages",
        help="Comma-separated role slugs from the YC jobs site. Empty uses the "
             "built-in engineering list.",
    ),
    Tunable(
        key="indeed_rss_enabled", env="INDEED_RSS_ENABLED", kind="bool", group="Sources",
        label="Indeed RSS",
        help="Indeed retired the feed and every query fails. On only if it "
             "comes back.",
    ),
    Tunable(
        key="arbeitnow_max_pages", env="ARBEITNOW_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="Sources",
        label="Arbeitnow: pages per run",
        help="Each page is one request.",
    ),
    Tunable(
        key="adzuna_max_pages", env="ADZUNA_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="Sources",
        label="Adzuna: pages per search",
        help="Fifty results a page, each page one call against the Adzuna "
             "quota.",
    ),
    Tunable(
        key="adzuna_max_days_old", env="ADZUNA_MAX_DAYS_OLD", kind="int",
        minimum=1, maximum=90, group="Sources",
        label="Adzuna: posted within (days)",
        help="A one-day window missed every posting from a day the fetch did "
             "not run; longer overlaps and the repeats are merged.",
    ),
    Tunable(
        key="usajobs_max_pages", env="USAJOBS_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="Sources",
        label="USAJobs: pages per search",
        help="Each page is one request to USAJobs.",
    ),
    Tunable(
        key="source_rest_after_failures", env="SOURCE_REST_AFTER_FAILURES", kind="int",
        minimum=0, maximum=100, group="Sources",
        label="Rest a source after this many failed runs in a row",
        help="An expired key fails the same way forever. A resting source is "
             "still probed now and then (below). 0 never rests one.",
    ),
    Tunable(
        key="source_rest_retry_every", env="SOURCE_REST_RETRY_EVERY", kind="int",
        minimum=1, maximum=100, group="Sources",
        label="Probe a resting source every (runs)",
        help="So a refreshed key resumes on its own.",
    ),
    # -- LinkedIn -----------------------------------------------------------
    Tunable(
        key="linkedin_max_detail_fetches", env="LINKEDIN_MAX_DETAIL_FETCHES", kind="int",
        minimum=0, maximum=10000, group="LinkedIn",
        label="LinkedIn descriptions per run",
        help="A politeness ceiling. Only postings that pass the title check "
             "are fetched, so each is one worth having. 0 fetches none.",
    ),
    Tunable(
        key="linkedin_detail_workers", env="LINKEDIN_DETAIL_WORKERS", kind="int",
        minimum=1, maximum=16, group="LinkedIn",
        label="LinkedIn descriptions fetched at once",
        help="Higher is faster and more likely to be throttled.",
    ),
    # -- Company boards -----------------------------------------------------
    Tunable(
        key="ats_auto_discovery", env="ATS_AUTO_DISCOVERY", kind="bool",
        group="Company boards",
        label="Learn company boards from job links",
        help="Every link to an ATS board makes that company pollable. Off "
             "polls only the boards already known.",
    ),
    Tunable(
        key="ats_seed_companies", env="ATS_SEED_COMPANIES", kind="bool",
        group="Company boards",
        label="Include the built-in list of known companies",
        help="A verified seed list of tech employers boards.",
    ),
    Tunable(
        key="ats_slug_validation", env="ATS_SLUG_VALIDATION", kind="bool",
        group="Company boards",
        label="Check and correct the boards listed below",
        help="Boards typed in by hand are checked against the ATS and fixed "
             "where the name is close.",
    ),
    Tunable(
        key="ats_list_harvest", env="ATS_LIST_HARVEST", kind="bool",
        group="Company boards",
        label="Mine community lists for boards",
        help="The lists named under Community lists to mine for boards.",
    ),
    Tunable(
        key="ats_board_registry", env="ATS_BOARD_REGISTRY", kind="bool",
        group="Company boards",
        label="Keep a registry of boards ranked by yield",
        help="Discovered boards are stored and polled by how much they have "
             "given. Off rebuilds the list each cycle from what is found then.",
    ),
    Tunable(
        key="ats_board_validation", env="ATS_BOARD_VALIDATION", kind="bool",
        group="Company boards",
        label="Probe a new board before polling it",
        help="A slug read out of a link is a guess; probing first stops "
             "non-companies spending the budget real ones compete for.",
    ),
    Tunable(
        key="ats_board_max_empty_cycles", env="ATS_BOARD_MAX_EMPTY_CYCLES", kind="int",
        minimum=1, maximum=100, group="Company boards",
        label="Retire a board after this many empty cycles",
        help="A discovered board that returns nothing this many times is no "
             "longer polled.",
    ),
    Tunable(
        key="board_backfill_on_start", env="BOARD_BACKFILL_ON_START", kind="bool",
        group="Company boards",
        label="Mine stored jobs for boards once after a deploy",
        help="Jobs stored before the registry existed were never searched for "
             "the boards in their descriptions. The first fetch after a "
             "deploy does it once.",
    ),
    Tunable(
        key="board_backfill_max_links", env="BOARD_BACKFILL_MAX_LINKS", kind="int",
        minimum=0, maximum=10000, group="Company boards",
        label="Links followed by that one-off mining",
        help="Caps the extra requests it costs.",
    ),
    Tunable(
        key="board_backfill_max_hosts", env="BOARD_BACKFILL_MAX_HOSTS", kind="int",
        minimum=0, maximum=5000, group="Company boards",
        label="Careers sites looked behind by that mining",
        help="Caps the extra requests it costs.",
    ),
    Tunable(
        key="board_backfill_workers", env="BOARD_BACKFILL_WORKERS", kind="int",
        minimum=1, maximum=64, group="Company boards",
        label="Requests at once during that mining",
        help="Sized so the worst case, every request timing out, stays a few "
             "minutes.",
    ),
    # -- Apply links --------------------------------------------------------
    Tunable(
        key="resolve_apply_links", env="RESOLVE_APPLY_LINKS", kind="bool",
        group="Apply links",
        label="Follow aggregator links to the employer",
        help="Adzuna, Jooble and Careerjet link to their own redirect page. "
             "Following it once gives the real apply link and the company "
             "board behind it.",
    ),
    Tunable(
        key="link_resolve_max_per_cycle", env="LINK_RESOLVE_MAX_PER_CYCLE", kind="int",
        minimum=0, maximum=50000, group="Apply links",
        label="Links followed per cycle",
        help="Only stops one cycle running unboundedly long; the real limit "
             "is per site, below.",
    ),
    Tunable(
        key="link_resolve_workers", env="LINK_RESOLVE_WORKERS", kind="int",
        minimum=1, maximum=64, group="Apply links",
        label="Links followed at once",
        help="Across all sites.",
    ),
    Tunable(
        key="link_resolve_per_host", env="LINK_RESOLVE_PER_HOST", kind="int",
        minimum=1, maximum=32, group="Apply links",
        label="Links followed at once on one site",
        help="So a backlog that is mostly one aggregator paces itself.",
    ),
    Tunable(
        key="link_resolve_host_delay_ms", env="LINK_RESOLVE_HOST_DELAY_MS", kind="int",
        minimum=0, maximum=10000, group="Apply links",
        label="Gap between links on one site (ms)",
        help="0 sends them back to back.",
    ),
    # -- Schedule -----------------------------------------------------------
    Tunable(
        key="match_interval_minutes", env="MATCH_INTERVAL_MINUTES", kind="int",
        minimum=1, maximum=1440, group="Schedule",
        label="Matching: every (minutes)",
        help="How often matching looks for new jobs on its own, besides "
             "after each fetch. It does nothing when there is nothing new.",
    ),
    Tunable(
        key="fetch_linked_interval_hours", env="FETCH_LINKED_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Schedule",
        label="Linked boards: feed every (hours)",
        help="Boards you linked with a credential, asked for their feed. "
             "Cheap: one request per page of twenty.",
    ),
    Tunable(
        key="fetch_linked_deep_interval_hours", env="FETCH_LINKED_DEEP_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=720, group="Schedule",
        label="Linked boards: whole index every (hours)",
        help="About a thousand requests each time, so far less often than the "
             "feed.",
    ),
    # -- Housekeeping -------------------------------------------------------
    Tunable(
        key="archive_enabled", env="ARCHIVE_ENABLED", kind="bool", group="Housekeeping",
        label="Archive settled rejections",
        help="Old rejected jobs stop carrying their descriptions. What "
             "deduplication needs is kept, so they are never fetched again.",
    ),
    Tunable(
        key="archive_after_days", env="ARCHIVE_AFTER_DAYS", kind="int",
        minimum=7, maximum=3650, group="Housekeeping",
        label="Archive rejections older than (days)",
        help="Shorter keeps the database smaller; an archived job cannot be "
             "reopened with its description.",
    ),
    Tunable(
        key="archive_max_per_run", env="ARCHIVE_MAX_PER_RUN", kind="int",
        minimum=1, maximum=100000, group="Housekeeping",
        label="Jobs archived per run",
        help="One transaction; higher holds a worker and a lock longer.",
    ),
    Tunable(
        key="archive_interval_hours", env="ARCHIVE_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Housekeeping",
        label="Archive every (hours)",
        help="How often archiving runs.",
    ),
    Tunable(
        key="llm_log_enabled", env="LLM_LOG_ENABLED", kind="bool", group="Housekeeping",
        label="Keep a log of model calls",
        help="Every prompt and reply, together, for telling a wrong prompt "
             "from a wrong answer on the Runs page.",
    ),
    Tunable(
        key="llm_log_max_chars", env="LLM_LOG_MAX_CHARS", kind="int",
        minimum=1000, maximum=200000, group="Housekeeping",
        label="Model log: characters kept per prompt or reply",
        help="Prompts carry whole descriptions; without a ceiling this table "
             "outgrows everything else.",
    ),
    Tunable(
        key="llm_log_keep_rows", env="LLM_LOG_KEEP_ROWS", kind="int",
        minimum=100, maximum=200000, group="Housekeeping",
        label="Model log: calls kept",
        help="The newest this many are kept.",
    ),
    Tunable(
        key="llm_log_prune_interval_hours", env="LLM_LOG_PRUNE_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Housekeeping",
        label="Model log: trim every (hours)",
        help="How often it is cut back to the size above.",
    ),
    Tunable(
        key="agent_ingest_retry_minutes", env="AGENT_INGEST_RETRY_MINUTES", kind="int",
        minimum=1, maximum=60, group="Housekeeping",
        label="Retry saved browser results every (minutes)",
        help="Recover server-side processing failures without browsing the page again. Higher values reduce retry load but delay recovery.",
    ),
    Tunable(
        key="agent_ingest_batch_size", env="AGENT_INGEST_BATCH_SIZE", kind="int",
        minimum=1, maximum=100, group="Housekeeping",
        label="Saved browser results per retry pass",
        help="Maximum results recovered in one pass. Lower values leave more capacity for interactive work.",
    ),
    Tunable(
        key="agent_event_keep_rows", env="AGENT_EVENT_KEEP_ROWS", kind="int",
        minimum=1000, maximum=1000000, group="Housekeeping",
        label="Extension events kept",
        help="Small rows answering questions about weeks: is the extension "
             "running, which sites is it failing on.",
    ),
    Tunable(
        key="agent_event_prune_interval_hours", env="AGENT_EVENT_PRUNE_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Housekeeping",
        label="Extension history: trim every (hours)",
        help="Trims the event log and finished browser tasks.",
    ),
    Tunable(
        key="browser_task_keep_days", env="BROWSER_TASK_KEEP_DAYS", kind="int",
        minimum=1, maximum=365, group="Housekeeping",
        label="Finished browser tasks kept for (days)",
        help="They carry the page they brought back, which is the large part.",
    ),
    Tunable(
        key="harvest_sample_ttl_days", env="HARVEST_SAMPLE_TTL_DAYS", kind="int",
        minimum=1, maximum=365, group="Housekeeping",
        label="Unread page samples kept for (days)",
        help="They can carry names from a logged-in session.",
    ),
    Tunable(
        key="score_history_keep_per_job", env="SCORE_HISTORY_KEEP_PER_JOB", kind="int",
        minimum=1, maximum=500, group="Housekeeping",
        label="Past scores kept per job",
        help="Per job, so a job first verdict survives however many calls "
             "the rest of the pipeline makes.",
    ),
    # -- Display ------------------------------------------------------------
    Tunable(
        key="display_timezone", env="DISPLAY_TIMEZONE", kind="text", group="Display",
        label="Time zone for dates on the pages",
        help="An IANA name such as America/New_York or Europe/London. Stored "
             "times stay UTC; a name that cannot be loaded shows UTC.",
    ),
])

# Boards polled whatever discovery finds, per ATS. Discovery fills the
# registry on its own from job links, community lists and careers sites; these
# are for a company it has not reached. Read per cycle through the cycle's
# settings (`ats_discovery.configured_ats_slugs`).
_BOARD_LISTS = (
    ("greenhouse_company_slugs", "GREENHOUSE_COMPANY_SLUGS", "Greenhouse",
     "board names, as in boards.greenhouse.io/acme"),
    ("lever_company_slugs", "LEVER_COMPANY_SLUGS", "Lever",
     "board names, as in jobs.lever.co/acme"),
    ("ashby_company_slugs", "ASHBY_COMPANY_SLUGS", "Ashby",
     "board names, as in jobs.ashbyhq.com/acme"),
    ("smartrecruiters_company_slugs", "SMARTRECRUITERS_COMPANY_SLUGS", "SmartRecruiters",
     "company names, as in jobs.smartrecruiters.com/acme"),
    ("workable_company_slugs", "WORKABLE_COMPANY_SLUGS", "Workable",
     "account names, as in apply.workable.com/acme"),
    ("recruitee_company_slugs", "RECRUITEE_COMPANY_SLUGS", "Recruitee",
     "subdomains, as in acme.recruitee.com"),
    ("icims_company_slugs", "ICIMS_COMPANY_SLUGS", "iCIMS",
     "portal names, as in careers-acme.icims.com"),
    ("bamboohr_company_slugs", "BAMBOOHR_COMPANY_SLUGS", "BambooHR",
     "subdomains, as in acme.bamboohr.com"),
    ("teamtailor_company_slugs", "TEAMTAILOR_COMPANY_SLUGS", "Teamtailor",
     "subdomains, as in acme.teamtailor.com"),
    ("jobvite_company_slugs", "JOBVITE_COMPANY_SLUGS", "Jobvite",
     "company names, as in jobs.jobvite.com/acme"),
    ("personio_company_slugs", "PERSONIO_COMPANY_SLUGS", "Personio",
     "subdomains, as in acme.jobs.personio.de"),
    ("workday_tenants", "WORKDAY_TENANTS", "Workday",
     "tenant:cluster:site entries, as in nvidia:wd5:NVIDIAExternalCareerSite"),
    ("taleo_boards", "TALEO_BOARDS", "Taleo",
     "tenant/section entries, as in textron/textron"),
    ("oracle_boards", "ORACLE_BOARDS", "Oracle Recruiting",
     "host:site entries, as in egug.fa.us2.oraclecloud.com:CX_1"),
    ("successfactors_boards", "SUCCESSFACTORS_BOARDS", "SuccessFactors",
     "careers hosts, as in careers.qorvo.com"),
    ("phenom_boards", "PHENOM_BOARDS", "Phenom",
     "host/country/language entries, as in careers.mastercard.com/us/en"),
    ("eightfold_boards", "EIGHTFOLD_BOARDS", "Eightfold",
     "careers hosts, as in qualcomm.eightfold.ai"),
    ("jibe_boards", "JIBE_BOARDS", "iCIMS careers sites",
     "careers hosts, as in careers.amd.com"),
    ("rippling_company_slugs", "RIPPLING_COMPANY_SLUGS", "Rippling",
     "board names, as in ats.rippling.com/acme"),
    ("pinpoint_company_slugs", "PINPOINT_COMPANY_SLUGS", "Pinpoint",
     "subdomains, as in acme.pinpointhq.com"),
    ("paylocity_company_ids", "PAYLOCITY_COMPANY_IDS", "Paylocity",
     "company ids, the one in recruiting.paylocity.com/Recruiting/Jobs/All/<id>"),
    ("avature_boards", "AVATURE_BOARDS", "Avature",
     "host/portal entries, as in bloomberg.avature.net/careers"),
)
TUNABLES.extend(
    Tunable(
        key=key, env=env, kind="text", group="Boards always polled",
        label=name,
        help=f"Comma-separated {what}. Polled every boards cycle within that "
             f"ATS budget, on top of what discovery finds. Empty is fine.",
    )
    for key, env, name, what in _BOARD_LISTS
)

BY_KEY: dict[str, Tunable] = {t.key: t for t in TUNABLES}


# What stays in the environment, and why. Every field of `app.config.Settings`
# is either declared above or named here — `tests/test_settings_coverage.py`
# fails on one that is neither, so an env-only knob is noticed the day it is
# added rather than the day somebody wonders why the page cannot change it.
# The test from CLAUDE.md: would you change it to see a different set of jobs?
# Then it is above. Is it what lets the application connect or authenticate at
# all? Then it is here.
SECRET = "a secret or credential; the page would have to store it in the profile and render it back"
CONNECTION = "where a service is and how to reach it"
SECURITY = "who may use the application; a web form must not be able to widen that"
DEPLOYMENT = "a fact about the machine or the deployment, read when a process starts"
PROTOCOL = "a timing the extension and the proxy in front of the app are built around"

ENVIRONMENT: dict[str, str] = {
    "SEMANTIC_API_KEY": SECRET,
    "SEMANTIC_BASE_URL": CONNECTION,
    **dict.fromkeys((
        "SECRET_KEY", "APP_PASSWORD", "AGENT_TOKEN", "NVIDIA_NIM_API_KEY",
        "FREEINFERENCE_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "HUNTER_IO_API_KEY", "GITHUB_TOKEN", "SMTP_USERNAME", "SMTP_PASSWORD",
        "IMAP_USERNAME", "IMAP_PASSWORD", "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
        "JSEARCH_API_KEY", "SERPAPI_API_KEY", "LINKEDIN_SESSION_COOKIE",
        "HANDSHAKE_SESSION_COOKIE", "JOOBLE_API_KEY", "FINDWORK_API_KEY",
        "CAREERJET_AFFID", "USAJOBS_API_KEY", "USAJOBS_USER_AGENT", "DICE_API_KEY",
    ), SECRET),
    **dict.fromkeys((
        "DATABASE_URL", "TEST_DATABASE_URL", "REDIS_URL", "NVIDIA_NIM_BASE_URL",
        "FREEINFERENCE_BASE_URL", "GEMINI_BASE_URL", "SMTP_HOST", "SMTP_PORT",
        "SMTP_USE_TLS", "SMTP_USE_SSL", "SMTP_TIMEOUT", "SMTP_FROM_EMAIL",
        "SMTP_FROM_NAME", "IMAP_HOST", "IMAP_PORT", "IMAP_FOLDER", "IMAP_TIMEOUT",
    ), CONNECTION),
    **dict.fromkeys((
        "AUTH_ENABLED", "SESSION_MAX_AGE_SECONDS", "SESSION_COOKIE_SECURE",
        "CORS_ALLOW_ORIGINS",
    ), SECURITY),
    **dict.fromkeys((
        "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE",
        "DEBUG", "STORAGE_PATH", "DOCS_OUTPUT_DIR", "BACKUP_DIR",
    ), DEPLOYMENT),
    **dict.fromkeys((
        "AGENT_LEASE_SECONDS", "AGENT_POLL_MAX_WAIT_SECONDS", "AGENT_MAX_LEASE_BATCH",
    ), PROTOCOL),
}


def choices_for(tunable: Tunable, profile_data: dict | None) -> list[str]:
    """
    The options to render for a choice tunable, built now rather than at import.

    Model lists are edited on the settings page and provider keys come and go,
    so a list captured at process start would hide exactly the model somebody
    just added.
    """
    if not tunable.dynamic:
        return tunable.choices
    from app.services import model_catalog, model_roles

    if tunable.key == "nvidia_nim_model":
        return model_catalog.models(profile_data, "nim")
    if tunable.catalog:
        return model_catalog.models(profile_data, tunable.catalog)
    return model_roles.choices(tunable.key.removeprefix("model_"), profile_data)

GROUPS: list[str] = list(dict.fromkeys(t.group for t in TUNABLES))


def default(tunable: Tunable):
    """The environment value this falls back to."""
    if not tunable.env:
        # A model role. Its default is the first choice, which is "auto" —
        # there is no environment variable holding it because the answer is
        # computed rather than configured.
        return tunable.choices[0] if tunable.choices else None
    return getattr(settings, tunable.env, None)


def coerce(tunable: Tunable, raw):
    """
    A form value as the right type, clamped to range. None if unusable.

    Clamping rather than rejecting: a typo'd 5000 in a page-count box should
    become the maximum, not silently keep the old value with no explanation.
    """
    if raw is None:
        return None
    try:
        if tunable.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in {"1", "true", "on", "yes"}
        if tunable.kind == "text":
            # A comma-separated list, as the env variable behind it is. Kept
            # as text rather than parsed, so the consumer that already splits
            # the env value reads the override the same way.
            text = ",".join(
                part.strip() for part in str(raw).replace("\n", ",").split(",")
                if part.strip()
            )
            return text[:2000]
        if tunable.kind == "choice":
            text = str(raw).strip()
            if tunable.dynamic:
                # Anything shaped like a model id. `model_roles.resolve` already
                # falls back when a setting names a provider that is no longer
                # configured, and losing the setting outright is worse than
                # carrying one that is briefly stale. Membership in the current
                # list is checked when the form is saved (see `parse_form`),
                # where the profile's model lists are to hand.
                from app.services.model_catalog import is_model_id

                return text if text and is_model_id(text) else None
            return text if text in tunable.choices else None
        number = float(raw)
    except (TypeError, ValueError):
        return None

    if tunable.minimum is not None:
        number = max(number, tunable.minimum)
    if tunable.maximum is not None:
        number = min(number, tunable.maximum)
    return int(number) if tunable.kind == "int" else round(number, 2)


def value(profile_data: dict | None, key: str):
    """The effective value: profile override if set, else the env default."""
    tunable = BY_KEY[key]
    data = profile_data or {}

    # The legacy top-level key wins while it's the one that's been live. Both
    # are written on every save, so this stops mattering after the first one.
    if tunable.legacy_key and tunable.legacy_key in data:
        coerced = coerce(tunable, data[tunable.legacy_key])
        if coerced is not None:
            return coerced

    stored = (data.get(STORE_KEY) or {}).get(key)
    if stored is not None:
        coerced = coerce(tunable, stored)
        if coerced is not None:
            return coerced
    return default(tunable)


def _load_profile_data() -> dict:
    """
    The profile's data, read in a session of its own. `{}` when there is none
    or it cannot be read — the environment's values then stand, which is what
    every setting falls back to anyway.

    One function, so the tests can point it at their own session: a test's
    profile lives inside a transaction no other connection can see.
    """
    try:
        from app.database import SessionLocal
        from app.models.profile import Profile

        db = SessionLocal()
        try:
            profile = db.query(Profile).first()
            return dict(profile.data or {}) if profile else {}
        finally:
            db.close()
    except Exception as exc:
        logger.warning("tunables: could not read the profile: %s", exc)
        return {}


def current(key: str):
    """
    A tunable's value for code that has no profile to hand.

    Some consumers sit a long way from a session — a check made once per URL
    inside a planner, a helper deep in enrichment — and threading the profile
    down to them would touch a dozen signatures for one read. This reads it
    itself. Deliberately uncached: it is one single-row query, and the settings
    it serves (a site to stop visiting) have to bite the moment they are saved.
    """
    return value(_load_profile_data(), key)


def values(profile_data: dict | None) -> dict:
    """Every tunable resolved, for rendering the form."""
    return {t.key: value(profile_data, t.key) for t in TUNABLES}


def is_overridden(profile_data: dict | None, key: str) -> bool:
    """Whether this differs from the environment default, for the UI to mark."""
    return value(profile_data, key) != default(BY_KEY[key])


def parse_form(form: dict, profile_data: dict | None = None) -> dict:
    """
    Coerce a submitted form into the stored override dict.

    Unchecked checkboxes don't appear in a form body at all, so booleans are
    read as absent-means-false rather than skipped like the other kinds.

    A model is only accepted if it is on the list the page offered — the model
    lists edited under "Model lists" — because the id goes straight to the
    provider. Pass the profile so that list can be read.
    """
    parsed = {}
    for tunable in TUNABLES:
        if tunable.kind == "bool":
            parsed[tunable.key] = coerce(tunable, form.get(tunable.key, False))
            continue
        if tunable.key not in form:
            continue
        coerced = coerce(tunable, form[tunable.key])
        if coerced is None:
            continue
        if tunable.dynamic and coerced not in choices_for(tunable, profile_data):
            logger.warning("tunables: %s=%r is not on the offered list; ignored",
                           tunable.key, coerced)
            continue
        parsed[tunable.key] = coerced
    return parsed


def apply_to_profile(profile_data: dict, parsed: dict) -> dict:
    """
    The profile data with these overrides stored. Does not mutate the input.

    Legacy top-level keys are written alongside, so the two places a value can
    live can't drift apart again.
    """
    import copy

    updated = copy.deepcopy(profile_data or {})
    updated[STORE_KEY] = {**(updated.get(STORE_KEY) or {}), **parsed}
    for tunable in TUNABLES:
        if tunable.legacy_key and tunable.key in parsed:
            updated[tunable.legacy_key] = parsed[tunable.key]
    return updated


class _Overlay:
    """
    `settings` with the profile's overrides on top.

    Adapters take a `cfg` object and read attributes off it, so handing them
    this instead of `settings` wires every one of them up without touching a
    single call site — and without them needing to know overrides exist.
    """

    def __init__(self, base, overrides: dict):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def effective_settings(profile_data: dict | None, base=None):
    """`settings` with UI overrides applied, for anything that reads `cfg.X`."""
    base = base if base is not None else settings
    overrides = {}
    for tunable in TUNABLES:
        if not tunable.env:
            # A model role. There is no `settings.X` behind it to overlay —
            # it is read through `model_roles`, not through `cfg`. Including it
            # here wrote an override keyed on the empty string, which built an
            # overlay for a profile that had overridden nothing.
            continue
        resolved = value(profile_data, tunable.key)
        if resolved is not None and resolved != getattr(base, tunable.env, None):
            overrides[tunable.env] = resolved
    return _Overlay(base, overrides) if overrides else base


# A settings object someone further up has already resolved — a fetch cycle's
# overlay (`sources.base.cycle_settings`) — so the code inside it reads the
# one snapshot the cycle started with rather than the profile again per call.
_BOUND: ContextVar = ContextVar("tunables_bound", default=None)


@contextmanager
def bound(cfg):
    """Make `cfg` what `live()` returns, for this block."""
    token = _BOUND.set(cfg)
    try:
        yield cfg
    finally:
        _BOUND.reset(token)


class _ReadOnFirstUse:
    """
    `live()` for one request or one task: the profile is read the first time a
    setting is asked for, and that answer serves the rest of the unit.

    Without it a page rendering fifty timestamps read the profile fifty times
    (`timefmt.zone`); with a plain read at the start, a request that asks for
    no setting at all paid for one anyway.
    """

    def __init__(self):
        object.__setattr__(self, "_cfg", None)

    def __getattr__(self, name):
        cfg = object.__getattribute__(self, "_cfg")
        if cfg is None:
            cfg = effective_settings(_load_profile_data())
            object.__setattr__(self, "_cfg", cfg)
        return getattr(cfg, name)


@contextmanager
def read_once():
    """
    Within this block `live()` reads the profile once, on first use. Wrapped
    around every web request (`main`) and every Celery task (`celery_app`), so
    a value saved on the settings page applies from the next request or task.
    """
    with bound(_ReadOnFirstUse()) as cfg:
        yield cfg


def live():
    """
    `settings` with the settings page's overrides on top, for code that has
    no profile to hand: read it as `live().THE_ENV_NAME`.

    Inside `bound()` it is that block's settings. Otherwise the profile is read
    now, uncached like `current()`, so a value saved on the page applies to the
    next pass that asks. One query — read it once at the top of a pass and hand
    the values down, rather than per item in a loop.
    """
    cfg = _BOUND.get()
    if cfg is not None:
        return cfg
    return effective_settings(_load_profile_data())
