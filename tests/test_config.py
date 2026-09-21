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


class TestTheTestDatabaseUrlKeepsItsPassword:
    """
    `conftest` round-trips the database URL through SQLAlchemy to derive the
    test database and each xdist worker's copy of it. `str(url)` renders the
    password as `***`, so every one of those helpers was handing
    `create_engine` the literal password `***`.

    It cost a CI run to find, and it hid for as long as it did because it only
    fails against a Postgres that checks passwords: a local server with `trust`
    in `pg_hba.conf` accepts `***` as readily as the real secret, so the suite
    passed on a dev machine and failed on the first run against a container.
    The error it produces — `password authentication failed for user "jobapp"`
    — names the user, which reads like a missing role rather than a mangled
    secret.

    No database needed, so this guard runs everywhere the suite does.
    """

    SECRET = "s3cr3t-not-three-stars"

    def test_deriving_the_test_database_keeps_it(self):
        from tests.conftest import _derive_test_url

        derived = _derive_test_url(
            f"postgresql://jobapp:{self.SECRET}@db:5432/jobapp"
        )

        assert self.SECRET in derived
        assert "***" not in derived
        # And still renames only the database, which is the other half of what
        # this helper exists for.
        assert derived.endswith("/jobapp_test")
        assert "jobapp_test:" not in derived   # not the username

    def test_the_per_worker_database_keeps_it(self):
        from tests.conftest import _worker_db_url

        derived = _worker_db_url(
            f"postgresql://jobapp:{self.SECRET}@db:5432/jobapp_test", "gw7"
        )

        assert self.SECRET in derived
        assert "***" not in derived
        assert derived.endswith("/jobapp_test_gw7")

    def test_a_serial_run_is_left_alone(self):
        from tests.conftest import _worker_db_url

        base = f"postgresql://jobapp:{self.SECRET}@db:5432/jobapp_test"

        assert _worker_db_url(base, "") == base

    def test_the_admin_url_would_keep_it_too(self):
        """
        `_ensure_database` swaps the database for `postgres` to issue CREATE
        DATABASE. That is the call site that actually failed in CI, and it is
        the same round-trip.
        """
        from sqlalchemy.engine import make_url

        from tests.conftest import _render

        url = make_url(f"postgresql://jobapp:{self.SECRET}@db:5432/jobapp_test")
        rendered = _render(url.set(database="postgres"))

        assert self.SECRET in rendered
        assert rendered.endswith("/postgres")

    def test_a_url_with_no_password_is_not_corrupted(self):
        """Trust auth and unix sockets are both normal; neither grows a `***`."""
        from tests.conftest import _derive_test_url

        derived = _derive_test_url("postgresql://jobapp@/jobapp")

        assert "***" not in derived
        assert "None" not in derived
