"""
The engine, and how many connections it is allowed.

Pool sizing is a real setting rather than SQLAlchemy's default of 5 + 10,
because this application holds connections in places a plain CRUD app does not:
an agent long-polls for work, HTMX fragments refresh independently of the page
around them, and several panels query on every render. Fifteen goes quickly
once a background browser is talking to it as well.

The rule the code follows, which matters more than the number: never hold a
pooled connection across a network call to someone else. Commit first, then go
out to the internet, then come back for one. A connection idling through a
minute of HTTP is a connection nobody else can have, and the symptom — every
page timing out at once — points nowhere near the request that caused it.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # Wait rather than fail instantly under a burst, but not so long that a
    # wedged connection turns a page into a hang. Exhaustion should surface.
    pool_timeout=settings.DB_POOL_TIMEOUT,
    # Postgres and connection proxies drop idle connections; recycling below
    # any such window avoids handing out one that is already closed.
    pool_recycle=settings.DB_POOL_RECYCLE,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def pool_status() -> dict:
    """
    What the pool is doing, for the health check and the runs page.

    Exhaustion is invisible until it is total, at which point every page fails
    at once and nothing says why. This is the number that would have said.
    """
    pool = engine.pool
    try:
        return {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "available": pool.size() - pool.checkedout(),
        }
    except Exception:  # pragma: no cover - pool types without these accessors
        return {}


# Write routes must call db.commit() explicitly — this generator never commits.
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
