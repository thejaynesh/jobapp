import json
import logging
import re
import time
from difflib import SequenceMatcher

from openai import OpenAI, RateLimitError

from app.config import settings
from app.llm.providers import call_provider, matching_fallbacks
from app.models.application import Application
from app.models.job import Job, JobStatus
from app.models.profile import Profile

logger = logging.getLogger(__name__)

MIN_KEYWORD_SKILLS = 2  # overridden by settings.MIN_KEYWORD_SKILLS if present


class LLMUnavailableError(Exception):
    """All LLM providers failed; the job should stay `new` and retry later."""


_STOP = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "at", "for", "to", "with",
    "as", "is", "be", "are", "was", "were", "it", "on", "by", "from",
})


def _normalize(text: str) -> str:
    return text.lower().strip()


def _flatten_skills(skills_data: dict) -> list[str]:
    result = []
    for category_skills in skills_data.values():
        result.extend(category_skills)
    return result


def _title_matches_roles(title: str, target_roles: list[str]) -> bool:
    title_lower = _normalize(title)
    title_words = set(re.findall(r'\b[a-z]+\b', title_lower)) - _STOP
    for role in target_roles:
        role_lower = _normalize(role)
        role_words = set(re.findall(r'\b[a-z]+\b', role_lower)) - _STOP
        # Match if any meaningful word overlaps (e.g. "engineer" in both)
        if role_words and (role_words & title_words):
            return True
        # Fallback sequence ratio for short/abbreviated titles
        if SequenceMatcher(None, title_lower, role_lower).ratio() >= 0.7:
            return True
    return False


def _count_skill_matches(description: str, skills_flat: list[str]) -> int:
    desc_lower = description.lower()
    count = 0
    for skill in skills_flat:
        s = skill.lower()
        if " " in s:
            # Multi-word skills: simple substring is fine
            if s in desc_lower:
                count += 1
        elif re.match(r'^\w+$', s):
            # Pure alphanumeric: word boundaries prevent false positives (java ≠ javascript)
            if re.search(r'\b' + re.escape(s) + r'\b', desc_lower):
                count += 1
        else:
            # Special chars (c++, c#, node.js): use lookaround instead of \b
            if re.search(r'(?<![a-z0-9])' + re.escape(s) + r'(?![a-z0-9])', desc_lower):
                count += 1
    return count


_SENIOR_TITLE_WORDS = ("senior", "sr", "staff", "principal", "lead", "director", "vp", "head")


def _blocked_by_seniority(title: str, profile_data: dict) -> bool:
    """
    Junior candidates waste LLM calls (and get penalized anyway) on jobs whose
    TITLE is explicitly senior-level, so drop them up front. Only title words
    count — 'senior' in a description is too noisy. Words that appear in the
    candidate's own target roles are never blocked.
    """
    if not getattr(settings, "FILTER_SENIOR_TITLES", True):
        return False
    total_years = sum(
        float(e.get("years", 0) or 0) for e in profile_data.get("experience", [])
    )
    if total_years >= getattr(settings, "JUNIOR_MAX_YEARS", 3.0):
        return False

    role_words = {
        w for role in profile_data.get("target_roles", [])
        for w in re.findall(r"[a-z]+", role.lower())
    }
    title_lower = title.lower()
    return any(
        word not in role_words and re.search(rf"\b{word}\b", title_lower)
        for word in _SENIOR_TITLE_WORDS
    )


def keyword_filter(job, profile_data: dict) -> tuple[bool, float]:
    target_roles = profile_data.get("target_roles", [])
    if not _title_matches_roles(job.title, target_roles):
        return False, 0.0

    if _blocked_by_seniority(job.title, profile_data):
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
    personal = profile_data.get("personal") or {}
    name = personal.get("name") or profile_data.get("name") or "Candidate"
    summary = profile_data.get("narrative", {}).get("summary", "")
    skills_flat = _flatten_skills(profile_data.get("skills", {}))
    roles = profile_data.get("target_roles", [])
    experience = profile_data.get("experience", [])
    remote_pref = profile_data.get("remote_preference", "any")
    salary_min = profile_data.get("salary_min")
    education = profile_data.get("education", [])

    projects = profile_data.get("projects", [])

    total_years = sum(float(e.get("years", 0) or 0) for e in experience)

    exp_lines = "\n".join(
        f"- {e.get('title') or e.get('role') or ''} at {e.get('company', '')} ({e.get('years', 'N/A')} years)"
        + (f" — tech: {', '.join(e.get('tech'))}" if e.get("tech") else "")
        for e in experience
    )

    proj_lines = "\n".join(
        f"- {p.get('name', '')}: {p.get('description', '')}"
        + (f" — tech: {', '.join(p.get('tech'))}" if p.get("tech") else "")
        for p in projects
    ) if projects else ""

    edu_lines = "\n".join(
        f"- {e.get('degree', '')} in {e.get('field', '')} from {e.get('school', '')}"
        + (f" (expected {e.get('end_date')})" if e.get("end_date") else "")
        for e in education
    ) if education else ""

    extras = []
    if total_years:
        extras.append(f"Total experience: {total_years:.0f} years")
    if remote_pref and remote_pref != "any":
        extras.append(f"Work preference: {remote_pref}")
    if salary_min:
        extras.append(f"Minimum salary: ${salary_min:,}")
    extras_str = "\n".join(extras)

    system_content = (
        "You are a job-match evaluator. Given a candidate profile and a job description, "
        "return a JSON object with exactly these fields:\n"
        "  score (0-100 integer — how well this job fits the candidate),\n"
        "  reasoning (1-2 sentence string explaining the score),\n"
        "  matched_skills (list of skills from the candidate that appear in the job),\n"
        "  missing_skills (list of skills the job requires that the candidate lacks),\n"
        "  seniority_fit (boolean — true if the job seniority matches the candidate's experience level).\n"
        "Score with this rubric, then sum:\n"
        "  - Core skill overlap with the job's REQUIRED (not nice-to-have) skills: 0-40\n"
        "  - Seniority/years fit: 0-25. Judge required years against the candidate's total; "
        "count substantial personal/academic projects as evidence of ability but not as years. "
        "A recent or soon-graduating Master's candidate is a fit for entry/new-grad/junior roles "
        "and roles asking up to ~3 years; heavily penalize roles demanding 5+ years or 'senior/staff/lead' titles.\n"
        "  - Domain and role-type fit (backend vs mobile vs data etc., industry): 0-20\n"
        "  - Location/remote/work-authorization compatibility: 0-15. Reward remote-friendly jobs "
        "when the candidate prefers remote.\n"
        "Treat transferable skills generously (e.g. strong Java experience for a Kotlin role), "
        "but never ignore explicit hard requirements stated in the job (clearances, specific "
        "degrees, must-have technologies).\n"
        "Return ONLY the JSON object, no markdown, no explanation."
    )

    user_content = (
        f"Candidate: {name}\n"
        f"Summary: {summary}\n"
        f"Target roles: {', '.join(roles)}\n"
        f"Skills: {', '.join(skills_flat)}\n"
        f"Experience:\n{exp_lines}\n"
        + (f"Projects:\n{proj_lines}\n" if proj_lines else "")
        + (f"Education:\n{edu_lines}\n" if edu_lines else "")
        + (f"{extras_str}\n" if extras_str else "")
        + f"\nJob title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'Unknown'} (remote: {job.is_remote})\n"
        f"Experience level: {job.experience_level or 'unknown'}\n"
        f"Description:\n{(job.description or '')[:4000]}"
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


def chat_completion(
    messages: list[dict],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.1,
    max_tokens: int = 512,
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=90,
    )
    return response.choices[0].message.content or ""


def _rpm_interval() -> float:
    """Minimum seconds to wait between LLM calls to stay under the RPM limit."""
    rpm = getattr(settings, "NVIDIA_NIM_RPM", 40)
    return 60.0 / max(rpm, 1)


def _retry_delays() -> list[int]:
    """Wait durations on 429: one short pause then a full minute window reset."""
    interval = _rpm_interval()
    return [int(interval * 2), 65]


def _score_via_fallbacks(messages: list[dict], job) -> dict | None:
    """Try the secondary providers (Gemini/Anthropic); None if all fail or none set."""
    for provider in matching_fallbacks():
        try:
            raw = call_provider(provider, messages, temperature=0.1, max_tokens=512)
            logger.info(
                "llm_score_job: scored job %s via fallback provider %s",
                getattr(job, "id", "?"), provider.name,
            )
            return _parse_llm_response(raw)
        except Exception as exc:
            logger.warning(
                "llm_score_job: fallback provider %s failed: %s", provider.name, exc
            )
    return None


def llm_score_job(job, profile_data: dict, api_key: str, base_url: str, model: str) -> dict:
    messages = _build_match_prompt(job, profile_data)
    delays = _retry_delays()
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + delays):
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
            last_exc = exc
            break

    # Primary provider exhausted — try the configured fallback providers before
    # giving up, so a NIM outage/rate-limit doesn't stall matching.
    result = _score_via_fallbacks(messages, job)
    if result is not None:
        return result

    if isinstance(last_exc, RateLimitError):
        logger.error("llm_score_job rate-limited after %d attempts for job %s", len(delays) + 1, getattr(job, "id", "?"))
        raise last_exc
    # Propagate instead of returning score 0: a transient LLM failure must not
    # cause the job to be filtered out — the caller keeps it `new` to retry.
    raise LLMUnavailableError(str(last_exc)) from last_exc


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
    except (RateLimitError, LLMUnavailableError):
        # Leave status as `new` so the next cycle retries this job
        return "rate_limited"

    score = llm_result["score"]
    # Seniority mismatch: penalize but don't hard-block (role might still be worth applying)
    if not llm_result.get("seniority_fit", True):
        score = max(0, score - 15)

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
    pace_interval = _rpm_interval()

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
            # Pace only when the LLM was actually called or attempted
            if result in ("matched", "rate_limited") or job.llm_score is not None:
                time.sleep(pace_interval)
        except Exception as exc:
            logger.error("match_all_new_jobs error on job %s: %s", getattr(job, "id", "?"), exc)
            db.rollback()
            errors += 1

    logger.info(
        "match_all_new_jobs done — processed=%d matched=%d filtered_out=%d rate_limited=%d errors=%d",
        processed, matched, filtered_out, rate_limited, errors,
    )
    return {"processed": processed, "matched": matched, "filtered_out": filtered_out, "rate_limited": rate_limited, "errors": errors}
