"""Reuse employer evidence without turning a similar company name into proof."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, or_

from app.models.company import Company
from app.models.company_board import CompanyBoard
from app.models.job import Job
from app.services.company_domain import extract_domain, is_company_domain, registrable_domain
from app.services.tunables import value


def normal_name(name):
    # Keep substantive words: Acme Systems and Acme Labs may be different firms.
    return " ".join(str(name or "").casefold().split())


def public_url(raw):
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter a public company or careers URL.")
    import httpx
    from app.services.url_safety import guard_request, UnsafeDestination
    # Syntax and address checks here; network fetches use public_client as well.
    try:
        guard_request(httpx.Request("GET", raw))
    except (UnsafeDestination, httpx.InvalidURL):
        raise ValueError("Enter a public company or careers URL.")
    return raw


def attach_known_companies(db, jobs):
    """Attach jobs to exact boards/domains or aliases explicitly supplied by the user."""
    # Placeholder identities made before research may be replaced by evidence.
    jobs = list(jobs)
    existing_ids = {job.company_id for job in jobs if getattr(job, "company_id", None)}
    placeholders = {row.id for row in db.query(Company).filter(Company.id.in_(existing_ids),
        Company.identity_source == "job", Company.domain_verified_at.is_(None)).all()} if existing_ids else set()
    jobs = [job for job in jobs if not getattr(job, "company_id", None) or job.company_id in placeholders]
    if not jobs:
        return 0
    companies = db.query(Company).filter(or_(Company.identity_source == "user",
        Company.domain_verified_at.isnot(None))).all()
    aliases, domains = {}, {}
    for company in companies:
        if company.domain and company.domain_verified_at:
            domains[company.domain] = company.id
        if company.identity_source == "user":
            for alias in [company.name, *(company.aliases or [])]:
                aliases.setdefault(normal_name(alias), set()).add(company.id)
    board_owners = {f"{b.ats}:{b.slug}".casefold(): b.company_id for b in
                    db.query(CompanyBoard).filter(CompanyBoard.company_id.isnot(None)).all()}
    attached = 0
    for job in jobs:
        candidates = set()
        owner = board_owners.get(str(getattr(job, "board", "") or "").casefold())
        if owner:
            candidates.add(owner)
        for url in [job.url, getattr(job, "apply_url", None)]:
            owner = domains.get(registrable_domain(extract_domain(url or "")))
            if owner:
                candidates.add(owner)
        # Strong contradictory evidence must not be overwritten by an alias.
        if not candidates:
            candidates = aliases.get(normal_name(job.company), set())
        if len(candidates) == 1:
            job.company_id = next(iter(candidates))
            attached += 1
    return attached


def ensure_for_job(db, job):
    attach_known_companies(db, [job])
    if job.company_id:
        return db.get(Company, job.company_id)
    company = Company(name=(job.company or "Unknown employer")[:200], identity_source="job")
    db.add(company)
    db.flush()
    job.company_id = company.id
    return company


def verified_domain_for(db, job):
    attach_known_companies(db, [job])
    company = db.get(Company, job.company_id) if job.company_id else None
    return company.domain if company and company.domain_verified_at else ""


def watch(db, name, website="", careers_url="", aliases=None):
    name = str(name or "").strip()[:200]
    if not name:
        raise ValueError("Enter an employer name.")
    website, careers_url = public_url(website), public_url(careers_url)
    domain = registrable_domain(extract_domain(website)) if website else None
    if domain and not is_company_domain(domain):
        raise ValueError("Use the employer's own website for its identity; put its ATS link in Careers URL.")
    company = db.query(Company).filter(Company.domain == domain).first() if domain else None
    if company is None:
        matches = db.query(Company).filter(func.lower(Company.name) == name.lower(),
                                           Company.identity_source == "user").all()
        if len(matches) == 1 and (not domain or matches[0].domain in {None, domain}):
            company = matches[0]
    if company is None:
        company = Company(name=name)
        db.add(company)
    now = datetime.now(timezone.utc)
    company.identity_source, company.watched = "user", True
    company.aliases = list(dict.fromkeys([*(company.aliases or []), name,
                                          *[a.strip()[:200] for a in (aliases or []) if a.strip()]]))[:50]
    if domain:
        company.domain, company.domain_verified_at = domain, now
        company.evidence = [*(company.evidence or []), {"kind": "user_confirmed_website", "url": website, "at": now.isoformat()}][-20:]
    if careers_url:
        company.careers_url = careers_url
    elif website and not company.careers_url:
        company.careers_url = website.rstrip("/") + "/careers"
    company.next_refresh_at, company.research_status = now, "pending"
    db.flush()
    # Explicit aliases connect the watch to existing opportunities immediately.
    names = {normal_name(a) for a in [company.name, *(company.aliases or [])]}
    rows = db.query(Job).filter(func.lower(Job.company).in_(names)).limit(5000).all()
    attach_known_companies(db, rows)
    # A shared alias cannot prove which employer owns a board.
    other_aliases = {normal_name(a) for other in db.query(Company).filter(
        Company.identity_source == "user", Company.id != company.id).all()
        for a in [other.name, *(other.aliases or [])]}
    unique_names = names - other_aliases
    if unique_names:
        for board in db.query(CompanyBoard).filter(CompanyBoard.company_id.is_(None),
            func.lower(CompanyBoard.company).in_(unique_names)).all():
            board.company_id = company.id
    return company


def sync_pins(db, names):
    """Existing priorities gain durable watch records without guessing a domain."""
    return [watch(db, name) for name in names]


def research(db, company, profile):
    """Learn career boards for a watched employer; network work is outside the transaction."""
    from app.services import ats_discovery, ats_sniffer, company_boards
    now = datetime.now(timezone.utc)
    db.refresh(company, with_for_update=True)
    if not company.watched:
        db.rollback()
        return {"company_id": str(company.id), "status": "unwatched", "boards": 0}
    if (company.research_status == "researching" and company.last_researched_at
            and now - company.last_researched_at < timedelta(minutes=10)):
        db.rollback()
        return {"company_id": str(company.id), "status": "researching", "boards": 0}
    name, url = company.name, company.careers_url or (f"https://{company.domain}/careers" if company.domain else "")
    identity = company.id
    snapshot = (company.name, company.careers_url, company.domain)
    company.research_status, company.last_researched_at = "researching", now
    db.commit()
    found, error = {}, ""
    if url:
        try:
            found = ats_discovery.extract_slugs(url)
            if not found:
                host = ats_sniffer.company_host(url)
                if host:
                    found = ats_sniffer.sniff_host(host, posting_url=url, company=name, allow_guess=False)
        except Exception as exc:
            error = str(exc)[:500]
    db.refresh(company, with_for_update=True)
    if not company.watched:
        db.rollback()
        return {"company_id": str(identity), "status": "unwatched", "boards": 0}
    if (company.research_status != "researching" or company.last_researched_at != now
            or snapshot != (company.name, company.careers_url, company.domain)):
        db.rollback()
        return {"company_id": str(identity), "status": "superseded", "boards": 0}
    company.last_researched_at = now
    company.next_refresh_at = now + timedelta(hours=int(value(profile, "watched_company_refresh_hours")))
    if error:
        company.research_status, company.research_note = "failed", error
    elif not url:
        company.research_status, company.research_note = "needs_website", "Add this employer's website or careers URL to discover its boards."
    elif not found:
        company.research_status, company.research_note = "needs_learning", "No supported board found. Open source learning with this careers URL to capture and learn its responses."
    else:
        company_boards.record_boards(db, found, origin="configured", company=name,
                                    source_host=urlparse(url).hostname)
        db.flush()
        count = 0
        for ats, slugs in found.items():
            for board in db.query(CompanyBoard).filter(CompanyBoard.ats == ats, CompanyBoard.slug.in_(slugs)).all():
                if board.company_id not in {None, identity}:
                    continue
                board.company_id = identity
                if hasattr(board, "next_due_at"):
                    board.next_due_at = now
                count += 1
        company.research_status, company.research_note = "ready", f"Watching {count} career board(s)."
    db.commit()
    if company.research_status == "needs_learning":
        from app.services import source_learning
        try:
            learned = source_learning.onboard(db, url)
            db.refresh(company, with_for_update=True)
            if (company.watched and company.last_researched_at == now
                    and snapshot == (company.name, company.careers_url, company.domain)):
                company.research_note = learned.get("reason") or company.research_note
            db.commit()
        except Exception:
            # The durable watch remains due on its schedule if dispatch fails.
            db.rollback()
            import logging
            logging.getLogger(__name__).exception("Source learning could not start for employer %s", identity)
    return {"company_id": str(identity), "status": company.research_status,
            "boards": sum(len(s) for s in found.values())}


def refresh_due(db, profile, now=None):
    now = now or datetime.now(timezone.utc)
    rows = db.query(Company).filter(Company.watched.is_(True), or_(Company.next_refresh_at.is_(None),
        Company.next_refresh_at <= now)).order_by(Company.next_refresh_at.asc().nullsfirst(), Company.created_at).limit(
        int(value(profile, "watched_company_batch_size"))).all()
    results = []
    for company in rows:
        identity = company.id
        try:
            results.append(research(db, company, profile))
        except Exception as exc:
            db.rollback()
            row = db.get(Company, identity)
            if row and row.watched:
                row.research_status, row.research_note = "failed", str(exc)[:500]
                row.next_refresh_at = now + timedelta(hours=int(value(profile, "watched_company_refresh_hours")))
                db.commit()
            results.append({"company_id": str(identity), "status": "failed", "boards": 0})
    return results
