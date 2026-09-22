import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.database import Base
from app.config import settings
import app.models  # noqa: F401 — registers all models with Base.metadata before create_all


def _derive_test_url(base: str) -> str:
    """
    The test database, derived from the app one when nothing names it.

    Not `base.replace("/jobapp", "/jobapp_test")`, which is what this was.
    `str.replace` is global and a URL like
    `postgresql://jobapp:jobapp@host/jobapp` contains `/jobapp` in the
    *userinfo* as well as in the path — so the fallback renamed the role too
    and failed with `role "jobapp_test" does not exist`. Masked in practice
    because `.env.example` sets `TEST_DATABASE_URL` explicitly, which is
    exactly the kind of thing that stays masked until someone runs the suite
    without it.
    """
    url = make_url(base)
    return _render(url.set(database=f"{url.database or 'jobapp'}_test"))


def _render(url) -> str:
    """
    A URL back to a string **with its password intact**.

    `str(url)` does not do this. SQLAlchemy's `URL.__str__` renders the
    password as `***`, so every helper here that round-tripped a URL through
    `str()` handed `create_engine` the literal password `***` — and the engine
    then failed with `password authentication failed for user "jobapp"`,
    naming the user, which reads like a missing role rather than a mangled
    secret.

    It stayed hidden because it only shows up against a Postgres that checks
    passwords. A local server with `trust` in `pg_hba.conf`, or a connection
    over the unix socket, accepts `***` as happily as the real thing — so the
    suite passed on a dev machine and on CI's first run failed twenty workers
    deep with an auth error.
    """
    return url.render_as_string(hide_password=False)


_BASE_DB_URL = settings.TEST_DATABASE_URL or _derive_test_url(settings.DATABASE_URL)

# One database per xdist worker.
#
# The suite takes about fifteen minutes on one core, which is long enough that
# it stops being run — and a check that is skipped protects nothing. `-n auto`
# cuts it to roughly a quarter of that, but only if the workers stop sharing a
# schema: `setup_test_db` runs per worker, so with one database the second
# worker's `create_all` races the first's and the first worker to finish drops
# the tables out from under everyone still running.
#
# So each worker gets its own database, created here if it is not there yet.
# Serial runs are untouched — no worker id means the original name.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")


def _worker_db_url(base: str, worker: str) -> str:
    if not worker:
        return base
    url = make_url(base)
    return _render(url.set(database=f"{url.database}_{worker}"))


TEST_DB_URL = _worker_db_url(_BASE_DB_URL, _WORKER)


def _ensure_database(url: str) -> None:
    """
    Create this worker's database if it does not exist.

    Connects to `postgres` rather than the target, because you cannot create a
    database from inside itself. Tolerant of the race between workers starting
    at the same moment: two of them can both see it missing, and the loser of
    `CREATE DATABASE` gets an error that means the database is now there, which
    is what it wanted.
    """
    target = make_url(url)
    if target.get_backend_name() != "postgresql":
        return

    admin = create_engine(
        _render(target.set(database="postgres")), isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                try:
                    conn.execute(text(f'CREATE DATABASE "{target.database}"'))
                except Exception:
                    pass  # another worker won the race; that is a success here
    finally:
        admin.dispose()


if _WORKER:
    _ensure_database(TEST_DB_URL)

test_engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db():
    """
    A session whose commits are rolled back when the test ends.

    `join_transaction_mode="create_savepoint"` is what lets application code use
    its own `begin_nested()` savepoints (the job fetcher wraps the board
    registry in one) — the older after_transaction_end recipe fought with them.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = TestSessionLocal(
        bind=connection, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _auth_disabled_by_default(monkeypatch):
    """
    Route tests exercise their own subject, not the front door.

    Authentication is enforced by middleware in front of every route, so leaving
    it on would make every existing route test a login test. `tests/test_auth.py`
    turns it back on explicitly for the tests that are about the gate itself.
    """
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)


@pytest.fixture(autouse=True)
def _slug_harvest_off_by_default(monkeypatch):
    """
    Community slug lists are five live README downloads from GitHub.

    `ATS_LIST_HARVEST` defaults to on, so any test that runs a fetch cycle was
    fetching them for real — which is slow, and worse, non-deterministic in a
    way that reads as an unrelated bug. `TestAtsDiscoveryWiring` asserts that a
    slug mined from a job description lands in `discovered_ats`; when the
    download succeeds it brings back hundreds of real slugs, the merge is
    capped, and the one the test is about is pushed out. When the network is
    slow or the requests fail the harvest returns nothing and the test passes.
    So it passed or failed on whether GitHub answered, which is not something
    this test is about.

    Same reasoning as `_board_validation_off_by_default` below, and the same
    remedy. `tests/test_slug_mining.py` exercises the harvest directly with the
    HTTP call stubbed.
    """
    monkeypatch.setattr(settings, "ATS_LIST_HARVEST", False)


@pytest.fixture(autouse=True)
def _age_cutoff_off_by_default(monkeypatch):
    """
    The jobs list's age window, off unless a test is about it.

    `DASHBOARD_MAX_AGE_DAYS` hides jobs fetched more than twenty days ago, and
    almost every fixture in this suite stamps `fetched_at` with a hardcoded
    date — `datetime(2026, 8, 3)` and friends. Those are already outside the
    window and drift further outside it every month, so leaving the cutoff on
    made seventeen tests about sorting, sources, filter reasons and posted-date
    labels fail on their fixture's age instead of on what they assert. Worse,
    they would have started failing on a date rather than on a commit.

    So the window is a product default, not a test default, and the tests that
    are actually about it set the value themselves — see
    `tests/test_tracker_ui.py::TestTheAgeWindow`.
    """
    monkeypatch.setattr(settings, "DASHBOARD_MAX_AGE_DAYS", 0)


@pytest.fixture(autouse=True)
def _no_dns_in_the_url_guard(monkeypatch):
    """
    `url_safety` resolves a host before letting a posting's URL be fetched.

    Real DNS is network, which no test may touch — and the fixtures use made-up
    hosts ("acme.example", "boards.greenhouse.io") that should read as public.
    Every name resolves to a public address (example.com's) here; the
    guard's own tests replace this to exercise private answers.
    """
    from app.services import url_safety

    monkeypatch.setattr(url_safety, "_resolve", lambda host: ["93.184.216.34"])
    url_safety.is_public_host.cache_clear()


@pytest.fixture(autouse=True)
def _board_validation_off_by_default(monkeypatch):
    """
    Board validation probes real ATS APIs, one request per unproven board.

    That is right in production and wrong in a test: a fetch cycle records
    hundreds of boards from the community slug lists, and validating them would
    put hundreds of live requests inside a unit test. `tests/test_company_boards
    .py` exercises the validation logic directly with the probe stubbed.
    """
    monkeypatch.setattr(settings, "ATS_BOARD_VALIDATION", False)


@pytest.fixture
def client(db):
    from app.main import app
    from app.database import get_db

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
