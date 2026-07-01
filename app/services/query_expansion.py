import hashlib
import json
import logging
import re

from app.services.matcher import chat_completion

logger = logging.getLogger(__name__)

MAX_QUERIES = 10
MAX_GENERATED = 5


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _basis_hash(roles: list[str], skills_flat: list[str]) -> str:
    payload = json.dumps([sorted(roles), sorted(skills_flat)], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()


def _dedupe_capped(queries: list[str], cap: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for q in queries:
        key = q.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(q.strip())
        if len(result) >= cap:
            break
    return result


def expand_search_queries(
    profile_data: dict,
    api_key: str,
    base_url: str,
    model: str,
) -> tuple[list[str], dict | None]:
    """
    Expand target roles into the fuller set of search queries job boards should be
    hit with (synonyms and adjacent titles recruiters actually post under, e.g.
    'Software Engineer' → 'Software Developer', 'Java Developer').

    Returns (queries, cache_entry). cache_entry is a dict to persist on the profile
    under 'search_query_cache' when the expansion is fresh, or None when the cached
    value was reused (or expansion failed — failures are never cached).
    """
    roles = profile_data.get("target_roles") or []
    if not roles:
        return [], None

    skills_flat = [
        s for cat in (profile_data.get("skills") or {}).values() for s in (cat or [])
    ]
    basis = _basis_hash(roles, skills_flat)
    cache = profile_data.get("search_query_cache") or {}
    if cache.get("basis") == basis and cache.get("queries"):
        return list(cache["queries"]), None

    messages = [
        {
            "role": "system",
            "content": (
                "You generate job-board search queries for a job seeker. Given their "
                f"target roles and skills, return ONLY a JSON array of up to {MAX_GENERATED} "
                "ADDITIONAL queries — job titles recruiters actually post under that are "
                "synonyms or close neighbors of the target roles and are supported by the "
                "candidate's strongest skills (e.g. 'Software Engineer' + Java suggests "
                "'Java Developer').\n"
                "Rules: 2-4 words each, plain titles only (no boolean operators, no "
                "locations, no seniority qualifiers), no duplicates of the given roles."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Target roles: {', '.join(roles)}\n"
                f"Skills: {', '.join(skills_flat)}"
            ),
        },
    ]
    try:
        raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
        extra = json.loads(_strip_json_fences(raw))
        extra = [str(q).strip() for q in extra if isinstance(q, (str, int)) and str(q).strip()]
    except Exception as exc:
        logger.error("expand_search_queries LLM error: %s", exc)
        return list(roles), None

    queries = _dedupe_capped(list(roles) + extra, MAX_QUERIES)
    logger.info("expand_search_queries: %d roles → %d queries: %s", len(roles), len(queries), queries)
    return queries, {"basis": basis, "queries": queries}
