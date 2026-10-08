"""Explicit employer identities and active career-board watchlists."""
import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.company import Company
from app.models.company_board import CompanyBoard
from app.models.job import Job
from app.services import company_identity
from app.templating import build

router = APIRouter(prefix="/companies", tags=["companies"])
templates = build()
logger = logging.getLogger(__name__)


def _queue(company_id):
    from app.tasks.opportunities import refresh_company
    try:
        refresh_company.delay(str(company_id))
    except Exception:
        logger.exception("Employer research remains due for maintenance: %s", company_id)


@router.get("", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return _render(request, db)


def _render(request, db, *, error="", values=None, status_code=200):
    companies = db.query(Company).filter(Company.identity_source == "user").order_by(Company.watched.desc(), Company.name).limit(250).all()
    ids = [c.id for c in companies]
    boards = dict(db.query(CompanyBoard.company_id, func.count()).filter(CompanyBoard.company_id.in_(ids)).group_by(CompanyBoard.company_id).all())
    jobs = dict(db.query(Job.company_id, func.count()).filter(Job.company_id.in_(ids), Job.closed_at.is_(None)).group_by(Job.company_id).all())
    return templates.TemplateResponse("companies/index.html", {"request": request, "companies": companies,
        "board_counts": boards, "job_counts": jobs, "error": error, "values": values or {}}, status_code=status_code)


@router.post("")
def add(request: Request, name: str = Form(""), website: str = Form(""), careers_url: str = Form(""), aliases: str = Form(""), db: Session = Depends(get_db)):
    try:
        company = company_identity.watch(db, name, website, careers_url, aliases.splitlines())
    except ValueError as exc:
        return _render(request, db, error=str(exc), status_code=422,
                       values={"name": name, "website": website, "careers_url": careers_url, "aliases": aliases})
    db.commit()
    _queue(company.id)
    return RedirectResponse("/companies", 303)


@router.post("/{company_id}/watch")
def toggle(company_id: uuid.UUID, watched: bool = Form(...), db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Employer not found")
    company.watched = watched
    if watched:
        company.next_refresh_at = None
    db.commit()
    if watched:
        _queue(company.id)
    return RedirectResponse("/companies", 303)


@router.post("/{company_id}/refresh")
def refresh(company_id: uuid.UUID, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "Employer not found")
    company.next_refresh_at = None
    db.commit()
    _queue(company.id)
    return RedirectResponse("/companies", 303)
