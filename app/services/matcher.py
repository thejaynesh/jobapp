import json
import logging
import re
import time
from difflib import SequenceMatcher

from openai import OpenAI, RateLimitError

from app.config import settings
from app.models.application import Application
from app.models.job import Job, JobStatus
from app.models.profile import Profile

logger = logging.getLogger(__name__)

MIN_KEYWORD_SKILLS = 2  # overridden by settings.MIN_KEYWORD_SKILLS if present


def _normalize(text: str) -> str:
    return text.lower().strip()


def _flatten_skills(skills_data: dict) -> list[str]:
    result = []
    for category_skills in skills_data.values():
        result.extend(category_skills)
    return result


def _title_matches_roles(title: str, target_roles: list[str]) -> bool:
    title_lower = _normalize(title)
    for role in target_roles:
        role_lower = _normalize(role)
        if role_lower in title_lower or title_lower in role_lower:
            return True
        ratio = SequenceMatcher(None, title_lower, role_lower).ratio()
        if ratio >= 0.6:
            return True
    return False


def _count_skill_matches(description: str, skills_flat: list[str]) -> int:
    desc_lower = description.lower()
    return sum(1 for skill in skills_flat if skill.lower() in desc_lower)


def keyword_filter(job, profile_data: dict) -> tuple[bool, float]:
    target_roles = profile_data.get("target_roles", [])
    if not _title_matches_roles(job.title, target_roles):
        return False, 0.0

    excluded = [c.lower() for c in profile_data.get("excluded_companies", [])]
    if job.company and job.company.lower() in excluded:
        return False, 0.0

    skills_flat = _flatten_skills(profile_data.get("skills", {}))
    if not skills_flat:
        return True, 1.0

    min_skills = getattr(settings, "MIN_KEYWORD_SKILLS", MIN_KEYWORD_SKILLS)
    matched = _count_skill_matches(job.description or "", skills_flat)
    if matched < min_skills:
        return False, 0.0

    score = matched / len(skills_flat)
    return True, score


def _build_match_prompt(job, profile_data: dict) -> list[dict[str, str]]:
    name = profile_data.get("name", "Candidate")
    summary = profile_data.get("narrative", {}).get("summary", "")
    skills_flat = _flatten_skills(profile_data.get("skills", {}))
    roles = profile_data.get("target_roles", [])
    experience = profile_data.get("experience", [])

    exp_lines = "\n".join(
        f"- {e.get('title', '')} at {e.get('company', '')} ({e.get('years', 'N/A')} years)"
        for e in experience
    )

    system_content = (
        "You are a job-match evaluator. Given a candidate profile and a job description, "
        "return a JSON object with exactly these fields:\n"
        "  score (0-100 integer),\n"
        "  reasoning (1-2 sentence string),\n"
        "  matched_skills (list of strings),\n"
        "  missing_skills (list of strings),\n"
        "  seniority_fit (boolean).\n"
        "Return ONLY the JSON object, no markdown, no explanation."
    )

    user_content = (
        f"Candidate: {name}\n"
        f"Summary: {summary}\n"
        f"Target roles: {', '.join(roles)}\n"
        f"Skills: {', '.join(skills_flat)}\n"
        f"Experience:\n{exp_lines}\n\n"
        f"Job title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Description:\n{job.description or ''}"
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _parse_llm_response(content: str) -> dict:
    if not content:
        return {"score": 0, "reasoning": "Parse error: empty response", "matched_skills": [], "missing_skills": [], "seniority_fit": False}

    text = content.strip()
    # strip ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
        return {
            "score": int(data.get("score", 0)),
            "reasoning": str(data.get("reasoning", "")),
            "matched_skills": list(data.get("matched_skills", [])),
            "missing_skills": list(data.get("missing_skills", [])),
            "seniority_fit": bool(data.get("seniority_fit", False)),
        }
    except Exception as exc:
        logger.warning("_parse_llm_response failed: %s | raw: %.200s", exc, content)
        return {"score": 0, "reasoning": f"Parse error: {exc}", "matched_skills": [], "missing_skills": [], "seniority_fit": False}


def chat_completion(messages: list[dict], api_key: str, base_url: str, model: str) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=512,
    )
    return response.choices[0].message.content or ""


_RETRY_DELAYS = [5, 15]  # seconds between attempts on 429


def llm_score_job(job, profile_data: dict, api_key: str, base_url: str, model: str) -> dict:
    messages = _build_match_prompt(job, profile_data)
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            logger.warning("llm_score_job rate-limited, retrying in %ds (attempt %d)", delay, attempt + 1)
            time.sleep(delay)
        try:
            raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
            return _parse_llm_response(raw)
        except RateLimitError as exc:
            last_exc = exc
        except Exception as exc:
            logger.error("llm_score_job failed for job %s: %s", getattr(job, "id", "?"), exc)
            return {"score": 0, "reasoning": f"LLM error: {exc}", "matched_skills": [], "missing_skills": [], "seniority_fit": False}
    # All retries exhausted on rate limit — propagate so caller can keep job as `new`
    logger.error("llm_score_job rate-limited after %d attempts for job %s", len(_RETRY_DELAYS) + 1, getattr(job, "id", "?"))
    raise last_exc


def match_job(db, job, profile_data: dict, api_key: str, base_url: str, model: str) -> str:
    """Returns 'matched', 'filtered_out', or 'rate_limited'."""
    passes, kw_score = keyword_filter(job, profile_data)

    if not passes:
        job.status = JobStatus.filtered_out
        job.keyword_score = 0.0
        job.llm_score = None
        return "filtered_out"

    job.keyword_score = round(kw_score, 4)

    try:
        llm_result = llm_score_job(job, profile_data, api_key, base_url, model)
    except RateLimitError:
        # Leave status as `new` so the next cycle retries this job
        return "rate_limited"

    score = llm_result["score"]
    min_score = profile_data.get("min_match_score", getattr(settings, "MIN_MATCH_SCORE", 70))

    job.llm_score = score
    job.llm_reasoning = llm_result["reasoning"]
    job.matched_skills = llm_result["matched_skills"]
    job.missing_skills = llm_result["missing_skills"]

    if score >= min_score:
        job.status = JobStatus.matched
        if not job.applications:
            db.add(Application(job_id=job.id))
        return "matched"
    else:
        job.status = JobStatus.filtered_out
        return "filtered_out"


def match_all_new_jobs(db) -> dict[str, int]:
    api_key = settings.NVIDIA_NIM_API_KEY
    base_url = settings.NVIDIA_NIM_BASE_URL
    model = settings.NVIDIA_NIM_MODEL

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}

    new_jobs = db.query(Job).filter(Job.status == JobStatus.new).all()

    processed = 0
    matched = 0
    filtered_out = 0
    rate_limited = 0
    errors = 0

    for job in new_jobs:
        try:
            result = match_job(db, job, profile_data, api_key, base_url, model)
            db.commit()
            processed += 1
            if result == "matched":
                matched += 1
            elif result == "rate_limited":
                rate_limited += 1
            else:
                filtered_out += 1
        except Exception as exc:
            logger.error("match_all_new_jobs error on job %s: %s", getattr(job, "id", "?"), exc)
            db.rollback()
            errors += 1

    logger.info(
        "match_all_new_jobs done — processed=%d matched=%d filtered_out=%d rate_limited=%d errors=%d",
        processed, matched, filtered_out, rate_limited, errors,
    )
    return {"processed": processed, "matched": matched, "filtered_out": filtered_out, "rate_limited": rate_limited, "errors": errors}
