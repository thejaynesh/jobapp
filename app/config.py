from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    TEST_DATABASE_URL: str = ""
    # Connection pool. Larger than SQLAlchemy's 5 + 10 default because an agent
    # long-polls, HTMX fragments refresh independently, and several panels query
    # per render — fifteen goes quickly with a browser talking to it too.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 20
    DB_POOL_RECYCLE: int = 1800
    REDIS_URL: str
    SECRET_KEY: str

    # ---- Authentication -------------------------------------------------
    # On by default: this application holds a full application history, a
    # profile, and — once the mailbox integration lands — mail credentials, and
    # it is normally deployed on a public VPS. Enabling it without configuring
    # it does not fall back to open access; the app serves 503 until it is set
    # up (see services.auth.misconfiguration).
    AUTH_ENABLED: bool = True
    # The password for the web UI. A long random string; there is no username.
    APP_PASSWORD: str = ""
    # Bearer token for /api/agent/* — the browser extension and local agent.
    # Separate from APP_PASSWORD so revoking one does not revoke the other.
    AGENT_TOKEN: str = ""
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 14

    # Agent queue (/api/agent/*). The laptop leases work, runs it, posts back.
    # How long a lease holds before the task returns to the queue. Long enough
    # for slow page work, short enough that a closed laptop is not a long stall.
    AGENT_LEASE_SECONDS: int = 120
    # Poll ceiling. Stays under the ~30s idle timeout that terminates an MV3
    # service worker, since a poll outliving its own client wakes nobody.
    AGENT_POLL_MAX_WAIT_SECONDS: int = 25
    # When queued work stops being worth doing. Resolving a job link matters
    # today and not next week.
    AGENT_TASK_TTL_HOURS: int = 24
    AGENT_MAX_LEASE_BATCH: int = 10
    # Per-cycle ceiling on aggregator links handed to the browser to resolve.
    # The server tries first and gets most of them; this is the remainder.
    AGENT_LINK_RESOLVE_MAX_QUEUED: int = 100

    # --- Driven browsing ---------------------------------------------------
    # Opening pages in a hidden window so the harvest reads them, instead of
    # waiting for the user to visit each one by hand.
    #
    # Every number here is deliberately conservative, and the reason is not
    # server load. This drives a real browser through a logged-in session, and
    # volume plus rhythm is exactly what a site's anti-automation systems look
    # at. A crawl that takes an evening and looks like reading is the point;
    # one that takes four minutes and looks like a script is the failure mode,
    # and the cost of getting it wrong is the account rather than the run.
    BROWSE_ENABLED: bool = True
    # Hosts the browser must not open, comma-separated. The switch to reach for
    # when a site says it has noticed the traffic: it stops new pages being
    # queued, drops the ones already waiting, and leaves every other board
    # running. Subdomains are covered, so `linkedin.com` is enough.
    #
    # Harvesting is unaffected and that separation is the point — reading job
    # data out of pages you open yourself makes no extra requests and is not
    # what gets an account flagged. Opening sixty pages a run through a
    # logged-in session is.
    BROWSE_PAUSED_HOSTS: str = ""
    # How long to leave a host alone after it showed a "confirm you're human"
    # check nobody got past. Jooble puts one in front of its apply redirects,
    # and without a backoff every thin Jooble job queues a visit that cannot
    # succeed. Short and re-earned on purpose: these checks are usually about
    # the traffic pattern rather than the visitor, so tomorrow is worth a try.
    BROWSE_CHALLENGE_BACKOFF_HOURS: int = 24
    # How long to leave a host alone after it asked us to slow down. Minutes
    # rather than the hours a human check gets, because that is what a rate
    # limit means — "not this fast", not "not at all". Greenhouse's board says
    # a few minutes; this is deliberately longer than it asks for.
    BROWSE_RATELIMIT_REST_MINUTES: int = 20
    # Seconds to rest between scroll batches, on a board that has objected
    # before. Zero everywhere else: pausing on a board that never complained is
    # depth given away for nothing. Worth more than a shallower scroll — the
    # limit is a rate, so a slower hand reaches further than a shorter run.
    BROWSE_SCROLL_PAUSE_SECONDS: int = 2
    # Pages per triggered run. Roughly an hour of browsing at the pace below.
    BROWSE_MAX_QUEUED: int = 60
    # Seconds to leave a page open after it finishes loading. LinkedIn fetches
    # the posting body after `load` fires, so closing the tab promptly is how
    # you crawl sixty pages and harvest nothing.
    BROWSE_SETTLE_SECONDS: int = 6
    # Minimum gap between one page closing and the next opening, in seconds.
    # The single most important number here.
    BROWSE_GAP_SECONDS: int = 20
    # Don't re-open a page browsed within this many days.
    BROWSE_RETRY_DAYS: int = 30
    # Result pages to walk per search. One page is about twenty-five cards, so
    # a crawl that stops there discovers almost nothing — depth is what makes
    # it a sweep rather than a peek. Each page is a queued visit like any
    # other, so this multiplies the run length: five pages of six roles across
    # two locations is sixty visits, which is one full run.
    BROWSE_SEARCH_PAGES: int = 5
    # Screens to scroll on a page whose board has no opinion. Boards that
    # scroll infinitely override this upwards — for them the scroll is the
    # pagination, so it is the only thing that decides how deep a visit gets.
    # The extension caps the total time either way, so a large number here
    # means "keep going until the list stops giving", not "hold the tab open".
    BROWSE_SCROLL_PASSES: int = 25
    # How often to check whether the browser has run out of work. Frequent is
    # fine: it does nothing unless the queue is nearly empty, so the interval
    # decides responsiveness rather than volume.
    BROWSE_TOPUP_INTERVAL_MINUTES: int = 30
    # Top up only when fewer than this many pages are still waiting. The queue
    # drains at one page every twenty seconds, so refilling a queue that is
    # still working would outrun the browser by an order of magnitude.
    BROWSE_TOPUP_BELOW: int = 10
    # An agent that has not polled in this long is treated as gone. Queueing
    # for a shut laptop fills the queue with tasks that expire unread, and
    # buries whatever real backlog is behind them.
    BROWSE_AGENT_STALE_HOURS: int = 24

    # --- Learned harvest recipes -------------------------------------------
    # When the generic reader makes nothing of a payload, keep a trimmed copy so
    # a recipe can be written from it. These are responses to a logged-in
    # session and can carry names and account ids, so they are capped per host
    # and expired rather than accumulated — a diagnostic, not an archive.
    HARVEST_SAMPLES_ENABLED: bool = True
    HARVEST_SAMPLES_PER_HOST: int = 5
    HARVEST_SAMPLE_TTL_DAYS: int = 30
    # Greenhouse's job-seeker board — every company on the platform rather than
    # one. Login-only, so only the browser can reach it.
    #
    # A setting rather than a constant because the location half of this URL is
    # not composable: it carries a name, a latitude, a longitude and a country
    # code that all have to agree, so substituting a location from the profile
    # would produce coordinates in Kansas labelled London. Set the filters on
    # the site, copy the address bar, and put `{q}` where the keyword is.
    #
    # A URL without `{q}` is crawled as-is, for a filter set that needs no
    # keyword. Comma-separated for several.
    BROWSE_GREENHOUSE_FEED: str = (
        "https://my.greenhouse.io/jobs/search?query={q}"
        "&location=United%20States&lat=39.71614&lon=-96.999246"
        "&location_type=country&country_short_name=US"
    )
    # Tsenta's recommendations feed. A setting for the same reason as the one
    # above, and one more: this board keeps its filters entirely in its own
    # state rather than in the address, so the URL cannot say what it is
    # showing. Changing what gets crawled means setting the filters on the site
    # and pasting whatever page you land on.
    BROWSE_TSENTA_FEED: str = (
        "https://dashboard.tsenta.com/dashboard/recommendations"
    )
    # Mark the session cookie `Secure`, so browsers only send it over HTTPS.
    # On by default. Set false ONLY while the deployment is still on plain
    # http:// — with it on, the browser accepts the cookie at login and then
    # refuses to send it back, which reads as an endless login loop rather than
    # as an error. Turn it back on the moment TLS is in front of the app.
    SESSION_COOKIE_SECURE: bool = True
    # Origins allowed to call the API cross-origin, comma-separated. The
    # extension's is `chrome-extension://<id>`, which is stable per install.
    CORS_ALLOW_ORIGINS: str = ""

    NVIDIA_NIM_API_KEY: str
    NVIDIA_NIM_BASE_URL: str
    NVIDIA_NIM_MODEL: str = "z-ai/glm-5.2"
    NVIDIA_NIM_RPM: int = 40  # requests per minute allowed by the API
    # Output ceiling for a matching call. Generous because the default model
    # reasons before answering: the scoring JSON itself is a couple of hundred
    # tokens, but a ceiling that only fits the answer truncates it mid-object
    # and the parse fails. Costs nothing when a model does not use it — only
    # tokens actually produced are generated.
    NIM_MATCH_MAX_TOKENS: int = 1536
    # How much of a posting the scoring prompt carries. Was 4,000 characters,
    # chosen when descriptions were mostly 500-character stubs and the ceiling
    # never bound; now that enrichment fetches the real text it was cutting off
    # mid-requirements, so the model judged skill and seniority fit against the
    # marketing half. Several times longer than the longest real posting — a
    # ceiling against a page that cleaned badly, not against job descriptions.
    MATCH_DESCRIPTION_CHARS: int = 24000

    # ---- LLM call log ---------------------------------------------------
    # Every request and reply, stored together. The existing log lines say a
    # call happened and how it ended — which the result already tells you. The
    # pair is what answers "was the prompt wrong or was the answer wrong".
    LLM_LOG_ENABLED: bool = True
    # Per-field ceiling. Prompts carry whole job descriptions and profiles, so
    # without one this table outgrows everything else in the schema.
    LLM_LOG_MAX_CHARS: int = 20000
    LLM_LOG_KEEP_ROWS: int = 2000
    LLM_LOG_PRUNE_INTERVAL_HOURS: int = 6

    # How many past verdicts to keep per job. Per job rather than per table on
    # purpose: the LLM log's global cap means a job's first evaluation is gone
    # within days on a pipeline making thousands of calls a week, and the first
    # evaluation is the one worth comparing against. Twenty covers a job
    # re-scored on every enrichment pass it will ever get.
    SCORE_HISTORY_KEEP_PER_JOB: int = 20

    # ---- FreeInference --------------------------------------------------
    # OpenAI-compatible, free daily credit for the research community. It goes
    # ahead of the paid providers in both chains: it cannot bill, and the chain
    # already falls through when a provider fails — including when the day's
    # credit runs out — so trying it first costs nothing but the attempt.
    FREEINFERENCE_API_KEY: str = ""
    FREEINFERENCE_BASE_URL: str = "https://freeinference.org/v1"
    FREEINFERENCE_MODEL: str = "glm-5.1"
    # Matching is high-volume JSON scoring, so it uses the faster sibling.
    FREEINFERENCE_MATCH_MODEL: str = "glm-5-turbo"
    # The endpoint accepts ONE request at a time. This app does not run one at
    # a time — two worker processes, matching overlapping generation by
    # design — so calls queue through a Redis gate rather than being refused.
    # Set 0 only if the limit is ever lifted; the gate costs a Redis round trip.
    FREEINFERENCE_MAX_CONCURRENCY: int = 1

    # Optional additional LLM providers. When configured, document generation
    # prefers quality-first (FreeInference -> Anthropic -> Gemini -> NIM) and
    # job matching uses them as failover (NIM -> FreeInference -> Gemini ->
    # Anthropic).
    ANTHROPIC_API_KEY: str = ""
    # Generation model (resumes/cover letters — the user-facing writing).
    # claude-opus-4-8: best quality, ~$0.11/application; claude-sonnet-5: ~$0.04.
    ANTHROPIC_MODEL: str = "claude-opus-4-8"
    # Matching-failover model (high-volume JSON scoring — cheap by design).
    ANTHROPIC_MATCH_MODEL: str = "claude-haiku-4-5"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    # Hard ceiling on PAID (non-NIM) matching calls per cycle. When NIM is down
    # and the cap is hit, remaining jobs simply stay `new` and retry next cycle.
    MAX_PAID_MATCH_CALLS_PER_CYCLE: int = 150

    # Which provider scores jobs first. "nim" is the historical behaviour and
    # the safe default; naming another configured provider ("freeinference",
    # "gemini", "anthropic") moves NIM to the end of the failover chain rather
    # than removing it.
    #
    # Worth knowing before switching to freeinference: it is the same free daily
    # credit document generation prefers, and matching is by far the higher
    # volume of the two. Spending the day's allowance on scoring means
    # generation falls through to whatever is next in its own chain.
    MATCH_PRIMARY: str = "nim"

    # ---- Second-opinion scoring ------------------------------------------
    # A fast model scores everything, and most of its answers are not close
    # calls: a 20 is a 20 and a 95 is a 95 whoever reads them. The band in the
    # middle is where accept and reject actually flip, and where a cheap
    # model's guess decides whether a job is ever seen — so those get scored
    # again by the strongest configured provider.
    #
    # Skipped entirely when nothing stronger than the primary is configured:
    # re-asking the same model the same question spends a call to hear the
    # same answer.
    DEEP_MATCH_ENABLED: bool = True
    DEEP_MATCH_BAND_LOW: int = 55
    DEEP_MATCH_BAND_HIGH: int = 85
    DEEP_MATCH_MAX_PER_CYCLE: int = 100

    # ---- Outreach -------------------------------------------------------
    HUNTER_IO_API_KEY: str = ""
    OUTREACH_ENABLED: bool = True
    # How many contacts one discovery run may store for an application. More
    # than a handful is noise — the point is two or three good people.
    OUTREACH_MAX_CONTACTS_PER_APP: int = 5
    # Use the LinkedIn people search (needs LINKEDIN_SESSION_COOKIE and costs a
    # browser launch). Off by default, and worth understanding before switching
    # on: it is an authenticated scrape from a datacenter IP, which is the
    # pattern LinkedIn restricts accounts for. The deep links below get you to
    # the same profiles with no account risk at all.
    OUTREACH_USE_LINKEDIN: bool = False
    # Hard ceiling on people searches per discovery run. The account risk scales
    # with volume, so this stays low whatever else is configured.
    OUTREACH_LINKEDIN_MAX_SEARCHES: int = 1

    # Public members of the company's GitHub org — real engineers with names and
    # often a published email. Needs a token: unauthenticated GitHub allows 60
    # requests an hour, which one cycle exhausts.
    OUTREACH_USE_GITHUB: bool = True
    GITHUB_TOKEN: str = ""
    # Mine the company's own /team and /about pages for LinkedIn profile links
    # and published addresses. No key, no quota.
    OUTREACH_USE_TEAM_PAGES: bool = True
    # Titles the LinkedIn search looks for, in priority order.
    OUTREACH_TARGET_TITLES: str = (
        "technical recruiter,talent acquisition,engineering manager,hiring manager"
    )
    # Derive likely addresses (first.last@domain and friends) when nobody hands
    # us a real one. Guesses are stored as email_status="guessed" and never sent
    # automatically.
    OUTREACH_GUESS_EMAILS: bool = True
    # Spend a Hunter verifier credit per discovered address.
    OUTREACH_VERIFY_EMAILS: bool = False
    # Days after a message is sent before its follow-up comes due, one entry per
    # step. Running out of entries ends the sequence.
    OUTREACH_FOLLOWUP_DAYS: str = "4,7,10"
    # Draft due follow-ups automatically on the beat schedule. Drafts only —
    # nothing is ever sent without an explicit click.
    OUTREACH_AUTO_DRAFT_FOLLOWUPS: bool = True
    OUTREACH_FOLLOWUP_INTERVAL_HOURS: int = 6  # how often the scheduler looks

    # SMTP. Sending stays off until OUTREACH_SEND_ENABLED is flipped on AND a
    # host is configured, so a misconfigured deploy cannot mail strangers.
    OUTREACH_SEND_ENABLED: bool = False
    OUTREACH_MAX_SENDS_PER_DAY: int = 20
    OUTREACH_ATTACH_DOCUMENTS: bool = True  # attach the current resume/cover letter
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True   # STARTTLS on a submission port
    SMTP_USE_SSL: bool = False  # implicit TLS (port 465); takes precedence
    SMTP_FROM_EMAIL: str = ""   # defaults to the profile's email
    SMTP_FROM_NAME: str = ""    # defaults to the profile's name
    SMTP_TIMEOUT: int = 30

    # IMAP. Reading the mailbox is what makes reply and bounce detection a
    # mechanism rather than a habit — OUTREACH_AUTO_DRAFT_FOLLOWUPS is on by
    # default, and without this the guard against chasing someone who already
    # answered depends on remembering to click a button. Off until switched on.
    IMAP_ENABLED: bool = False
    IMAP_HOST: str = ""          # imap.gmail.com for Gmail/Workspace
    IMAP_PORT: int = 993
    IMAP_USERNAME: str = ""      # defaults to SMTP_USERNAME
    IMAP_PASSWORD: str = ""      # defaults to SMTP_PASSWORD (a Gmail app password)
    IMAP_FOLDER: str = "INBOX"
    IMAP_TIMEOUT: int = 30
    # How far back the first poll looks. Later polls resume from the last UID,
    # so this only bounds the initial scan of what may be years of mail.
    IMAP_LOOKBACK_DAYS: int = 14
    IMAP_MAX_MESSAGES_PER_POLL: int = 200
    IMAP_POLL_INTERVAL_MINUTES: int = 15

    # Job source API keys
    ADZUNA_APP_ID: str = ""
    ADZUNA_APP_KEY: str = ""
    JSEARCH_API_KEY: str = ""
    LINKEDIN_SESSION_COOKIE: str = ""
    HANDSHAKE_SESSION_COOKIE: str = ""
    GREENHOUSE_COMPANY_SLUGS: str = ""
    LEVER_COMPANY_SLUGS: str = ""
    ASHBY_COMPANY_SLUGS: str = ""
    SMARTRECRUITERS_COMPANY_SLUGS: str = ""
    WORKABLE_COMPANY_SLUGS: str = ""
    RECRUITEE_COMPANY_SLUGS: str = ""
    # Each ATS we speak makes every company hosted on it reachable, and the
    # discovery flywheel starts finding boards for it automatically in job
    # links and career pages. Empty is fine: discovery fills them in.
    ICIMS_COMPANY_SLUGS: str = ""
    BAMBOOHR_COMPANY_SLUGS: str = ""
    TEAMTAILOR_COMPANY_SLUGS: str = ""
    JOBVITE_COMPANY_SLUGS: str = ""
    PERSONIO_COMPANY_SLUGS: str = ""
    WORKDAY_TENANTS: str = ""  # comma-separated tenant:host:site, e.g. nvidia:wd5:NVIDIAExternalCareerSite
    JOOBLE_API_KEY: str = ""
    FINDWORK_API_KEY: str = ""
    CAREERJET_AFFID: str = ""
    # USAJOBS identifies callers by the email they registered with as well as
    # by the key, and 401s a request carrying only one of the two.
    USAJOBS_API_KEY: str = ""
    USAJOBS_USER_AGENT: str = ""   # the registered email address
    USAJOBS_MAX_PAGES: int = 2
    # hiring.cafe indexes ATS boards directly rather than other job boards, so
    # its postings carry full descriptions and link at the employer.
    HIRINGCAFE_ENABLED: bool = True
    # Y Combinator's public role pages (a fixed taxonomy, not search queries).
    YC_ENABLED: bool = True
    YC_ROLES: str = ""             # blank uses sources.ycombinator.DEFAULT_ROLES

    # A source that has failed every run for this many cycles is skipped rather
    # than called again — an expired key answers identically forever, and the
    # error line it produces only trains everyone to ignore error lines. One
    # probe still goes out every `RETRY` runs, so a refreshed key resumes on
    # its own without anybody re-enabling anything.
    SOURCE_REST_AFTER_FAILURES: int = 10
    SOURCE_REST_RETRY_EVERY: int = 10
    ATS_AUTO_DISCOVERY: bool = True  # learn company ATS boards from fetched job links
    ATS_SEED_COMPANIES: bool = True  # include the verified seed list of known tech companies
    ATS_SLUG_VALIDATION: bool = True  # validate/auto-fix configured slugs against the ATS APIs
    ATS_LIST_HARVEST: bool = True  # harvest company slugs from community job lists
    ATS_BOARD_REGISTRY: bool = True  # persist discovered boards and rank them by yield
    ATS_MAX_SLUGS_PER_ATS: int = 300  # per-cycle slug budget per ATS (tighter caps still apply)
    ATS_BOARD_FETCH_WORKERS: int = 8  # concurrent per-company board fetches
    ATS_BOARD_MAX_EMPTY_CYCLES: int = 8  # retire a discovered board after this many silent cycles
    # Discovery reads a slug out of a link and files it as a company, which is
    # a guess. Probing before polling is what stops `greenhouse/linkedin` and
    # `greenhouse/appcast` from spending the budget real companies compete for.
    ATS_BOARD_VALIDATION: bool = True
    ATS_BOARD_VALIDATE_PER_CYCLE: int = 150

    # Aggregators (Adzuna, Jooble, Careerjet) link to their own redirect page
    # rather than the employer. Following those once per new posting yields the
    # real apply URL and exposes the company's ATS board to discovery.
    RESOLVE_APPLY_LINKS: bool = True
    # Was 400, which left 79,705 aggregator links never resolved at all — a
    # backlog that grows faster than the budget drains it never drains. The
    # real limit is politeness per host, below; this only stops one cycle from
    # running unboundedly long.
    LINK_RESOLVE_MAX_PER_CYCLE: int = 5000
    LINK_RESOLVE_WORKERS: int = 16
    # Concurrent requests to any one host, and the minimum gap between them.
    # A backlog that is 90% Adzuna paces itself while everything else runs flat
    # out.
    LINK_RESOLVE_PER_HOST: int = 4
    LINK_RESOLVE_HOST_DELAY_MS: int = 250
    # Sniff company careers sites (careers.acme.com) for an embedded ATS board.
    ATS_SNIFF_CAREER_SITES: bool = True
    ATS_SNIFF_MAX_HOSTS_PER_CYCLE: int = 40

    # Jobs stored before the registry existed were never mined for the company
    # boards hiding in their descriptions. The first fetch cycle after deploy
    # does that once; these cap the extra requests it costs that one cycle.
    # Sized so the worst case (every request timing out) stays a few minutes
    # rather than stalling the cycle for twenty.
    BOARD_BACKFILL_ON_START: bool = True
    BOARD_BACKFILL_MAX_LINKS: int = 400
    BOARD_BACKFILL_MAX_HOSTS: int = 120
    BOARD_BACKFILL_WORKERS: int = 16

    # LinkedIn guest API. The endpoint pages in blocks of 10, and jobs without a
    # description are dropped by the skill filter, so these two caps set the
    # source's real yield.
    LINKEDIN_MAX_PAGES: int = 15
    LINKEDIN_RECENCY_HOURS: int = 168  # 0 disables the freshness filter
    # A politeness ceiling, not a ration. The old 200 was spent before the
    # title gate ran, mostly on jobs that died at it moments later — which is
    # why 8,800 stored LinkedIn jobs have no description. The gate runs first
    # now, and everything that survives it gets one.
    LINKEDIN_MAX_DETAIL_FETCHES: int = 2000
    LINKEDIN_DETAIL_WORKERS: int = 4

    # Adzuna pages 50 results at a time; a 1-day window threw most of them away.
    ADZUNA_MAX_PAGES: int = 3
    ADZUNA_MAX_DAYS_OLD: int = 7

    # Indeed retired its public RSS feed — every query 404s. Off by default so
    # it stops burning requests; flip on if the feed ever comes back.
    INDEED_RSS_ENABLED: bool = False
    ARBEITNOW_MAX_PAGES: int = 3

    # Wellfound role pages are a fixed taxonomy (wellfound.com/role/<slug>), so
    # they're listed explicitly rather than derived from expanded search
    # queries, which would mostly request pages that don't exist.
    WELLFOUND_ROLES: str = (
        "software-engineer,full-stack-engineer,backend-engineer,mobile-engineer"
    )
    # Wellfound has served this server empty responses through a browser, and
    # every one of the 140 jobs it ever stored arrived with no description at
    # all. Off until that changes: a Chromium launch per cycle is the most
    # expensive thing in the fetch, and it has never bought a usable posting.
    WELLFOUND_ENABLED: bool = False
    # Dice's search results carry titles and links but no descriptions — those
    # live on the job-detail pages, which enrichment fetches. Here so the whole
    # browser tier can be switched off from the settings page if it stops
    # earning its keep.
    DICE_ENABLED: bool = True
    # Community job lists, mined for the ATS slugs in their apply links. Each
    # one is a few thousand company boards for the cost of a single request,
    # which makes it by far the cheapest discovery we have — the three original
    # lists alone yielded 212 boards on one cycle.
    # Every URL here was checked to return 200 before being added; four
    # plausible-looking ones did not and are deliberately absent, because a
    # dead list costs a request and a warning line every single cycle forever.
    SLUG_HARVEST_URLS: str = (
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md,"
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md,"
        "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/README.md,"
        "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/README.md,"
        "https://raw.githubusercontent.com/speedyapply/2026-AI-College-Jobs/main/README.md"
    )

    # ---- Enrichment ------------------------------------------------------
    # Going back for the description the source didn't send. Adzuna truncates
    # at 500 chars, LinkedIn ships 90% of its jobs without one, and ~25k jobs
    # were auto-rejected for thin data rather than for being bad jobs.
    ENRICH_ENABLED: bool = True
    # Per pass, not per day. The backlog drains over days; a pass with no
    # ceiling holds a worker slot for hours while the jobs it already rescued
    # wait behind it to be scored.
    ENRICH_MAX_PER_RUN: int = 200
    ENRICH_INTERVAL_MINUTES: int = 30
    # A pass at the end of each fetch cycle, so the jobs that just arrived are
    # scored on their real descriptions rather than on the stub the aggregator
    # sent. Smaller than a scheduled pass: the cycle is already long, and the
    # backlog is the scheduled pass's job.
    ENRICH_ON_FETCH: bool = True
    ENRICH_MAX_PER_FETCH: int = 150
    ENRICH_WORKERS: int = 8
    # The only real budget: concurrent requests to one host, and the gap
    # between them.
    ENRICH_PER_HOST: int = 4
    ENRICH_HOST_DELAY_MS: int = 400
    # Queue the next batch as soon as one fills up, instead of idling until the
    # next scheduled pass. A 200-job batch takes under a minute, so the old
    # behaviour spent 29 of every 30 minutes doing nothing while a six-figure
    # backlog waited.
    ENRICH_CHAIN_PASSES: bool = True
    # A ceiling on one chain, so a bug cannot turn this into a permanent loop.
    # The queue shrinks on its own (every attempt is stamped), so this is belt
    # and braces rather than the thing that ends the chain.
    ENRICH_MAX_CHAINED_PASSES: int = 50
    # How long before a job that could not be enriched is worth trying again.
    # A cooloff rather than a write-off: a host that was refusing us last week
    # may not be next week. Without it the same unenrichable jobs sit at the
    # head of a newest-first queue and no pass ever reaches the real backlog.
    ENRICH_RETRY_DAYS: int = 7
    # Ceiling on browser tasks waiting at once. A browser drains these at human
    # pace, so queueing faster than it can drain makes nothing arrive sooner —
    # it only builds a backlog large enough that most of it expires unread.
    ENRICH_MAX_BROWSER_OUTSTANDING: int = 500

    # What zone the pages render times in. Storage stays UTC — this is purely
    # a rendering concern. An IANA name rather than an offset, so the PST/PDT
    # switch is handled instead of being an hour wrong two thirds of the year.
    DISPLAY_TIMEZONE: str = "America/Los_Angeles"

    DEBUG: bool = False
    STORAGE_PATH: str = "/storage"
    DOCS_OUTPUT_DIR: str = "/storage"
    MIN_MATCH_SCORE: int = 70
    # Kept for the combined cycle and for anything still reading it; the
    # scheduled work is the three group intervals below.
    FETCH_INTERVAL_HOURS: int = 5

    # ---- Fetch groups ----------------------------------------------------
    # One 47-minute task fetched everything, so an API source that could
    # refresh hourly ran on the schedule of a Chromium launch. Each slice now
    # has its own cadence, its own lock and its own history row.
    FETCH_API_INTERVAL_HOURS: int = 2
    FETCH_BOARDS_INTERVAL_HOURS: int = 5
    FETCH_BROWSER_INTERVAL_HOURS: int = 12
    # Boards asked over their own API with a stored credential. Cheap — one
    # request per page of twenty — and the board it exists for is personalised,
    # so it changes on the site's schedule rather than ours. Three hours is
    # frequent enough to catch a day's new recommendations and slow enough to
    # be unremarkable traffic.
    FETCH_LINKED_INTERVAL_HOURS: int = 3
    # The browser tier is the one worth being able to switch off wholesale: it
    # is the most expensive thing in the pipeline and the least productive.
    BROWSER_TIER_ENABLED: bool = True

    # ---- Matching cadence ------------------------------------------------
    # Matching used to run only as a tail-call from a fetch cycle, so anything
    # it did not get through — a worker restarted mid-pass, a backlog larger
    # than one pass, a provider that was down — waited hours for the next
    # fetch, which looks exactly like matching being broken. A sweep of its own
    # means "still `new`" is always temporary.
    MATCH_INTERVAL_MINUTES: int = 20
    # Jobs per match task. A pass over hundreds of jobs is minutes of LLM calls
    # holding one of two worker slots, during which nothing else — including
    # the document generation it just queued — can run. Bounded batches that
    # re-queue themselves keep the queue moving and make progress durable:
    # a restart loses at most one batch.
    MATCH_MAX_JOBS_PER_TASK: int = 25
    # An application whose generation has been running longer than this had its
    # worker killed — Celery lost the task, and nothing was ever going to
    # retry it. The sweeper re-queues those.
    GENERATION_STUCK_MINUTES: int = 20

    # Documents written before enrichment brought the real posting in were
    # tailored to a teaser. This rewrites them on a clock instead of waiting
    # for the user to notice the badge — but only for applications they have
    # not acted on; see `services.doc_refresh`.
    DOC_REFRESH_ENABLED: bool = True
    DOC_REFRESH_INTERVAL_HOURS: int = 6
    # Bounded per pass. The first run after enrichment has been going for a
    # while can find hundreds, and queueing all of them means the documents for
    # the job the user is looking at right now wait behind refreshes for jobs
    # they are not.
    DOC_REFRESH_MAX_PER_RUN: int = 25

    # Read the draft back as the recruiter would before compiling it. Document
    # generation is the one step whose output a human actually reads and the
    # only one that never got a second look. See `services.self_review`.
    SELF_REVIEW_ENABLED: bool = True

    # What the browser extension did. Rows are small — a kind, a host and a few
    # counts — so this keeps far more than the LLM log, which carries whole
    # prompts. The questions it answers ("is the extension even running", "which
    # hosts is it failing on") are questions about weeks.
    AGENT_EVENT_KEEP_ROWS: int = 20000
    AGENT_EVENT_PRUNE_INTERVAL_HOURS: int = 12
    # Finished browser tasks carry the page they brought back, which is the
    # large part. The countable history now lives in `agent_events`, so the task
    # row only needs to outlive anyone's interest in the detail.
    BROWSER_TASK_KEEP_DAYS: int = 14

    # A nightly dump, on this machine and nowhere else. This protects against
    # the failures that actually happen here — a bad migration, a DROP in the
    # wrong shell, a container rebuilt with the volume detached. It does not
    # protect against losing the machine; see `services.backups`.
    BACKUP_ENABLED: bool = True
    BACKUP_DIR: str = "/storage/backups"
    BACKUP_INTERVAL_HOURS: int = 24
    # Two weeks. Long enough that damage done on a Friday and noticed the
    # following Monday week is still recoverable, which is the realistic
    # detection lag for a system nobody watches full time.
    BACKUP_KEEP: int = 14

    # Settled rejections stop carrying their descriptions after this long. They
    # are moved rather than deleted: deduplication reads three columns off the
    # tombstone, and losing them means re-fetching and re-scoring the same
    # posting forever. See `services.archive`.
    ARCHIVE_ENABLED: bool = True
    ARCHIVE_AFTER_DAYS: int = 60
    # Bounded per pass: the first run has a six-figure backlog, and one
    # transaction that size holds a worker and a lock for the duration.
    ARCHIVE_MAX_PER_RUN: int = 5000
    ARCHIVE_INTERVAL_HOURS: int = 24

    MIN_KEYWORD_SKILLS: int = 2
    MAX_JOB_AGE_DAYS: int = 30  # skip fetched jobs posted longer ago than this (0 disables)
    FILTER_SENIOR_TITLES: bool = True  # prefilter Senior/Staff/... titles for junior candidates
    JUNIOR_MAX_YEARS: float = 3.0  # candidate is "junior" below this many years of experience

    # Postings written in a language you don't read. Arbeitnow returns German
    # listings with English titles, so the title gate passes them and the model
    # is then asked to score a description nobody involved can act on.
    FILTER_BY_LANGUAGE: bool = True
    # Comma-separated ISO 639-1 codes. Env-only rather than a tunable: it is a
    # fact about the person, not a dial you turn while reading results.
    MATCH_LANGUAGES: str = "en"

    # ---- Posting liveness -----------------------------------------------
    # Re-check matched/docs-generated jobs against the employer's page and
    # mark the ones that closed, so a dead posting wears a badge instead of
    # silently wasting an application. Conservative by design: only a 404, an
    # explicit "no longer accepting applications", or a known ATS bouncing to
    # its board index counts — ambiguity never closes a job.
    LIVENESS_ENABLED: bool = True
    LIVENESS_INTERVAL_HOURS: int = 12
    LIVENESS_MAX_PER_CYCLE: int = 200   # postings checked per sweep
    LIVENESS_WORKERS: int = 8
    LIVENESS_RECHECK_DAYS: int = 3      # how long a verdict stands before re-checking


settings = Settings()
