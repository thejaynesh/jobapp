from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)

templates = Jinja2Templates(directory="app/templates")


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
