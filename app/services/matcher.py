import json
import logging
import re
import time
from difflib import SequenceMatcher
from typing import NamedTuple

from openai import OpenAI, RateLimitError

from app.config import settings
from app.llm.providers import call_provider, matching_fallbacks
from app.services import eligibility
from app.services.locations import describe_prefs, location_allowed, normalize_prefs
from app.models.application import Application
from app.models.job import Job, JobStatus
from app.models.profile import Profile

logger = logging.getLogger(__name__)

MIN_KEYWORD_SKILLS = 2  # overridden by settings.MIN_KEYWORD_SKILLS if present


class LLMUnavailableError(Exception):
    """All LLM providers failed; the job should stay `new` and retry later."""


class ResponseParseError(Exception):
    """The model replied, but not with a score we can read."""


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

# How far past the candidate's experience a stated requirement may reach and
# still be worth a scoring call. Requirements are written as wishes, and the
# model can weigh substantial projects and adjacent experience against them —
# which is exactly the judgement this prefilter cannot make.
SENIORITY_YEARS_TOLERANCE = 1.5


def _blocked_by_seniority(job, profile_data: dict) -> bool:
    """
    Whether this posting is too senior to be worth a scoring call.

    A title word is a guess about the number. Now that postings state the
    number (see `services.job_details`), the number wins: a "Senior Engineer"
    asking for 3 years is a job a 2.4-year candidate should be scored against,
    and dropping it on the word "Senior" is exactly the kind of confident
    mistake that makes the list smaller than it should be.

    The title rule still applies when the posting says nothing, because a title
    is the only evidence left. Words appearing in the candidate's own target
    roles are never blocked either way.
    """
    from app.services.experience import total_years as _total_years
    from app.services.tunables import value as tunable

    if not tunable(profile_data, "filter_senior_titles"):
        return False

    total_years = _total_years(profile_data.get("experience", []))
    if total_years >= tunable(profile_data, "junior_max_years"):
        return False

    required = getattr(job, "required_years", None)
    if isinstance(required, (int, float)) and not isinstance(required, bool):
        return float(required) > total_years + SENIORITY_YEARS_TOLERANCE

    role_words = {
        w for role in profile_data.get("target_roles", [])
        for w in re.findall(r"[a-z]+", role.lower())
    }
    title_lower = (getattr(job, "title", "") or "").lower()
    return any(
        word not in role_words and re.search(rf"\b{word}\b", title_lower)
        for word in _SENIOR_TITLE_WORDS
    )


class FilterOutcome(NamedTuple):
    """Why the keyword prefilter decided what it decided."""
    passed: bool
    score: float
    reason: str | None = None   # stable key, see FILTER_REASON_LABELS
    detail: str | None = None   # sentence naming the specific values involved


# Short labels for the UI. Keys are stored on the job, so they're stable.
FILTER_REASON_LABELS = {
    "title_mismatch": "Title doesn't match target roles",
    "seniority": "Too senior for your experience",
    "location": "Outside your locations",
    "excluded_company": "Excluded company",
    "blocked_title": "Title contains a word you blocked",
    "few_skills": "Too few skills in description",
    "no_description": "No job description available",
    "low_score": "AI score below your minimum",
    "restricted": "Restricted to US citizens",
    "duplicate": "Same posting already has an application",
    "manual": "You filtered it manually",
}

# Verdicts that were reached by reading the description, and are therefore
# worth reaching again once there is more of it. `low_score` is the one that
# matters most by volume: a job scored 45 on Adzuna's 500-character stub is a
# job scored on a teaser, and the real posting routinely tells a different
# story.
#
# `title_mismatch` and `location` are deliberately absent. Neither reads the
# description, so re-scoring them would cost a call and reach the same answer.
DESCRIPTION_DEPENDENT_REASONS = frozenset({
    "no_description", "few_skills", "low_score", "restricted", "seniority",
})

# Verdicts the user made. A fuller description is not a reason to overrule
# somebody who looked at a job and said no.
USER_CHOICE_REASONS = frozenset({
    "manual", "blocked_title", "excluded_company", "duplicate",
})


def _title_match_roles(profile_data: dict) -> list[str]:
    """
    Every phrasing a matching title may arrive under.

    The LLM already expands the target roles into the titles recruiters
    actually post ("Software Engineer" → "Java Developer", "Backend
    Developer") and the fetcher searches under all of them — but the title
    gate only knew the raw roles, so a job found BY an expanded query could
    then be rejected for not matching it. The expansion is cached on the
    profile by the fetch cycle, so reading it here costs nothing.
    """
    roles = list(profile_data.get("target_roles") or [])
    expanded = (profile_data.get("search_query_cache") or {}).get("queries") or []
    seen = {r.lower().strip() for r in roles}
    for query in expanded:
        text = str(query).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            roles.append(text)
    return roles


def _blocked_title_word(title: str, profile_data: dict) -> str | None:
    """The first user-blocked word this title contains, or None."""
    blocked = profile_data.get("blocked_title_words") or []
    title_lower = (title or "").lower()
    for word in blocked:
        text = str(word).strip().lower()
        if text and re.search(rf"\b{re.escape(text)}\b", title_lower):
            return str(word).strip()
    return None


def evaluate_keyword_filter(job, profile_data: dict, scan=None) -> FilterOutcome:
    """
    The keyword prefilter, with its reasoning.

    Five distinct rejections used to be indistinguishable — every one returned
    (False, 0.0) — so a filtered job gave no clue whether the title was wrong,
    the location was, or the description simply never arrived. Each now names
    itself and the values that triggered it.

    `scan` is an already-computed `eligibility.scan()` result. Callers that need
    the advisory half of it anyway pass theirs in rather than paying for a
    second pass over the description.
    """
    target_roles = _title_match_roles(profile_data)
    if not _title_matches_roles(job.title, target_roles):
        roles = ", ".join((profile_data.get("target_roles") or [])[:5]) or "none set"
        return FilterOutcome(
            False, 0.0, "title_mismatch",
            f"Title {job.title!r} shares no keyword with your target roles ({roles}) "
            "or their expanded variants.",
        )

    blocked_word = _blocked_title_word(job.title, profile_data)
    if blocked_word:
        return FilterOutcome(
            False, 0.0, "blocked_title",
            f"Title contains {blocked_word!r}, which you blocked from the jobs list.",
        )

    if _blocked_by_seniority(job, profile_data):
        from app.services.experience import total_years as _total_years

        required = getattr(job, "required_years", None)
        if isinstance(required, (int, float)) and not isinstance(required, bool):
            # The posting stated a number, so the reason names the number
            # rather than a word we read off the title.
            yours = _total_years(profile_data.get("experience", []))
            detail = (
                f"The posting asks for {float(required):g} years; your profile "
                f"shows {yours:g}, more than {SENIORITY_YEARS_TOLERANCE:g} short."
            )
        else:
            hit = next(
                (w for w in _SENIOR_TITLE_WORDS
                 if re.search(rf"\b{w}\b", (job.title or "").lower())),
                "senior",
            )
            max_years = getattr(settings, "JUNIOR_MAX_YEARS", 3.0)
            detail = (
                f"Title contains {hit!r} and the posting states no required "
                f"years, which is filtered while your profile shows under "
                f"{max_years:g} years of experience."
            )
        return FilterOutcome(False, 0.0, "seniority", detail)

    # Drop jobs whose location clearly belongs to a region the candidate did
    # not choose; ambiguous/unknown locations continue to the LLM.
    loc_text = job.location if isinstance(getattr(job, "location", None), str) else ""
    if location_allowed(loc_text, bool(getattr(job, "is_remote", False)),
                        normalize_prefs(profile_data)) is False:
        wanted = describe_prefs(normalize_prefs(profile_data))
        return FilterOutcome(
            False, 0.0, "location",
            f"Location {loc_text or 'unknown'!r} is outside your preferences ({wanted}).",
        )

    excluded = [c.lower() for c in profile_data.get("excluded_companies", [])]
    if job.company and job.company.lower() in excluded:
        return FilterOutcome(
            False, 0.0, "excluded_company",
            f"{job.company} is on your excluded-companies list.",
        )

    # Postings that say outright they are closed to non-citizens. Checked after
    # the cheap title/location tests so it only runs on jobs that were otherwise
    # worth considering, which is also the only case where the answer matters.
    if scan is None:
        scan = eligibility.scan(job.description)
    if scan.blocked:
        return FilterOutcome(
            False, 0.0, "restricted",
            f"{scan.restriction_label}. The posting says: “{scan.restriction_quote}”",
        )

    skills_flat = _flatten_skills(profile_data.get("skills", {}))
    if not skills_flat:
        return FilterOutcome(True, 1.0)

    from app.services.tunables import value as tunable
    min_skills = tunable(profile_data, "min_keyword_skills")
    description = job.description or ""
    matched = _count_skill_matches(description, skills_flat)
    if matched < min_skills:
        # An empty description is a fetch problem, not a bad job — worth saying
        # so, because the fix is on the source side rather than the filters.
        if not description.strip():
            return FilterOutcome(
                False, 0.0, "no_description",
                "The source returned no description, so skills couldn't be matched.",
            )
        return FilterOutcome(
            False, 0.0, "few_skills",
            f"Only {matched} of your {len(skills_flat)} skills appear in the "
            f"description; the minimum is {min_skills}.",
        )

    return FilterOutcome(True, matched / len(skills_flat))


def keyword_filter(job, profile_data: dict) -> tuple[bool, float]:
    """Pass/score only — see evaluate_keyword_filter for the reasoning."""
    outcome = evaluate_keyword_filter(job, profile_data)
    return outcome.passed, outcome.score


def _description_for_prompt(job) -> str:
    """
    The posting as the model should see it: all of it, for any real posting.

    It used to be the first 4,000 characters, chosen when descriptions were
    mostly Adzuna's 500-character stubs and the ceiling never bound. Now that
    enrichment fetches the real text, 4,000 characters routinely cut off
    mid-requirements — so the model was scoring seniority and skill fit against
    the marketing half of the posting and never saw the part that says what the
    job needs.

    There is still a ceiling, because "the full text" and "unbounded" are not
    the same promise. A page that cleaned badly can be hundreds of kilobytes,
    and putting that into every scoring call would cost minutes per batch for
    text that is not a job description at all. The default is several times
    longer than the longest real posting.
    """
    text = job.description or ""
    limit = max(1000, int(getattr(settings, "MATCH_DESCRIPTION_CHARS", 24000)))
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[description truncated]"


def _stated_facts(job) -> str:
    """
    The posting's own numbers, as explicit lines above the description.

    Extracted once into columns (see `services.job_details`), so the scoring
    call reads "Required experience: 3 years" instead of hunting for it in the
    same prose it is being asked to judge. Silent when nothing was stated —
    an empty "Salary:" line invites the model to fill the gap itself.
    """
    # Every read is both optional and type-checked. This runs against real
    # rows, against the sample job the settings page previews the prompt with,
    # and against rows written before these columns existed — and a line that
    # cannot be rendered must cost the line, not the whole scoring call.
    def _number(field) -> float | None:
        value = getattr(job, field, None)
        return float(value) if isinstance(value, (int, float)) and not isinstance(
            value, bool) else None

    def _string(field) -> str | None:
        value = getattr(job, field, None)
        return value.strip() or None if isinstance(value, str) else None

    def _strings(field) -> list[str]:
        value = getattr(job, field, None)
        if not isinstance(value, (list, tuple)):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    lines = []
    years = _number("required_years")
    if years is not None:
        lines.append(f"Required experience (stated in the posting): {years:g} years")
    label = getattr(job, "salary_label", None)
    if isinstance(label, str) and label:
        lines.append(f"Stated salary: {label}")
    employment = _string("employment_type")
    if employment:
        lines.append(f"Employment type: {employment.replace('_', ' ')}")
    required = _strings("required_skills")
    if required:
        lines.append(f"Required skills: {', '.join(required)}")
    nice = _strings("nice_to_have_skills")
    if nice:
        lines.append(f"Nice to have: {', '.join(nice)}")
    education = _string("education_required")
    if education:
        lines.append(f"Education required: {education}")
    return "\n".join(lines) + "\n" if lines else ""


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

    # Derived from the start/end dates rather than asked for separately: the
    # rubric leans on the total, and no form ever collected a years field.
    from app.services.experience import entry_years, total_years as sum_years

    total_years = sum_years(experience)

    def _span(entry) -> str:
        years = entry_years(entry)
        if years is not None:
            return f"{years} years"
        dates = " to ".join(x for x in (entry.get("start_date"),
                                        entry.get("end_date")) if x)
        return dates or "dates not given"

    exp_lines = "\n".join(
        f"- {e.get('title') or e.get('role') or ''} at {e.get('company', '')} ({_span(e)})"
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
        # One decimal, not rounded to whole years: 2.6 shown as "3" would push
        # the candidate over thresholds the rubric is asked to police.
        extras.append(f"Total experience: {total_years:g} years "
                      f"(overlapping roles counted once)")
    if remote_pref and remote_pref != "any":
        extras.append(f"Work preference: {remote_pref}")
    if salary_min:
        extras.append(f"Minimum salary: ${salary_min:,}")
    extras.append(f"Preferred locations: {describe_prefs(normalize_prefs(profile_data))}")
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
        "  - Location/remote compatibility: 0-15. Reward remote-friendly jobs "
        "when the candidate prefers remote.\n"
        "Treat transferable skills generously (e.g. strong Java experience for a Kotlin role), "
        "but never ignore explicit hard requirements stated in the job (specific degrees, "
        "must-have technologies).\n"
        "Ignore visa, work-authorization and sponsorship considerations entirely: they are "
        "handled outside this scoring step and must not affect the score or the reasoning.\n"
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
        + _stated_facts(job)
        + f"Description:\n{_description_for_prompt(job)}"
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _extract_json_object(text: str) -> dict:
    """
    Find the scoring object in a model response.

    Plain `json.loads` on the whole reply only works for models that emit
    nothing but JSON. Reasoning models wrap it in thinking, and chattier ones
    add a sentence either side, so fall back to the first balanced {...} span.
    """
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    if not text:
        raise ResponseParseError("empty response")

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                    except Exception:
                        break
                    if isinstance(data, dict):
                        return data
                    break
        start = text.find("{", start + 1)

    raise ResponseParseError(f"no JSON object found in {text[:120]!r}")


def _parse_llm_response(content: str) -> dict:
    """
    The scoring fields, or ResponseParseError.

    Deliberately not "score 0 on failure": that flowed straight into the
    minimum-score check and filtered the job out with the reason "AI scored
    this 0/100", so a formatting hiccup silently discarded a job and blamed the
    score for it. An unparseable reply means we don't know, and the caller
    leaves the job to be retried.
    """
    data = _extract_json_object(content)
    if "score" not in data:
        raise ResponseParseError(f"no score field in {sorted(data)[:8]}")
    try:
        score = int(float(data["score"]))
    except Exception as exc:
        raise ResponseParseError(f"score {data['score']!r} is not a number") from exc

    return {
        "score": max(0, min(100, score)),
        "reasoning": str(data.get("reasoning", "")),
        "matched_skills": [str(s) for s in (data.get("matched_skills") or [])],
        "missing_skills": [str(s) for s in (data.get("missing_skills") or [])],
        "seniority_fit": bool(data.get("seniority_fit", True)),
    }


def _match_max_tokens() -> int:
    """
    Room for a matching reply, thinking included.

    A reasoning model spends tokens before it emits anything, so a ceiling
    sized for the JSON alone cuts the object in half and the parse fails —
    which reads as the model being bad at the task rather than as a budget.
    """
    return max(256, int(getattr(settings, "NIM_MATCH_MAX_TOKENS", 1536)))


def chat_completion(
    messages: list[dict],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> str:
    from app.services import llm_log

    ceiling = max_tokens if max_tokens is not None else _match_max_tokens()
    with llm_log.call("nim", model, messages,
                      temperature=temperature, max_tokens=ceiling) as entry:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=ceiling,
            timeout=90,
        )
        message = response.choices[0].message
        # Logged as the model actually returned them, not as _reply_text folds
        # them together — an empty `content` beside a full `reasoning_content`
        # is the signature of a token ceiling that was too low, and merging the
        # two hides exactly that.
        entry.finish(
            getattr(message, "content", None) or "",
            reasoning=getattr(message, "reasoning_content", None),
            raw=response,
        )
        return _reply_text(response)


def _reply_text(response) -> str:
    """
    The model's answer, wherever it put it.

    Reasoning models on NIM return their thinking in `reasoning_content` and the
    answer in `content`, which is the good case: the answer arrives clean. But
    when the ceiling is reached mid-thought `content` comes back empty, and
    returning "" throws away the only text there is — the scoring object is
    often already inside the thinking, and the parser hunts for a balanced
    object rather than assuming the reply is pure JSON.
    """
    message = response.choices[0].message
    content = (getattr(message, "content", None) or "").strip()
    if content:
        return content
    return (getattr(message, "reasoning_content", None) or "").strip()


def _rpm_interval() -> float:
    """Minimum seconds to wait between LLM calls to stay under the RPM limit."""
    rpm = getattr(settings, "NVIDIA_NIM_RPM", 40)
    return 60.0 / max(rpm, 1)


def _retry_delays() -> list[int]:
    """Wait durations on 429: one short pause then a full minute window reset."""
    interval = _rpm_interval()
    return [int(interval * 2), 65]


def _score_via_fallbacks(messages: list[dict], job, budget: dict | None = None) -> dict | None:
    """
    Try the secondary (paid) providers; None if all fail, none are set, or the
    per-cycle paid-call budget is exhausted. `budget` is a mutable counter dict
    shared across one matching cycle: {"paid_calls": int}.
    """
    cap = getattr(settings, "MAX_PAID_MATCH_CALLS_PER_CYCLE", 150)
    for provider in matching_fallbacks():
        # The cap exists to stop a NIM outage turning into a surprise bill. A
        # provider that cannot bill — a fixed free daily allowance — has nothing
        # to be surprised by, and counting its calls would spend the budget
        # protecting against a cost that does not exist.
        billable = getattr(provider, "paid", True)
        if billable and budget is not None and cap and budget.get("paid_calls", 0) >= cap:
            # Skip this one rather than abandoning the chain: a free provider
            # further down is still worth trying, and the loop falls out to
            # None on its own if nothing serves the call.
            logger.warning(
                "llm_score_job: paid-call budget (%d) exhausted this cycle; "
                "skipping %s for job %s", cap, provider.name, getattr(job, "id", "?"),
            )
            continue
        try:
            raw = call_provider(
                provider, messages, temperature=0.1, max_tokens=_match_max_tokens()
            )
            # Counted after the call returns: a provider that refused or timed
            # out billed nothing, and charging failures to the budget could
            # burn the whole cap on one broken provider without a single score.
            if billable and budget is not None:
                budget["paid_calls"] = budget.get("paid_calls", 0) + 1
            logger.info(
                "llm_score_job: scored job %s via fallback provider %s (%s)",
                getattr(job, "id", "?"), provider.name, provider.model,
            )
            result = _parse_llm_response(raw)
            result["scored_by"] = f"{provider.name}/{provider.model}"
            return result
        except Exception as exc:
            logger.warning(
                "llm_score_job: fallback provider %s failed: %s", provider.name, exc
            )
    return None


def llm_score_job(
    job, profile_data: dict, api_key: str, base_url: str, model: str,
    budget: dict | None = None,
) -> dict:
    from app.services import llm_log

    with llm_log.stage("match", job_id=getattr(job, "id", None)):
        return _llm_score_job(job, profile_data, api_key, base_url, model, budget)


def _llm_score_job(
    job, profile_data: dict, api_key: str, base_url: str, model: str,
    budget: dict | None = None,
) -> dict:
    messages = _build_match_prompt(job, profile_data)
    delays = _retry_delays()
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + delays):
        if delay:
            logger.warning("llm_score_job rate-limited, retrying in %ds (attempt %d)", delay, attempt + 1)
            time.sleep(delay)
        try:
            raw = chat_completion(messages=messages, api_key=api_key, base_url=base_url, model=model)
            result = _parse_llm_response(raw)
            result["scored_by"] = f"nim/{model}"
            return result
        except RateLimitError as exc:
            last_exc = exc
        except ResponseParseError as exc:
            # Fall through to the other providers rather than scoring 0: the
            # model is reachable, it just isn't answering in the agreed shape.
            logger.warning("llm_score_job: unreadable reply from %s: %s", model, exc)
            last_exc = exc
            break
        except Exception as exc:
            logger.error("llm_score_job failed for job %s: %s", getattr(job, "id", "?"), exc)
            last_exc = exc
            break

    # Primary provider exhausted — try the configured fallback providers before
    # giving up, so a NIM outage/rate-limit doesn't stall matching.
    result = _score_via_fallbacks(messages, job, budget)
    if result is not None:
        return result

    if isinstance(last_exc, RateLimitError):
        logger.error("llm_score_job rate-limited after %d attempts for job %s", len(delays) + 1, getattr(job, "id", "?"))
        raise last_exc
    # Propagate instead of returning score 0: a transient LLM failure must not
    # cause the job to be filtered out — the caller keeps it `new` to retry.
    raise LLMUnavailableError(str(last_exc)) from last_exc


def match_job(
    db, job, profile_data: dict, api_key: str, base_url: str, model: str,
    budget: dict | None = None,
) -> str:
    """Returns 'matched', 'filtered_out', or 'rate_limited'."""
    # One pass over the description feeds both halves of the eligibility read.
    # The advisory half is recorded whatever happens next — including on jobs
    # that go on to be filtered for an unrelated reason — because the note is a
    # fact about the posting rather than a step in deciding its fate.
    scan = eligibility.scan(job.description)
    job.sponsorship_note = scan.sponsorship_note
    job.sponsorship_direction = scan.sponsorship_direction

    outcome = evaluate_keyword_filter(job, profile_data, scan=scan)

    if not outcome.passed:
        job.status = JobStatus.filtered_out
        job.keyword_score = 0.0
        job.llm_score = None
        job.filter_reason = outcome.reason
        job.filter_detail = outcome.detail
        return "filtered_out"

    job.keyword_score = round(outcome.score, 4)

    # Read the posting's stated facts before scoring it, so the prompt below
    # gets "asks for 3 years, pays $140-170k" as data instead of leaving the
    # model to find both in the prose it is also being asked to judge. Placed
    # after the filter on purpose: a title-reject never costs this call.
    from app.services import job_details

    if job_details.needs_extraction(job):
        try:
            job_details.extract_and_apply(job)
        except Exception as exc:
            # Details are an improvement to scoring, not a precondition for it.
            logger.warning("match_job: detail extraction failed for %s: %s", job.id, exc)

    try:
        llm_result = llm_score_job(job, profile_data, api_key, base_url, model, budget=budget)
    except (RateLimitError, LLMUnavailableError):
        # Leave status as `new` so the next cycle retries this job
        return "rate_limited"

    score = llm_result["score"]
    # Seniority mismatch: penalize but don't hard-block (role might still be worth applying)
    if not llm_result.get("seniority_fit", True):
        score = max(0, score - 15)

    from app.services.tunables import value as tunable
    min_score = tunable(profile_data, "min_match_score")

    job.llm_score = score
    job.llm_reasoning = llm_result["reasoning"]
    job.matched_skills = llm_result["matched_skills"]
    job.missing_skills = llm_result["missing_skills"]
    job.matched_by = llm_result.get("scored_by")

    if score >= min_score:
        if not job.applications:
            # A cross-post of a job that already has an application must not
            # buy a second full document generation. The dedupe hash catches
            # exact matches at fetch time; this catches the near-misses
            # ("Backend Engineer" vs "Backend Engineer - Remote").
            from app.services.deduplication import find_duplicate_application_job

            duplicate = find_duplicate_application_job(db, job)
            if duplicate is not None:
                job.status = JobStatus.filtered_out
                job.filter_reason = "duplicate"
                job.filter_detail = (
                    f"Scored {score}/100, but {duplicate.title!r} at "
                    f"{duplicate.company} already has an application — this "
                    "looks like the same posting cross-posted."
                )
                return "filtered_out"
            db.add(Application(job_id=job.id))
        job.status = JobStatus.matched
        # Clear any reason from a previous cycle so a re-matched job isn't
        # still carrying an explanation for why it used to be rejected.
        job.filter_reason = None
        job.filter_detail = None
        return "matched"

    job.status = JobStatus.filtered_out
    job.filter_reason = "low_score"
    penalty = " (after a 15-point seniority penalty)" if not llm_result.get(
        "seniority_fit", True) else ""
    job.filter_detail = (
        f"AI scored this {score}/100{penalty}, below your minimum of {min_score}."
    )
    return "filtered_out"


def count_unmatched(db) -> int:
    """How many jobs are still waiting to be scored."""
    return db.query(Job).filter(Job.status == JobStatus.new).count()


def match_all_new_jobs(db, limit: int | None = None, on_matched=None) -> dict[str, int]:
    """
    Score jobs still sitting at `new`.

    `limit` bounds one pass. Matching a large backlog in a single call means one
    task holding a worker slot for the length of every LLM round trip put
    together, and a worker restarted anywhere in that window loses the lot; a
    bounded pass that the caller repeats makes progress durable.

    `on_matched(job)` fires as each job crosses the threshold rather than after
    the pass, so the documents for the first match are being written while the
    hundredth is still being scored.

    `remaining` in the result is how many are still `new` afterwards — the
    caller's cue to come back for another pass.
    """
    from app.services.tunables import value as tunable

    api_key = settings.NVIDIA_NIM_API_KEY
    base_url = settings.NVIDIA_NIM_BASE_URL
    pace_interval = _rpm_interval()

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}
    model = tunable(profile_data, "nvidia_nim_model")

    query = db.query(Job).filter(Job.status == JobStatus.new).order_by(Job.fetched_at.desc())
    if limit is not None:
        query = query.limit(limit)
    new_jobs = query.all()

    processed = 0
    matched = 0
    filtered_out = 0
    rate_limited = 0
    errors = 0
    # One shared paid-call budget for the whole cycle (see _score_via_fallbacks).
    budget = {"paid_calls": 0}

    for job in new_jobs:
        try:
            result = match_job(db, job, profile_data, api_key, base_url, model, budget=budget)
            db.commit()
            processed += 1
            if result == "matched":
                matched += 1
                if on_matched is not None:
                    # A failure here is a queueing problem, not a matching one:
                    # the score is already committed, and the sweeper picks up
                    # anything that never got queued.
                    try:
                        on_matched(job)
                    except Exception as exc:
                        logger.error("match_all_new_jobs: on_matched failed for %s: %s",
                                     job.id, exc)
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

    remaining = count_unmatched(db)
    logger.info(
        "match_all_new_jobs done — processed=%d matched=%d filtered_out=%d "
        "rate_limited=%d errors=%d paid_llm_calls=%d remaining=%d",
        processed, matched, filtered_out, rate_limited, errors,
        budget["paid_calls"], remaining,
    )
    return {"processed": processed, "matched": matched, "filtered_out": filtered_out,
            "rate_limited": rate_limited, "errors": errors,
            "paid_llm_calls": budget["paid_calls"], "remaining": remaining}
