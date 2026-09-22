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
from app.services.model_catalog import DEFAULT_NIM_MODELS

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
    # Choices that depend on runtime state rather than on this file. A model
    # role's options come from which providers have keys, so the list built at
    # import is a snapshot — validating against it would silently discard a
    # pinned model whenever that snapshot and the present disagree, which is
    # the one moment the setting most needs to survive.
    dynamic: bool = False
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
        # The NIM list on this page, editable under "Model lists" — see
        # `model_catalog`. Dynamic so a model added there is accepted here.
        choices=list(DEFAULT_NIM_MODELS),
        dynamic=True,
        label="Matching model",
        help="Which NIM model scores your jobs. Add newly released models under "
             "\"Model lists\" below; compare candidates on the runs page first — "
             "the count that matters there is unreadable replies.",
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
        key="dashboard_max_age_days", env="DASHBOARD_MAX_AGE_DAYS", kind="int",
        minimum=0, maximum=3650, group="Filtering",
        label="Hide jobs older than (days)",
        help="How far back the jobs list looks, counted from when the job was "
             "fetched — not from the posting date, which most sources don't "
             "report. Nothing is deleted, and anything you applied to or "
             "starred stays visible however old it is. The list says how many "
             "it's hiding and links to show them. 0 shows everything.",
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
        key="filter_by_language", env="FILTER_BY_LANGUAGE", kind="bool",
        group="Filtering",
        label="Skip postings not written in English",
        help="Arbeitnow and friends return German listings under English "
             "titles, so the title gate passes them and a model is then asked "
             "to score a description you could not act on. A posting whose "
             "language could not be read is always kept — set MATCH_LANGUAGES "
             "to accept more than English.",
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
        key="jsearch_date_posted", env="JSEARCH_DATE_POSTED", kind="choice",
        choices=["today", "3days", "week", "month", "all"], group="Sources",
        label="JSearch: posted within",
        help="How far back each JSearch search reaches. \"today\" misses "
             "anything from a day the fetch did not run; wider windows return "
             "more repeats, which dedupe merges.",
    ),
    Tunable(
        key="jsearch_num_pages", env="JSEARCH_NUM_PAGES", kind="int",
        minimum=1, maximum=5, group="Sources",
        label="JSearch: pages per search",
        help="Each page is one call against a small monthly quota, for every "
             "role and location you search — so 2 doubles the spend.",
    ),
    # The three scheduled fetch groups. Beat ticks every few minutes and each
    # group checks its interval against its own last run, so a change here
    # takes effect on the next tick — no restart. The single "fetch interval"
    # this replaced was read by nothing: the schedule had moved to these three
    # environment variables and the setting on this page stayed behind.
    Tunable(
        key="fetch_api_interval_hours", env="FETCH_API_INTERVAL_HOURS", kind="int",
        minimum=1, maximum=168, group="Schedule",
        label="API sources: every (hours)",
        help="Keyed APIs and public feeds (Adzuna, LinkedIn guest, RemoteOK…). "
             "Minutes per run. Lower catches postings sooner and spends more of "
             "each provider's quota.",
    ),
    Tunable(
        key="fetch_boards_interval_hours", env="FETCH_BOARDS_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Schedule",
        label="Company boards: every (hours)",
        help="Greenhouse, Lever, Workday and the rest of the board registry. "
             "A run can take hours; one that is still going when the next is "
             "due is skipped rather than doubled.",
    ),
    Tunable(
        key="fetch_browser_interval_hours", env="FETCH_BROWSER_INTERVAL_HOURS",
        kind="int", minimum=1, maximum=168, group="Schedule",
        label="Browser tier: every (hours)",
        help="The Playwright scrapers — the most expensive tier and the least "
             "productive per minute, so the least often.",
    ),
]

def _model_role_tunables() -> list[Tunable]:
    """
    One selector per LLM role, generated from `model_roles`.

    Generated rather than written out, because the list of roles belongs to
    that module and two copies would drift — the symptom being a role the code
    uses that the settings page cannot show, which is the state this replaced.
    """
    from app.services.model_roles import ROLES, choices, tunable_key

    return [
        Tunable(
            key=tunable_key(role.key),
            # No environment variable behind these. The default is "auto",
            # which is a decision about how to decide rather than a value, and
            # it lives in `model_roles.resolve` where the deciding happens.
            env="",
            kind="choice",
            choices=choices(role.key),
            dynamic=True,
            label=role.label,
            help=role.help,
            group="Models",
        )
        for role in ROLES
    ]


TUNABLES.extend(_model_role_tunables())

BY_KEY: dict[str, Tunable] = {t.key: t for t in TUNABLES}


def choices_for(tunable: Tunable, profile_data: dict | None) -> list[str]:
    """
    The options to render for a choice tunable, built now rather than at import.

    Model lists are edited on the settings page and provider keys come and go,
    so a list captured at process start would hide exactly the model somebody
    just added.
    """
    if not tunable.dynamic:
        return tunable.choices
    from app.services import model_catalog, model_roles

    if tunable.key == "nvidia_nim_model":
        return model_catalog.models(profile_data, "nim")
    return model_roles.choices(tunable.key.removeprefix("model_"), profile_data)

GROUPS: list[str] = list(dict.fromkeys(t.group for t in TUNABLES))


def default(tunable: Tunable):
    """The environment value this falls back to."""
    if not tunable.env:
        # A model role. Its default is the first choice, which is "auto" —
        # there is no environment variable holding it because the answer is
        # computed rather than configured.
        return tunable.choices[0] if tunable.choices else None
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
            if tunable.dynamic:
                # Anything shaped like a model id. `model_roles.resolve` already
                # falls back when a setting names a provider that is no longer
                # configured, and losing the setting outright is worse than
                # carrying one that is briefly stale. Membership in the current
                # list is checked when the form is saved (see `parse_form`),
                # where the profile's model lists are to hand.
                from app.services.model_catalog import is_model_id

                return text if text and is_model_id(text) else None
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


def parse_form(form: dict, profile_data: dict | None = None) -> dict:
    """
    Coerce a submitted form into the stored override dict.

    Unchecked checkboxes don't appear in a form body at all, so booleans are
    read as absent-means-false rather than skipped like the other kinds.

    A model is only accepted if it is on the list the page offered — the model
    lists edited under "Model lists" — because the id goes straight to the
    provider. Pass the profile so that list can be read.
    """
    parsed = {}
    for tunable in TUNABLES:
        if tunable.kind == "bool":
            parsed[tunable.key] = coerce(tunable, form.get(tunable.key, False))
            continue
        if tunable.key not in form:
            continue
        coerced = coerce(tunable, form[tunable.key])
        if coerced is None:
            continue
        if tunable.dynamic and coerced not in choices_for(tunable, profile_data):
            logger.warning("tunables: %s=%r is not on the offered list; ignored",
                           tunable.key, coerced)
            continue
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
        if not tunable.env:
            # A model role. There is no `settings.X` behind it to overlay —
            # it is read through `model_roles`, not through `cfg`. Including it
            # here wrote an override keyed on the empty string, which built an
            # overlay for a profile that had overridden nothing.
            continue
        resolved = value(profile_data, tunable.key)
        if resolved is not None and resolved != getattr(base, tunable.env, None):
            overrides[tunable.env] = resolved
    return _Overlay(base, overrides) if overrides else base
