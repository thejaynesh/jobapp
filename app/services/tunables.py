"""
Settings you'd change while looking at your results, editable from the UI.

The settings page used to write `profile.data["settings"]` and nothing read it,
so all three of its fields were theatre — the match score you actually changed
was the one on the *skills* tab, under a different key, and the other two were
env-only. This is the fix, and the shape is chosen so it can't happen again:
every tunable is declared once here, the UI renders that declaration, and every
consumer reads through `value()` or the `effective_settings()` overlay. A field
that isn't wired up can't exist, because the wiring is the declaration.

Not everything belongs here. API keys, session cookies and connection URLs stay
in the environment — the test is whether you'd change it to see a different set
of jobs, not whether it happens to be configurable.
"""

import logging
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

# Where the overrides live on the profile. Kept as the key the settings page
# already wrote, so values saved before any of this worked start taking effect.
STORE_KEY = "settings"


@dataclass(frozen=True)
class Tunable:
    key: str                        # form field and storage key
    env: str                        # the matching app.config attribute
    kind: str                       # int | float | bool | choice
    label: str
    help: str
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] = field(default_factory=list)
    # Read once at process start, so a change needs a restart to bite. Said out
    # loud in the UI rather than left for the user to discover.
    restart_required: bool = False
    # An older key at the top level of the profile that's been the live value.
    # It wins on read until the next save writes both.
    legacy_key: str | None = None
    group: str = "Matching"


TUNABLES: list[Tunable] = [
    Tunable(
        key="min_match_score", env="MIN_MATCH_SCORE", kind="int",
        minimum=0, maximum=100, legacy_key="min_match_score",
        label="Minimum match score",
        help="Jobs the model scores below this are filtered out. Also editable "
             "on the profile's skills tab — the two stay in sync.",
    ),
    Tunable(
        key="min_keyword_skills", env="MIN_KEYWORD_SKILLS", kind="int",
        minimum=0, maximum=20,
        label="Minimum skill matches",
        help="How many of your skills must appear in a description before it's "
             "worth an LLM call. Raise it to spend fewer calls, lower it if "
             "good jobs are being filtered as \"too few skills\".",
    ),
    Tunable(
        key="nvidia_nim_model", env="NVIDIA_NIM_MODEL", kind="choice",
        choices=[
            "deepseek-ai/deepseek-v4-flash",
            "meta/llama-3.3-70b-instruct",
            "meta/llama-3.1-70b-instruct",
            "qwen/qwen3-next-80b-a3b-instruct",
            "mistralai/mistral-medium-3.5-128b",
            "google/gemma-4-31b-it",
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "meta/llama-3.1-8b-instruct",
            "openai/gpt-oss-120b",
            "nvidia/nemotron-3-super-120b-a12b",
        ],
        label="Matching model",
        help="Which NIM model scores your jobs. Compare candidates on the runs "
             "page first — the count that matters there is unreadable replies.",
    ),
    Tunable(
        key="max_job_age_days", env="MAX_JOB_AGE_DAYS", kind="int",
        minimum=0, maximum=365, group="Filtering",
        label="Maximum job age (days)",
        help="Postings older than this are dropped at fetch time. 0 disables "
             "the check. Only applies to jobs whose source reports a posting "
             "date, and only to new fetches — it won't clear what's stored.",
    ),
    Tunable(
        key="filter_senior_titles", env="FILTER_SENIOR_TITLES", kind="bool",
        group="Filtering",
        label="Skip senior-titled jobs while junior",
        help="Drops Senior/Staff/Principal/Lead titles before they cost an LLM "
             "call, unless the word appears in one of your target roles. Only "
             "active below the junior threshold below.",
    ),
    Tunable(
        key="junior_max_years", env="JUNIOR_MAX_YEARS", kind="float",
        minimum=0, maximum=30, group="Filtering",
        label="Junior threshold (years)",
        help="Below this much total experience you count as junior. Your total "
             "is worked out from the dates on your experience entries — see the "
             "profile's AI prompt tab for what it came to.",
    ),
    Tunable(
        key="linkedin_recency_hours", env="LINKEDIN_RECENCY_HOURS", kind="int",
        minimum=0, maximum=2160, group="LinkedIn",
        label="LinkedIn recency window (hours)",
        help="Asks LinkedIn for postings newer than this. 0 disables it. It's a "
             "hint to their ranker rather than a guarantee, so the age filter "
             "above is what actually enforces freshness.",
    ),
    Tunable(
        key="linkedin_max_pages", env="LINKEDIN_MAX_PAGES", kind="int",
        minimum=1, maximum=20, group="LinkedIn",
        label="LinkedIn pages per search",
        help="10 results a page. Deeper pages return looser matches and more "
             "undated postings, so more isn't always better.",
    ),
    Tunable(
        key="fetch_interval_hours", env="FETCH_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Schedule", restart_required=True,
        label="Fetch interval (hours)",
        help="How often the scheduled cycle runs.",
    ),
]

BY_KEY: dict[str, Tunable] = {t.key: t for t in TUNABLES}

GROUPS: list[str] = list(dict.fromkeys(t.group for t in TUNABLES))


def default(tunable: Tunable):
    """The environment value this falls back to."""
    return getattr(settings, tunable.env, None)


def coerce(tunable: Tunable, raw):
    """
    A form value as the right type, clamped to range. None if unusable.

    Clamping rather than rejecting: a typo'd 5000 in a page-count box should
    become the maximum, not silently keep the old value with no explanation.
    """
    if raw is None:
        return None
    try:
        if tunable.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in {"1", "true", "on", "yes"}
        if tunable.kind == "choice":
            text = str(raw).strip()
            return text if text in tunable.choices else None
        number = float(raw)
    except (TypeError, ValueError):
        return None

    if tunable.minimum is not None:
        number = max(number, tunable.minimum)
    if tunable.maximum is not None:
        number = min(number, tunable.maximum)
    return int(number) if tunable.kind == "int" else round(number, 2)


def value(profile_data: dict | None, key: str):
    """The effective value: profile override if set, else the env default."""
    tunable = BY_KEY[key]
    data = profile_data or {}

    # The legacy top-level key wins while it's the one that's been live. Both
    # are written on every save, so this stops mattering after the first one.
    if tunable.legacy_key and tunable.legacy_key in data:
        coerced = coerce(tunable, data[tunable.legacy_key])
        if coerced is not None:
            return coerced

    stored = (data.get(STORE_KEY) or {}).get(key)
    if stored is not None:
        coerced = coerce(tunable, stored)
        if coerced is not None:
            return coerced
    return default(tunable)


def values(profile_data: dict | None) -> dict:
    """Every tunable resolved, for rendering the form."""
    return {t.key: value(profile_data, t.key) for t in TUNABLES}


def is_overridden(profile_data: dict | None, key: str) -> bool:
    """Whether this differs from the environment default, for the UI to mark."""
    return value(profile_data, key) != default(BY_KEY[key])


def parse_form(form: dict) -> dict:
    """
    Coerce a submitted form into the stored override dict.

    Unchecked checkboxes don't appear in a form body at all, so booleans are
    read as absent-means-false rather than skipped like the other kinds.
    """
    parsed = {}
    for tunable in TUNABLES:
        if tunable.kind == "bool":
            parsed[tunable.key] = coerce(tunable, form.get(tunable.key, False))
            continue
        if tunable.key not in form:
            continue
        coerced = coerce(tunable, form[tunable.key])
        if coerced is not None:
            parsed[tunable.key] = coerced
    return parsed


def apply_to_profile(profile_data: dict, parsed: dict) -> dict:
    """
    The profile data with these overrides stored. Does not mutate the input.

    Legacy top-level keys are written alongside, so the two places a value can
    live can't drift apart again.
    """
    import copy

    updated = copy.deepcopy(profile_data or {})
    updated[STORE_KEY] = {**(updated.get(STORE_KEY) or {}), **parsed}
    for tunable in TUNABLES:
        if tunable.legacy_key and tunable.key in parsed:
            updated[tunable.legacy_key] = parsed[tunable.key]
    return updated


class _Overlay:
    """
    `settings` with the profile's overrides on top.

    Adapters take a `cfg` object and read attributes off it, so handing them
    this instead of `settings` wires every one of them up without touching a
    single call site — and without them needing to know overrides exist.
    """

    def __init__(self, base, overrides: dict):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def effective_settings(profile_data: dict | None, base=None):
    """`settings` with UI overrides applied, for anything that reads `cfg.X`."""
    base = base if base is not None else settings
    overrides = {}
    for tunable in TUNABLES:
        resolved = value(profile_data, tunable.key)
        if resolved is not None and resolved != getattr(base, tunable.env, None):
            overrides[tunable.env] = resolved
    return _Overlay(base, overrides) if overrides else base
