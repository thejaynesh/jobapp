"""Source learning runs off request threads and preserves failures for retry."""
from app.celery_app import celery_app
from app.database import SessionLocal


@celery_app.task(name="app.tasks.source_learning.learn_source", soft_time_limit=540, time_limit=600)
def learn_source(host, endpoint_key, claim_token, hint=""):
    from app.models.harvest_recipe import HarvestLearningState
    from app.models.profile import Profile
    from app.services import harvest_recipes, source_learning
    with SessionLocal() as db:
        row = db.query(HarvestLearningState).filter_by(host=host, endpoint_key=endpoint_key,
            claim_token=claim_token, status="queued").with_for_update().first()
        if row is None:
            return {"ok": False, "reason": "Attempt already claimed or superseded."}
        row.status = "learning"
        row.attempts += 1
        profile = db.query(Profile).first()
        data = dict(profile.data or {}) if profile else {}
        db.commit()
        try:
            if endpoint_key == source_learning.NAVIGATION:
                from app.services import crawl_recipes
                outcome = crawl_recipes.learn(db, host, data, hint=hint)
            else:
                outcome = harvest_recipes.learn(db, host, data, hint=hint, endpoint=endpoint_key)
        except Exception as exc:
            db.rollback()
            outcome = {"ok": False, "reason": f"Learning failed: {exc}"}
        source_learning.finish(db, host, endpoint_key, claim_token, outcome)
        return outcome


@celery_app.task(name="app.tasks.source_learning.sweep_source_learning", soft_time_limit=120)
def sweep_source_learning():
    from app.services.source_learning import sweep
    with SessionLocal() as db:
        return sweep(db)
