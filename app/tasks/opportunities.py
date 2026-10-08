"""Employer watches and optional preparation of explicitly shortlisted jobs."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.opportunities.refresh_company", soft_time_limit=180, time_limit=210)
def refresh_company(company_id):
    from app.models.company import Company
    from app.models.profile import Profile
    from app.services.company_identity import research
    with SessionLocal() as db:
        company = db.get(Company, company_id)
        if not company or not company.watched:
            return {"skipped": "Employer is no longer watched"}
        profile = db.query(Profile).first()
        return research(db, company, profile.data if profile else {})


@celery_app.task(name="app.tasks.opportunities.maintain", soft_time_limit=540, time_limit=600)
def maintain():
    from app.models.profile import Profile
    from app.models.application import Application, ApplicationStatus
    from app.models.job import Job
    from app.services import capacity, company_identity, outreach
    from app.services.tunables import value
    with SessionLocal() as db:
        profile = db.query(Profile).first()
        data = (profile.data or {}) if profile else {}
        if not capacity.allow_background(data):
            return {"deferred": "interactive capacity"}
        companies = company_identity.refresh_due(db, data)
        queued = 0
        if value(data, "shortlist_prepare_outreach"):
            now = datetime.now(timezone.utc)
            retry_cutoff = now - timedelta(hours=int(value(data, "outreach_followup_retry_hours")))
            claim_cutoff = now - outreach.DISCOVERY_TIMEOUT
            rows = db.query(Job.id).outerjoin(Application).filter(Job.favourite.is_(True), Job.closed_at.is_(None),
                or_(Application.id.is_(None), Application.status == ApplicationStatus.not_applied),
                or_(Application.id.is_(None), Application.outreach_status == "idle",
                    (Application.outreach_status == "failed") & or_(Application.outreach_checked_at.is_(None), Application.outreach_checked_at < retry_cutoff),
                    (Application.outreach_status == "discovering") & or_(Application.outreach_checked_at.is_(None), Application.outreach_checked_at < claim_cutoff))
                ).order_by(Job.fetched_at.desc(), Job.id).limit(int(value(data, "watched_company_batch_size"))).all()
            db.commit()
            for (job_id,) in rows:
                try:
                    prepare_shortlist.delay(str(job_id))
                    queued += 1
                except Exception:
                    logger.exception("Could not queue shortlist preparation for %s", job_id)
        return {"companies": companies, "shortlist_queued": queued}


@celery_app.task(name="app.tasks.opportunities.prepare_shortlist", soft_time_limit=300, time_limit=330)
def prepare_shortlist(job_id):
    from app.models.application import Application, ApplicationStatus
    from app.models.job import Job
    from app.models.profile import Profile
    from app.services import company_identity, enrichment, outreach
    from app.services.tunables import value
    from app.services.url_safety import public_client
    with SessionLocal() as db:
        profile = db.query(Profile).first()
        data = (profile.data or {}) if profile else {}
        if not value(data, "shortlist_prepare_outreach"):
            return {"skipped": "Shortlist preparation is disabled"}
        job = db.query(Job).filter(Job.id == job_id).with_for_update().first()
        if not job or not job.favourite or job.closed_at:
            return {"skipped": "Job is no longer an open shortlist item"}
        application = db.query(Application).filter(Application.job_id == job.id).first()
        if application is None:
            application = Application(job_id=job.id)
            db.add(application)
            db.flush()
        if application.status != ApplicationStatus.not_applied:
            return {"skipped": "Application has progressed"}
        if application.outreach_status == "discovering" and not outreach.discovery_stale(application):
            return {"skipped": "Application has progressed or research is already running"}
        if application.outreach_status == "done":
            return {"skipped": "Research is already available; refresh it from the contact panel"}
        company_identity.ensure_for_job(db, job)
        application.outreach_status, application.outreach_error = "discovering", None
        application.outreach_checked_at = datetime.now(timezone.utc)
        application_id = application.id
        needs_details, url = len(job.description or "") < 600, job.apply_url or job.url
        db.commit()
        try:
            if needs_details and url:
                with public_client(timeout=20, follow_redirects=True) as client:
                    found = enrichment.enrich_one(client, url, job_id=job.id)
                db.refresh(job)
                if found:
                    enrichment.apply_extraction(db, job, found)
                    db.commit()
            db.refresh(job)
            db.refresh(application)
            if not job.favourite or job.closed_at or application.status != ApplicationStatus.not_applied:
                application.outreach_status = "idle"
                db.commit()
                return {"skipped": "Shortlist changed while preparing"}
            contacts = outreach.run_outreach(db, application, draft=True)
            application.outreach_status = "done"
            db.commit()
            return {"application_id": str(application_id), "contacts": len(contacts)}
        except Exception as exc:
            db.rollback()
            application = db.get(Application, application_id)
            if application:
                application.outreach_status, application.outreach_error = "failed", str(exc)[:500]
                db.commit()
            logger.exception("Shortlist preparation failed for %s", job_id)
            return {"application_id": str(application_id), "error": str(exc)[:500]}


def queue_shortlist(job_ids, profile):
    from app.services.tunables import value
    if not value(profile, "shortlist_prepare_outreach"):
        return
    for job_id in job_ids:
        try:
            prepare_shortlist.delay(str(job_id))
        except Exception:
            # The saved star remains the durable request; maintenance retries.
            logger.exception("Shortlist preparation will retry on maintenance: %s", job_id)
