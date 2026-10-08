"""Turn an unfamiliar source into observed evidence, a reader and recovered jobs."""
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy.dialects.postgresql import insert

from app.config import live
from app.models.harvest_recipe import HarvestLearningState, HarvestSample
from app.services import harvest_recipes, harvest_samples

ONBOARDING = "@source"
NAVIGATION = "@navigation"


def _now():
    return datetime.now(timezone.utc)


def _state(db, host, endpoint):
    db.execute(insert(HarvestLearningState).values(
        id=uuid.uuid4(), host=host, endpoint_key=endpoint, status="waiting", evidence_hash="",
        attempts=0, result={}, updated_at=_now()).on_conflict_do_nothing(
            constraint="uq_harvest_learning_endpoint"))
    return db.query(HarvestLearningState).filter_by(host=host, endpoint_key=endpoint).with_for_update().one()


def pending_samples(db, host, endpoint):
    return [row for row in harvest_samples.for_host(db, host, limit=100, endpoint=endpoint)
            if not row.found and harvest_recipes.jobbiness(row.payload)]


def evidence_hash(samples):
    # The learner sees bounded snapshots. Changes only in trimmed tails (or
    # repeated copies of the same snapshot) cannot improve its next attempt.
    return harvest_samples.fingerprint(sorted({harvest_samples.fingerprint(row.payload) for row in samples}))


def navigation_hash(sample):
    evidence = sample.evidence or {}
    return harvest_samples.fingerprint({"url": sample.source_url,
        "controls": evidence.get("controls") or [], "query": evidence.get("query") or {}})


def request_learning(db, host, endpoint, *, force=False, hint=""):
    """Reserve one task; changed evidence reopens exhausted attempts after cooldown."""
    if not force and not live().HARVEST_AUTO_LEARN_ENABLED:
        return {"queued": False, "reason": "Automatic source learning is disabled in Settings."}
    if endpoint == NAVIGATION:
        from app.services import crawl_recipes
        sample = crawl_recipes.latest_sample(db, host)
        samples = [sample] if sample and sample.evidence else []
        if crawl_recipes.active_for(db, host) and not force:
            return {"queued": False, "reason": "Navigation already has a validated reader."}
    else:
        samples = pending_samples(db, host, endpoint)
    if not samples:
        return {"queued": False, "reason": "Waiting for a response containing job fields."}
    now = _now()
    row = _state(db, host, endpoint)
    # A worker killed without returning must not suppress learning forever.
    if row.status in ("queued", "learning") and row.attempted_at and row.attempted_at > now - timedelta(minutes=15):
        db.commit()
        return {"queued": False, "reason": "Learning is already queued or running."}
    digest = (navigation_hash(samples[0])
              if endpoint == NAVIGATION else evidence_hash(samples))
    changed = digest != row.evidence_hash
    if not force and row.next_retry_at and row.next_retry_at > now:
        db.commit()
        return {"queued": False, "reason": "Waiting until the next learning retry."}
    if not force and not changed and row.attempts >= live().HARVEST_LEARN_MAX_ATTEMPTS:
        row.status = "needs_evidence"
        db.commit()
        return {"queued": False, "reason": "Visit a different search or posting to capture fresh evidence, or retry manually."}
    if changed or force:
        row.attempts = 0
    token = str(uuid.uuid4())
    row.evidence_hash, row.claim_token = digest, token
    row.status, row.attempted_at = "queued", now
    row.note = "Evidence captured; waiting for the source learning worker."
    db.commit()
    try:
        from app.tasks.source_learning import learn_source
        learn_source.delay(host, endpoint, token, hint)
    except Exception as exc:
        row = _state(db, host, endpoint)
        if row.claim_token == token:
            row.status, row.claim_token = "retry", None
            row.note = f"Could not queue learning: {exc}"[:1000]
            row.next_retry_at = now + timedelta(minutes=live().HARVEST_LEARN_RETRY_MINUTES)
        db.commit()
        return {"queued": False, "reason": row.note}
    return {"queued": True, "reason": "Learning queued; validated jobs will be recovered automatically."}


def finish(db, host, endpoint, token, outcome):
    row = _state(db, host, endpoint)
    if row.claim_token != token:
        db.commit()
        return
    row.claim_token = None
    row.note = str(outcome.get("reason") or "")[:2000]
    row.result = {key: outcome[key] for key in ("ok", "jobs", "replay", "id") if key in outcome}
    row.status = "ready" if outcome.get("ok") else (
        "needs_evidence" if row.attempts >= live().HARVEST_LEARN_MAX_ATTEMPTS else "retry")
    row.next_retry_at = None if outcome.get("ok") else _now() + timedelta(
        minutes=live().HARVEST_LEARN_RETRY_MINUTES * max(1, row.attempts))
    db.commit()
    if endpoint == NAVIGATION and outcome.get("ok"):
        from app.services import browse_plan, crawl_recipes
        sample = crawl_recipes.latest_sample(db, host)
        source = db.query(HarvestLearningState).filter_by(host=host, endpoint_key=ONBOARDING).first()
        if sample and sample.source_url and (not source or source.status != "paused"):
            browse_plan.enqueue(db, [sample.source_url], limit=1, purpose="source_learning",
                                priority=browse_plan.PRIORITY_SWEEP)


def sweep(db):
    """Retry durable unread evidence; broker loss and worker restarts are recoverable."""
    samples = db.query(HarvestSample).filter(HarvestSample.found == 0).all()
    groups = {(row.host, harvest_samples.sample_endpoint(row)) for row in samples
              if harvest_recipes.jobbiness(row.payload)}
    from app.services import browse_plan, crawl_recipes
    groups |= {(host, NAVIGATION) for host in crawl_recipes.hosts_needing_a_recipe(db)}
    queued = 0
    for host, endpoint in sorted(groups):
        queued += int(request_learning(db, host, endpoint).get("queued", False))
    revisits = 0
    for row in db.query(HarvestLearningState).filter_by(endpoint_key=ONBOARDING, status="ready").all():
        url = (row.result or {}).get("url")
        if url:
            revisits += browse_plan.enqueue(db, [url], limit=1, purpose="source_learning",
                                             priority=browse_plan.PRIORITY_SWEEP)
    return {"endpoints": len(groups), "queued": queued, "revisits": revisits}


def onboard(db, url: str):
    """Register a known ATS or queue an ordinary permitted browser capture."""
    from app.models.browser_task import BrowserTask
    from app.services import ats_discovery, browse_plan, company_boards
    url = (url or "").strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Enter the full HTTP(S) address of a careers or job-search page.")
    except ValueError as exc:
        return {"ok": False, "status": "needs_help", "reason": str(exc)}
    host = parsed.hostname.lower()
    row = _state(db, host, ONBOARDING)
    row.result = {**(row.result or {}), "url": url[:1000]}
    known = ats_discovery.extract_slugs(url)
    if known:
        count = company_boards.record_boards(db, known, origin="discovered")
        row.status = "board_registered"
        row.note = "Recognized ATS board saved. The normal board-validation and fetch cycle will check it."
        row.result = {**row.result, "boards": count}
    elif not browse_plan.enabled():
        row.status, row.note = "needs_browser", "Enable browser collection in Settings, connect the extension, then retry this URL."
    elif browse_plan.is_paused(url):
        row.status, row.note = "paused", "This host is paused. Resume it in browser settings, then retry this URL."
    else:
        db.commit()  # Enqueue commits; release the learning-state lock first.
        count = browse_plan.enqueue(db, [url], limit=1, purpose="source_learning", priority=browse_plan.PRIORITY_REQUESTED)
        task = db.query(BrowserTask).filter(BrowserTask.kind == "browse_page",
            BrowserTask.payload["url"].astext == url,
            BrowserTask.status.in_(["queued", "leased"])).order_by(BrowserTask.created_at.desc()).first()
        row = _state(db, host, ONBOARDING)
        if count or task:
            row.status = "waiting_browser"
            row.note = ("Waiting for the extension to capture this page. Enable browsing and grant its existing site/tab permissions; "
                        "for an unfamiliar site use Harvesting → Add job site in extension options and approve its site permission. "
                        "Sign in on the site if required, then Retry capture. No permission is changed automatically.")
            row.result = {**(row.result or {}), "url": url[:1000], "task_id": str(task.id) if task else None}
        else:
            row.status, row.note = "resting", "This site is resting after a rate limit. Wait for its browser cooldown, then retry."
    db.commit()
    return {"ok": row.status in ("board_registered", "waiting_browser"), "status": row.status,
            "reason": row.note, "host": host, **(row.result or {})}


def note_capture(db, source_url, counts, *, page_url="", read_by="walker"):
    """A response proves progress on an explicitly added source, including API subdomains."""
    host = urlsplit(page_url or source_url).hostname or ""
    row = db.query(HarvestLearningState).filter_by(host=host, endpoint_key=ONBOARDING).first()
    if row:
        found = int(counts.get("found") or 0)
        if row.status in ("ready", "paused") and not found:
            return
        if row.status == "paused":
            return
        invalid = int(counts.get("invalid") or 0)
        row.status = "needs_help" if invalid else ("ready" if found else "evidence_captured")
        row.note = (f"Recognized {found} job(s), but {invalid} could not be stored. Inspect the ingestion logs and retry learning." if invalid else
                    f"Captured {found} job(s) with the {read_by}; jobs entered normal ingestion." if found else
                    "Captured an unread response. Job-like evidence is learned automatically; inspect samples for the result.")
        row.result = {**(row.result or {}), "response_host": urlsplit(source_url).hostname,
                      "last_capture": _now().isoformat(), "found": found}


def note_visit(db, task):
    """Keep login/permission/capture failures separate from extraction failures."""
    payload, result = task.payload or {}, task.result or {}
    if payload.get("purpose") != "source_learning":
        return
    host = urlsplit(payload.get("url") or "").hostname or ""
    row = _state(db, host, ONBOARDING)
    if row.status == "paused":
        db.commit()
        return
    if result.get("signed_in") is False or result.get("challenge") in ("timeout", "skipped"):
        row.status, row.note = "needs_login", "Open this site in your browser, sign in or finish its check, then retry this URL."
    elif row.status == "waiting_browser":
        row.status, row.note = "needs_evidence", "The page opened but no readable job response arrived. Open a search with visible jobs, then retry or inspect captured samples."
    db.commit()


def listing(db, limit=30):
    from app.models.browser_task import BrowserTask
    rows = db.query(HarvestLearningState).order_by(HarvestLearningState.updated_at.desc()).limit(limit).all()
    for row in rows:
        task_id = (row.result or {}).get("task_id")
        if row.endpoint_key != ONBOARDING or row.status != "waiting_browser" or not task_id:
            continue
        try:
            task = db.get(BrowserTask, uuid.UUID(task_id))
        except (ValueError, TypeError):
            continue
        if task and task.status in ("failed", "expired"):
            row.status = "needs_browser"
            row.note = f"Browser capture {task.status}: {(task.error or 'No connected browser completed the capture')[:500]}. Check extension permissions, then retry."
    db.flush()
    return rows


def pause(db, host):
    from app.models.browser_task import BrowserTask
    row = _state(db, host, ONBOARDING)
    row.status, row.note = "paused", "Automatic capture paused. Retry capture resumes this source."
    url = (row.result or {}).get("url")
    if url:
        db.query(BrowserTask).filter(BrowserTask.kind == "browse_page", BrowserTask.status == "queued",
            BrowserTask.payload["purpose"].astext == "source_learning", BrowserTask.payload["url"].astext == url).update(
                {"status": "expired", "error": "Source capture paused by the user"}, synchronize_session=False)
    db.commit()
