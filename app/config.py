from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    TEST_DATABASE_URL: str = ""
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
    NVIDIA_NIM_MODEL: str = "meta/llama-3.3-70b-instruct"
    NVIDIA_NIM_RPM: int = 40  # requests per minute allowed by the API

    # Optional additional LLM providers. When configured, document generation
    # prefers quality-first (Anthropic -> Gemini -> NIM) and job matching uses
    # them as failover (NIM -> Gemini -> Anthropic).
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
    WORKDAY_TENANTS: str = ""  # comma-separated tenant:host:site, e.g. nvidia:wd5:NVIDIAExternalCareerSite
    JOOBLE_API_KEY: str = ""
    FINDWORK_API_KEY: str = ""
    CAREERJET_AFFID: str = ""
    ATS_AUTO_DISCOVERY: bool = True  # learn company ATS boards from fetched job links
    ATS_SEED_COMPANIES: bool = True  # include the verified seed list of known tech companies
    ATS_SLUG_VALIDATION: bool = True  # validate/auto-fix configured slugs against the ATS APIs
    ATS_LIST_HARVEST: bool = True  # harvest company slugs from community job lists
    ATS_BOARD_REGISTRY: bool = True  # persist discovered boards and rank them by yield
    ATS_MAX_SLUGS_PER_ATS: int = 300  # per-cycle slug budget per ATS (tighter caps still apply)
    ATS_BOARD_FETCH_WORKERS: int = 8  # concurrent per-company board fetches
    ATS_BOARD_MAX_EMPTY_CYCLES: int = 8  # retire a discovered board after this many silent cycles

    # Aggregators (Adzuna, Jooble, Careerjet) link to their own redirect page
    # rather than the employer. Following those once per new posting yields the
    # real apply URL and exposes the company's ATS board to discovery.
    RESOLVE_APPLY_LINKS: bool = True
    LINK_RESOLVE_MAX_PER_CYCLE: int = 400
    LINK_RESOLVE_WORKERS: int = 8
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
    LINKEDIN_MAX_PAGES: int = 5
    LINKEDIN_RECENCY_HOURS: int = 168  # 0 disables the freshness filter
    LINKEDIN_MAX_DETAIL_FETCHES: int = 200  # per cycle, not per search
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
    # Wellfound has served this server empty responses through a browser. Set
    # false to stop paying for the attempt (including a Chromium launch) if it
    # turns out to be blocked for good.
    WELLFOUND_ENABLED: bool = True
    SLUG_HARVEST_URLS: str = (
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md,"
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md,"
        "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/README.md"
    )

    DEBUG: bool = False
    STORAGE_PATH: str = "/storage"
    DOCS_OUTPUT_DIR: str = "/storage"
    MIN_MATCH_SCORE: int = 70
    FETCH_INTERVAL_HOURS: int = 5
    MIN_KEYWORD_SKILLS: int = 2
    MAX_JOB_AGE_DAYS: int = 30  # skip fetched jobs posted longer ago than this (0 disables)
    FILTER_SENIOR_TITLES: bool = True  # prefilter Senior/Staff/... titles for junior candidates
    JUNIOR_MAX_YEARS: float = 3.0  # candidate is "junior" below this many years of experience


settings = Settings()
