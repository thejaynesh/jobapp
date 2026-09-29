"""Opt-in cached embeddings. Reads never perform inference or hide postings."""
import math
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.models.intelligence import SemanticVector
from app.models.job import Job, JobStatus
from app.services import evidence
from app.services.tunables import value


def key(text, model):
    return evidence.fingerprint([settings.SEMANTIC_BASE_URL, model, text])


def profile_text(profile):
    return "\n".join(f["text"] for f in evidence.select(profile, " ".join(profile.get("target_roles") or [])))


def job_text(job):
    return f"{job.title}\n{(job.description or '')[:12000]}"


def cosine(left, right):
    if not left or len(left) != len(right):
        return 0.0
    a, b = math.sqrt(sum(v * v for v in left)), math.sqrt(sum(v * v for v in right))
    return max(0.0, min(1.0, sum(x * y for x, y in zip(left, right)) / (a * b))) if a and b else 0.0


def cached_scores(db, jobs, profile):
    model = str(value(profile, "semantic_model") or "").strip()
    if value(profile, "semantic_mode") == "off" or not model or not jobs:
        return {}
    candidate_key = key(profile_text(profile), model)
    keys = {str(job.id): key(job_text(job), model) for job in jobs}
    vectors = {row.key: row.vector for row in db.query(SemanticVector).filter(
        SemanticVector.key.in_([candidate_key, *keys.values()])).all()}
    candidate = vectors.get(candidate_key)
    if candidate is None:
        return {}
    return {job_id: cosine(candidate, vectors[cache_key]) for job_id, cache_key in keys.items() if cache_key in vectors}


def _embed(texts, model):
    endpoint = settings.SEMANTIC_BASE_URL.rstrip("/") + "/embeddings"
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        with client.stream("POST", endpoint, headers={"Authorization": f"Bearer {settings.SEMANTIC_API_KEY}"},
                           json={"model": model, "input": texts}) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 8_000_000:
                    raise ValueError("Embedding response too large")
            decoded = json.loads(body)
            data = decoded.get("data") if isinstance(decoded, dict) else None
    if not isinstance(data, list) or len(data) != len(texts):
        raise ValueError("Embedding response count does not match the request")
    result = [None] * len(texts)
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Invalid embedding item")
        index, vector = item.get("index"), item.get("embedding")
        if (not isinstance(index, int) or not 0 <= index < len(texts) or result[index] is not None
                or not isinstance(vector, list) or not 1 <= len(vector) <= 8192
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)):
            raise ValueError("Invalid embedding result")
        result[index] = vector
    if len({len(v) for v in result}) != 1:
        raise ValueError("Embedding dimensions differ")
    return result


def update(db, profile):
    started = time.monotonic()
    mode, model = value(profile, "semantic_mode"), str(value(profile, "semantic_model") or "").strip()
    if mode == "off":
        return {"embedded": 0, "status": "off"}
    if not model or not settings.SEMANTIC_BASE_URL or not settings.SEMANTIC_API_KEY:
        return {"embedded": 0, "status": "Configure the embedding connection and model first."}
    count = int(value(profile, "semantic_batch_size"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(value(profile, "semantic_active_days")))
    jobs = db.query(Job).filter(Job.closed_at.is_(None), Job.status.in_([JobStatus.matched, JobStatus.docs_generated]),
        or_(Job.fetched_at >= cutoff, Job.favourite.is_(True))).order_by(Job.favourite.desc(), Job.fetched_at.desc()).limit(200).all()
    from types import SimpleNamespace
    jobs = [SimpleNamespace(id=j.id, title=j.title, description=j.description,
        score=j.llm_score_deep if j.llm_score_deep is not None else j.llm_score) for j in jobs]
    texts = list(dict.fromkeys([profile_text(profile)] + [job_text(job) for job in jobs]))
    texts = [text for text in texts if text.strip()]
    existing = {row[0] for row in db.query(SemanticVector.key).filter(SemanticVector.key.in_([key(t, model) for t in texts]))}
    missing = [t for t in texts if key(t, model) not in existing][:count]
    # This worker owns the session; release reads before remote inference.
    db.commit()
    vectors = _embed(missing, model) if missing else []
    if vectors:
        db.execute(insert(SemanticVector).values([
            {"key": key(text, model), "model": model, "vector": vector} for text, vector in zip(missing, vectors)
        ]).on_conflict_do_nothing(index_elements=[SemanticVector.key]))
    db.commit()
    scores = cached_scores(db, jobs, profile)
    base = [str(j.id) for j in sorted(jobs, key=lambda j: -(j.score or 0)) if str(j.id) in scores][:10]
    proposed = sorted(scores, key=scores.get, reverse=True)[:10]
    return {"embedded": len(vectors), "status": mode, "model": model,
            "cohort": len(jobs), "cached": len(scores), "input_characters": sum(len(t) for t in missing),
            "elapsed_seconds": round(time.monotonic() - started, 3), "baseline_top": base,
            "semantic_top": proposed, "top_overlap": len(set(base) & set(proposed)),
            "note": "Overlap measures a change in ordering, not quality. Paid cost depends on the configured provider; no cost estimate is inferred."}


def prune(db, profile):
    before = datetime.now(timezone.utc) - timedelta(days=2 * int(value(profile, "semantic_active_days")))
    return db.query(SemanticVector).filter(SemanticVector.created_at < before).delete(synchronize_session=False)
