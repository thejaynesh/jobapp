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


def test_the_default_matching_model_is_glm():
    from app.config import Settings

    assert Settings.model_fields["NVIDIA_NIM_MODEL"].default == "z-ai/glm-5.2"


def test_the_default_model_is_one_the_picker_offers():
    # A default the Settings dropdown cannot represent would silently reset
    # itself the first time anyone opened that page.
    from app.config import Settings
    from app.services.tunables import TUNABLES

    picker = next(t for t in TUNABLES if t.key == "nvidia_nim_model")
    assert Settings.model_fields["NVIDIA_NIM_MODEL"].default in picker.choices


def test_the_matching_ceiling_leaves_room_for_thinking():
    # A reasoning model spends tokens before it answers; a ceiling sized for
    # the JSON alone truncates it mid-object and the parse fails, which reads
    # as the model being bad at scoring rather than as a budget.
    from app.config import Settings

    assert Settings.model_fields["NIM_MATCH_MAX_TOKENS"].default >= 1024
