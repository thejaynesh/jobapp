import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.templating import build as build_templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm"])
templates = build_templates()

PAGE_SIZE = 40


@router.get("", response_class=HTMLResponse)
def list_calls(
    request: Request,
    stage: str = "",
    status: str = "",
    application_id: str = "",
    sort: str = "newest",
    limit: int = PAGE_SIZE,
    db: Session = Depends(get_db),
):
    """
    Every LLM request and reply, newest first.

    Filtered by stage because a document generation is six calls with six
    different jobs — "the resume came out empty" is a question about exactly
    one of them, and reading the other five is how you miss it.
    """
    from app.models.llm_call import LLMCall

    limit = max(1, min(limit, 200))
    query = db.query(LLMCall)
    if stage:
        query = query.filter(LLMCall.stage == stage)
    if status == "failed":
        query = query.filter(LLMCall.ok.is_(False))
    elif status == "empty":
        # Succeeded and returned nothing: the failure that looks like success
        # everywhere else in the app.
        query = query.filter(LLMCall.ok.is_(True),
                             func.coalesce(LLMCall.response, "") == "")
    elif status == "ok":
        query = query.filter(LLMCall.ok.is_(True))
    if application_id:
        try:
            query = query.filter(LLMCall.application_id == uuid.UUID(application_id))
        except ValueError:
            pass

    if sort == "oldest":
        calls = query.order_by(LLMCall.created_at.asc()).limit(limit).all()
    elif sort == "slowest":
        calls = query.order_by(LLMCall.duration_ms.desc().nullslast()).limit(limit).all()
    else:
        calls = query.order_by(LLMCall.created_at.desc()).limit(limit).all()

    stages = [
        {"name": row[0], "count": row[1]}
        for row in db.query(LLMCall.stage, func.count(LLMCall.id))
        .group_by(LLMCall.stage)
        .order_by(func.count(LLMCall.id).desc())
        .all()
    ]
    totals = {
        "all": db.query(func.count(LLMCall.id)).scalar() or 0,
        "failed": db.query(func.count(LLMCall.id))
        .filter(LLMCall.ok.is_(False)).scalar() or 0,
        "empty": db.query(func.count(LLMCall.id))
        .filter(LLMCall.ok.is_(True), func.coalesce(LLMCall.response, "") == "")
        .scalar() or 0,
    }

    return templates.TemplateResponse(
        "llm/index.html",
        {"request": request, "calls": calls, "stages": stages, "totals": totals,
         "active_stage": stage, "active_status": status,
         "application_id": application_id, "sort": sort, "limit": limit},
    )


@router.get("/{call_id}", response_class=HTMLResponse)
def call_detail(request: Request, call_id: uuid.UUID, db: Session = Depends(get_db)):
    """One call in full: every message that went in, and everything that came back."""
    from app.models.llm_call import LLMCall

    call = db.query(LLMCall).filter(LLMCall.id == call_id).first()
    if not call:
        raise HTTPException(status_code=404, detail="No such LLM call.")
    return templates.TemplateResponse(
        "llm/detail.html", {"request": request, "call": call}
    )
