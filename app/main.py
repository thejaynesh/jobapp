from contextlib import asynccontextmanager
import logging
import subprocess

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db, SessionLocal
from app.routers import profile as profile_router
from app.routers.docs import router as docs_router
from app.routers.jobs import router as jobs_router
from app.routers.apps import router as apps_router
from app.routers.settings import router as settings_router
from app.routers.outreach import router as outreach_router

logger = logging.getLogger(__name__)


def _seed_profile_if_empty() -> None:
    from app.models.profile import Profile
    from app.services.profile_service import apply_seed
    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        if not profile:
            logger.info("No profile found — skipping seed")
            return
        if profile.data and profile.data.get("experience"):
            logger.info("Profile already has experience — skipping seed")
            return
        apply_seed(db)
        logger.info("Profile seeded with experience, skills, education, and narrative")
    except Exception as exc:
        logger.error("Profile seed failed: %s", exc)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error("alembic upgrade failed: %s", result.stderr or result.stdout)
        else:
            logger.info("alembic upgrade head: %s", result.stdout.strip() or "up to date")
    except Exception as exc:
        logger.error("alembic upgrade error: %s", exc)
    _seed_profile_if_empty()
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(profile_router.router)
app.include_router(docs_router)
app.include_router(jobs_router)
app.include_router(apps_router)
app.include_router(settings_router)
app.include_router(outreach_router)


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


@app.get("/")
def root():
    return RedirectResponse(url="/apps")
