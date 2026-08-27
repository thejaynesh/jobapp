import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.database import Base
from app.config import settings
import app.models  # noqa: F401 — registers all models with Base.metadata before create_all

_BASE_DB_URL = settings.TEST_DATABASE_URL or settings.DATABASE_URL.replace(
    "/jobapp", "/jobapp_test"
)

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
    return str(url.set(database=f"{url.database}_{worker}"))


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
        str(target.set(database="postgres")), isolation_level="AUTOCOMMIT",
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
