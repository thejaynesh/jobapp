import os
import pytest
from app.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "testsecret")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "testkey")
    monkeypatch.setenv("NVIDIA_NIM_BASE_URL", "https://api.nvidia.com/v1")
    monkeypatch.setenv("NVIDIA_NIM_MODEL", "meta/llama-3.1-70b-instruct")

    settings = Settings()

    assert settings.DATABASE_URL == "postgresql://u:p@localhost/db"
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.MIN_MATCH_SCORE == 70
    assert settings.MIN_KEYWORD_SKILLS == 2
    assert settings.FETCH_INTERVAL_HOURS == 5


def test_settings_defaults():
    s = Settings(
        DATABASE_URL="postgresql://u:p@localhost/db",
        REDIS_URL="redis://localhost:6379/0",
        SECRET_KEY="s",
        NVIDIA_NIM_API_KEY="k",
        NVIDIA_NIM_BASE_URL="https://api.nvidia.com/v1",
        NVIDIA_NIM_MODEL="meta/llama-3.1-70b-instruct",
    )
    assert s.MIN_MATCH_SCORE == 70
    assert s.STORAGE_PATH == "/storage"
    assert s.DEBUG is False
