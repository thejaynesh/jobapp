"""
The applications you have in flight: what is next on each, and what gets replies.

**Status changes** go through `set_status`, from the application page and from
the extension alike. It stamps when the status moved, stamps `applied_at` the
first time an application is marked applied (the page never did; only the
extension's button did), records which resume version and whether a cover
letter was current at that moment, and sets a next action with a due date
unless you have written your own:

    applied       follow up if there is no reply   in `followup_after_days`
    interviewing  thank-you note, prepare the next round   tomorrow
    offered       decide on the offer               in three days

**The board** (`/apps/board`) is the same applications in a column per status,
each card with its next action, red when overdue. **Reminders**: on the
settings page's interval, the actions due today or earlier go to the log as
one warning, which the Log badge counts.

**Response rates** read what was sent against what came back. "Heard back" is
an interview, an offer or a rejection; "interview" the first two. Grouped by
whether a cover letter went, whether the resume was edited by hand before it
went, how many of the posting's keywords a parser could read in it, and which
model wrote it. A group of fewer than five applications shows its counts but
no rate: two of three is not a rate.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType

ACTIVE = (ApplicationStatus.applied, ApplicationStatus.interviewing, ApplicationStatus.offered)
HEARD_BACK = (ApplicationStatus.interviewing, ApplicationStatus.offered, ApplicationStatus.rejected)
INTERVIEWED = (ApplicationStatus.interviewing, ApplicationStatus.offered)
# Statuses that mean the application went out.
SENT = ACTIVE + (ApplicationStatus.rejected,)
BOARD_COLUMNS = ("not_applied", "applied", "interviewing", "offered", "rejected", "withdrawn")
# "Not applied" holds every matched job with documents; the board shows the
# ones worth acting on, not the backlog.
TO_APPLY_LIMIT = 30
MIN_FOR_RATE = 5

DEFAULT_ACTIONS = {
    ApplicationStatus.not_applied: ("Apply", None),
    ApplicationStatus.applied: ("Follow up if there is no reply", "followup"),
    ApplicationStatus.interviewing: ("Send a thank-you note and prepare the next round", 1),
    ApplicationStatus.offered: ("Decide on the offer", 3),
}
_DEFAULT_TEXTS = {text for text, _ in DEFAULT_ACTIONS.values()}


def _today(now: datetime) -> date:
    return now.date()


def default_action(status: ApplicationStatus, now: datetime, followup_days: int):
    text, due = DEFAULT_ACTIONS.get(status, (None, None))
    if due == "followup":
        due = followup_days
    return text, (_today(now) + timedelta(days=due)) if isinstance(due, int) else None


def _current(application, doc_type):
    return next((d for d in application.documents or []
                 if d.doc_type == doc_type and d.is_current), None)


def set_status(db, application, status: ApplicationStatus, now: datetime | None = None,
               profile_data: dict | None = None, record_event: bool = True) -> None:
    """Move an application to `status`, with everything that goes with the move."""
    from app.services.tunables import value

    now = now or datetime.now(timezone.utc)
    if application.id is not None:
        # Serialize transitions, including simultaneous clicks from two tabs.
        db.flush()
        db.refresh(application, attribute_names=["status", "status_changed_at", "applied_at", "sent_resume_id"], with_for_update=True)
    if status == application.status and application.status_changed_at is not None:
        return
    from app.services import application_history
    if record_event:
        application_history.record_status(db, application, status, profile_data or {}, now)
    application.status = status
    application.status_changed_at = now
    # Straight to "interviewing" from "not applied" still means it was sent.
    if status in SENT:
        if application.applied_at is None:
            application.applied_at = now
        if application.sent_resume_id is None:
            resume = _current(application, DocType.resume)
            application.sent_resume_id = resume.id if resume is not None else None
            application.sent_cover_letter = _current(application, DocType.cover_letter) is not None
    # Your own next action stays; a default is replaced by the new default.
    if application.next_action is None or application.next_action in _DEFAULT_TEXTS:
        days = int(value(profile_data, "followup_after_days"))
        text, due = default_action(status, now, days)
        application.next_action, application.next_action_due = text, due


def set_next_action(application, text: str, due: date | None) -> None:
    application.next_action = " ".join((text or "").split())[:200] or None
    application.next_action_due = due if application.next_action else None


# --- The board -------------------------------------------------------------------

def _card(application, today: date) -> dict:
    changed = application.status_changed_at or application.applied_at or application.created_at
    due = application.next_action_due
    return {
        "app": application,
        "job": application.job,
        "days_in_column": (today - changed.date()).days if changed else None,
        "next_action": application.next_action,
        "due": due,
        "overdue": due is not None and due < today,
        "due_today": due == today,
    }


def board(db, now: datetime | None = None) -> dict:
    from sqlalchemy import func

    from app.models.job import Job

    now = now or datetime.now(timezone.utc)
    today = _today(now)
    columns: dict = {c: [] for c in BOARD_COLUMNS}
    moving = (db.query(Application).join(Application.job)
              .filter(Application.status != ApplicationStatus.not_applied).all())
    for application in moving:
        columns[application.status.value].append(_card(application, today))
    # To apply: documents written or a job you starred, best score first.
    score = func.coalesce(Job.llm_score_deep, Job.llm_score)
    waiting = (db.query(Application).join(Application.job)
               .filter(Application.status == ApplicationStatus.not_applied,
                       (Application.generation_status == "done") | Job.favourite.is_(True))
               .order_by(score.desc().nullslast())
               .limit(TO_APPLY_LIMIT).all())
    columns["not_applied"] = [_card(a, today) for a in waiting]
    epoch = date.max
    for status, cards in columns.items():
        if status != "not_applied":
            cards.sort(key=lambda c: (c["due"] or epoch, -(c["days_in_column"] or 0)))
    return columns


def due(db, now: datetime | None = None) -> list[Application]:
    """Active applications whose next action is due today or earlier."""
    today = _today(now or datetime.now(timezone.utc))
    return (db.query(Application)
            .filter(Application.status.in_(ACTIVE),
                    Application.next_action.isnot(None),
                    Application.next_action_due.isnot(None),
                    Application.next_action_due <= today)
            .order_by(Application.next_action_due)
            .all())


def reminder_line(applications: list[Application], now: datetime | None = None) -> str | None:
    if not applications:
        return None
    today = _today(now or datetime.now(timezone.utc))
    parts = []
    for a in applications[:5]:
        late = (today - a.next_action_due).days
        when = "today" if late == 0 else f"{late} day{'s' if late != 1 else ''} late"
        parts.append(f"{a.next_action} — {a.job.title} at {a.job.company} ({when})")
    more = len(applications) - 5
    return (f"{len(applications)} application follow-up{'s' if len(applications) != 1 else ''} due: "
            + "; ".join(parts) + (f"; and {more} more" if more > 0 else "") + ". See /apps/board.")


# --- What gets replies --------------------------------------------------------------

def _coverage_band(doc) -> str:
    ats = ((doc.content or {}).get("ats") if doc is not None else None) or {}
    keywords = ats.get("keywords") or []
    if not keywords:
        return "not measured"
    share = len(ats.get("present") or []) / len(keywords)
    return "80% or more" if share >= 0.8 else "50–79%" if share >= 0.5 else "under 50%"


def response_rates(db) -> dict:
    from app.services.document_edit import EDITED_BY

    sent = (db.query(Application)
            .filter(Application.status.in_(ACTIVE + (ApplicationStatus.rejected,
                                                     ApplicationStatus.withdrawn)))
            .all())
    docs = {d.id: d for d in db.query(ApplicationDocument).filter(
        ApplicationDocument.id.in_([a.sent_resume_id for a in sent if a.sent_resume_id]))} \
        if any(a.sent_resume_id for a in sent) else {}
    from app.services import application_history
    history = application_history.milestones(db, [a.id for a in sent])
    groups: dict = defaultdict(lambda: defaultdict(lambda: {"sent": 0, "heard": 0, "interviews": 0}))
    for application in sent:
        resume = docs.get(application.sent_resume_id)
        letter = {True: "With a cover letter", False: "Without one"}.get(
            application.sent_cover_letter, "Not recorded")
        edited = ("Not recorded" if resume is None else
                  "Edited by hand" if resume.generated_by == EDITED_BY else "As generated")
        model = ("Not recorded" if resume is None or not resume.generated_by
                 else resume.generated_by.split(",")[0].strip())
        for question, answer in (("Cover letter", letter), ("Resume", edited),
                                 ("Keywords a parser read", _coverage_band(resume)),
                                 ("Written by", model)):
            row = groups[question][answer]
            row["sent"] += 1
            milestones = history.get(application.id, set())
            row["heard"] += application.status in HEARD_BACK or bool(milestones & {"assessment", "interview_invited", "interview_completed", "offered", "rejected"})
            row["interviews"] += application.status in INTERVIEWED or bool(milestones & {"interview_invited", "interview_completed", "offered"})
    table = {}
    for question, answers in groups.items():
        table[question] = [{
            "answer": answer, **row,
            "heard_rate": row["heard"] / row["sent"] if row["sent"] >= MIN_FOR_RATE else None,
            "interview_rate": row["interviews"] / row["sent"] if row["sent"] >= MIN_FOR_RATE else None,
        } for answer, row in sorted(answers.items(), key=lambda kv: -kv[1]["sent"])]
    return {"total": len(sent), "groups": table, "min_for_rate": MIN_FOR_RATE}
