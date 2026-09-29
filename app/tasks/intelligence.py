"""Bounded intelligence maintenance, independently scheduled from page requests."""
from app.celery_app import celery_app
from app.database import SessionLocal


@celery_app.task(name="app.tasks.intelligence.maintain", max_retries=0, soft_time_limit=180, time_limit=210)
def maintain():
    from app.models.profile import Profile
    from app.services import application_history, capacity, outcome_learning, semantic
    with SessionLocal() as db:
        profile = db.query(Profile).first()
        data = dict(profile.data or {}) if profile else {}
        if not capacity.allow_background(data):
            return {"deferred": "interactive capacity"}
        pruned = application_history.prune(db, data)
        vector_pruned = semantic.prune(db, data)
        model = outcome_learning.fit(db, data)
        # Reload before writing so a long operation cannot overwrite settings.
        if profile:
            db.refresh(profile, with_for_update=True)
            profile.data = {**(profile.data or {}), outcome_learning.STORE_KEY: model}
        db.commit()
        embedded = semantic.update(db, data)
        if profile:
            db.refresh(profile, with_for_update=True)
            profile.data = {**(profile.data or {}), "intelligence_report": embedded}
        db.commit()
        return {"decisions_pruned": pruned, "vectors_pruned": vector_pruned, "embeddings": embedded, "outcome_labels": model["labels"]}
