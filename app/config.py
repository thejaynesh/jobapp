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
    ANTHROPIC_MODEL: str = "claude-opus-4-8"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

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
    JOOBLE_API_KEY: str = ""
    FINDWORK_API_KEY: str = ""
    ATS_AUTO_DISCOVERY: bool = True  # learn company ATS boards from fetched job links

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
