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

    HUNTER_IO_API_KEY: str = ""

    DEBUG: bool = False
    STORAGE_PATH: str = "/storage"
    MIN_MATCH_SCORE: int = 70
    FETCH_INTERVAL_HOURS: int = 5
    MIN_KEYWORD_SKILLS: int = 2


settings = Settings()
