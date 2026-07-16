from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    TEST_DATABASE_URL: str = ""
    REDIS_URL: str
    SECRET_KEY: str

    NVIDIA_NIM_API_KEY: str
    NVIDIA_NIM_BASE_URL: str
    NVIDIA_NIM_MODEL: str = "meta/llama-3.1-70b-instruct"
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

    HUNTER_IO_API_KEY: str = ""

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
