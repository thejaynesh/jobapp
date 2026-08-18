import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.database import Base
from app.config import settings
import app.models  # noqa: F401 — registers all models with Base.metadata before create_all

TEST_DB_URL = settings.TEST_DATABASE_URL or settings.DATABASE_URL.replace(
    "/jobapp", "/jobapp_test"
)

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
