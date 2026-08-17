"""
Reading the facts out of a job description, once.

Salary, required years, the skills a posting actually demands, whether it is an
internship — all of it sits in the description as prose, and everything that
wants it re-derives it from text. The matcher asks a model to weigh "required
years" against the candidate's experience while only ever seeing paragraphs;
the jobs page cannot filter on pay because pay is a sentence; the card cannot
show any of it.

So one call per job reads it into columns, and everything downstream reads
columns. The call happens after the keyword filter passes, which is the whole
reason it is affordable: a title-reject never costs one.

The rule the prompt is built around, and the one worth defending in review:
**null when the posting does not say.** A guessed salary is worse than a
missing one, because the salary floor filter would then drop jobs on a number
nobody wrote down. Same for years: "5+ years preferred" is a number; "senior
role" is not.
"""

import json
import logging
import re
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger(__name__)

# Enough description to hold the requirements section, which is where almost
# everything here is stated.
MAX_DESCRIPTION_CHARS = 16_000

# Below this there is nothing to read, and a call would return nulls.
MIN_DESCRIPTION_CHARS = 200

EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship")

# Long enough to be a real note, short enough not to become a second copy of
# the description in a column meant for a summary.
_MAX_BENEFITS_CHARS = 400
_MAX_SKILLS = 25

_SYSTEM_PROMPT = (
    "You extract structured facts from a job posting. Return ONLY a JSON "
    "object, no prose and no markdown, with exactly these keys:\n"
    '  "salary_min", "salary_max": numbers the posting states, or null. Use the '
    "annual figure when the posting gives one; if it quotes an hourly rate, "
    "give the hourly number. Never convert, never estimate, never infer from "
    "the seniority or the location.\n"
    '  "salary_currency": ISO 4217 code (USD, EUR, GBP...), or null.\n'
    '  "employment_type": exactly one of full_time, part_time, contract, '
    "internship — or null if the posting does not say.\n"
    '  "required_years": the minimum years of experience the posting asks for, '
    'as a number, or null. "3+ years" is 3. "3-5 years" is 3. A seniority word '
    'with no number ("senior", "experienced") is null, not a guess.\n'
    '  "required_skills": technologies and skills the posting lists as '
    "required/must-have. Short names only (\"Python\", \"Kubernetes\"), at most "
    "25.\n"
    '  "nice_to_have_skills": the same, for preferred/bonus/nice-to-have.\n'
    '  "education_required": the minimum degree stated ("Bachelor\'s in '
    'Computer Science", "PhD"), or null. Null if a degree is only preferred.\n'
    '  "benefits_note": one short sentence naming concrete benefits the posting '
    "states (equity, visa sponsorship, 4-day week, remote stipend), or null.\n"
    '  "language": ISO 639-1 code of the language THE POSTING ITSELF is '
    'written in ("en", "de", "fr").\n\n'
    "Every field is null unless the posting states it. Do not infer, do not "
    "estimate, and do not fill a field from what is typical for such a role."
)


def _to_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        # Models return "120,000", "$120k" and "120000" interchangeably.
        text = str(value).strip().lower().replace(",", "").replace("$", "")
        multiplier = 1000.0 if text.endswith("k") else 1.0
        text = text.rstrip("k")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        number = float(match.group(0)) * multiplier
    if number <= 0:
        return None
    return number


def _to_skills(value) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = str(item).strip()[:60]
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out[:_MAX_SKILLS]


def _to_text(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def normalize(parsed: dict) -> dict:
    """
    The model's JSON, made safe to write to columns.

    Defensive on purpose: a free provider that returns "competitive" for
    salary_min must produce a null, not a crash and not a zero — a zero would
    read as "this job pays nothing" to every filter downstream.
    """
    details: dict = {}

    low = _to_number(parsed.get("salary_min"))
    high = _to_number(parsed.get("salary_max"))
    # A single figure arriving as the max reads as "up to X" to the filter,
    # which is the opposite of what a "$150,000 salary" line means.
    if low is None and high is not None:
        low = high
    if low is not None and high is not None and high < low:
        low, high = high, low
    details["salary_min"] = low
    details["salary_max"] = high

    currency = _to_text(parsed.get("salary_currency"), 8)
    details["salary_currency"] = (
        currency.upper() if currency and low is not None else None
    )

    employment = _to_text(parsed.get("employment_type"), 32)
    employment = (employment or "").lower().replace("-", "_").replace(" ", "_")
    details["employment_type"] = employment if employment in EMPLOYMENT_TYPES else None

    years = _to_number(parsed.get("required_years"))
    # A posting asking for 45 years is a parse error, not a job.
    details["required_years"] = years if years is not None and years <= 40 else None

    details["required_skills"] = _to_skills(parsed.get("required_skills"))
    details["nice_to_have_skills"] = _to_skills(parsed.get("nice_to_have_skills"))
    details["education_required"] = _to_text(parsed.get("education_required"), 120)
    details["benefits_note"] = _to_text(parsed.get("benefits_note"), _MAX_BENEFITS_CHARS)

    language = _to_text(parsed.get("language"), 8)
    details["language"] = language.lower()[:5] if language else None
    return details


def extract(description: str, job_id=None) -> dict | None:
    """
    Ask the model for the posting's stated facts. None when it could not.

    None and "all fields null" are deliberately different: the first means the
    call failed and should be retried, the second means the posting genuinely
    says nothing and re-reading it would waste a call forever.
    """
    text = (description or "").strip()
    if len(text) < MIN_DESCRIPTION_CHARS:
        return None

    from app.llm.providers import generation_chat
    from app.services import llm_log
    from app.services.matcher import _extract_json_object

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": text[:MAX_DESCRIPTION_CHARS]},
    ]
    try:
        with llm_log.stage("job_details", job_id=job_id):
            reply = generation_chat(
                messages,
                api_key=settings.NVIDIA_NIM_API_KEY,
                base_url=settings.NVIDIA_NIM_BASE_URL,
                model=settings.NVIDIA_NIM_MODEL,
                temperature=0.0,
                max_tokens=1024,
            )
    except Exception as exc:
        logger.warning("job_details: extraction call failed: %s", exc)
        return None

    try:
        parsed = _extract_json_object(reply)
    except Exception as exc:
        logger.warning("job_details: unreadable reply: %s", exc)
        return None

    return normalize(parsed)


def needs_extraction(job) -> bool:
    """
    Whether this job's details are worth a call right now.

    Re-read only when the description has meaningfully grown since the last
    read — enrichment routinely replaces a 500-character stub with the real
    posting, and the facts in it were not there before.
    """
    if not (job.description or "").strip():
        return False
    if len(job.description) < MIN_DESCRIPTION_CHARS:
        return False
    if job.details_extracted_at is None:
        return True
    # Re-read when the description grew after the last read. The stamp is only
    # set for meaningful growth in the first place (200+ characters), so it
    # already means "this posting now says more than it did".
    stamped = job.description_updated_at
    return stamped is not None and stamped > job.details_extracted_at


def apply(job, details: dict) -> None:
    """Write extracted details onto the job, stamping when it was read."""
    for field, value in details.items():
        if hasattr(job, field):
            setattr(job, field, value)
    job.details_extracted_at = datetime.now(timezone.utc)


def extract_and_apply(job) -> bool:
    """One call, written to the job. True when details were read."""
    details = extract(job.description, job_id=getattr(job, "id", None))
    if details is None:
        return False
    apply(job, details)
    return True
