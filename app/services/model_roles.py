"""
Which model does what, and where you change it.

The application makes five quite different kinds of LLM call, and until now
exactly one of them was selectable. `nvidia_nim_model` on the settings page
chose the scoring model; everything else picked its provider in code, in five
different ways, and you could not see which — let alone change it.

That produced the specific complaint this exists to answer: "Work it out" (the
crawl-recipe button) spent NIM calls, because that is what the code it grew out
of happened to pass, when the free provider was sitting right there configured
and unused.

So the calls get *named*. A role is a job the application needs a model for,
not a model and not a provider:

    match        scoring a job against the profile
    match_deep   the second, more careful pass over what survived
    generate     writing a CV or a covering letter
    extract      reading a description into structured fields
    learn        working out how to read or crawl a site

Each role resolves to a provider and a model, and each is selectable on the
settings page. "Auto" is the default and means "walk this role's preference
order over whatever is configured", which is what the code did implicitly
before — the difference is that it is now written down, visible, and
overridable per role rather than per deployment.

The preferences differ per role because the jobs differ. Writing a covering
letter is worth a good model and happens a few times a day. Scoring runs
thousands of times and wants the cheap fast one. Learning a recipe is a button
somebody pressed and is rare, so it should take the free provider and leave the
paid budget alone — which is exactly what it was not doing.
"""

import logging
from dataclasses import dataclass, replace

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    help: str
    # Provider names in the order this role would like them, best first. The
    # first one configured wins when the role is left on "auto".
    prefer: tuple[str, ...]


ROLES: tuple[Role, ...] = (
    Role(
        "match", "Scoring jobs",
        "Runs on every job that passes the keyword filter, so this is the one "
        "that decides how much scoring costs. Wants fast and cheap over clever.",
        prefer=("nim", "freeinference", "gemini", "anthropic"),
    ),
    Role(
        "match_deep", "Second-pass scoring",
        "Re-reads only the jobs that survived the first pass, with the full "
        "description. Few enough calls that a better model is affordable.",
        prefer=("nim", "anthropic", "gemini", "freeinference"),
    ),
    Role(
        "generate", "Writing documents",
        "Your CV and covering letters. A handful of calls a day, and the one "
        "place where the output is read by a person — worth the best model you "
        "have configured.",
        prefer=("anthropic", "gemini", "freeinference", "nim"),
    ),
    Role(
        "extract", "Reading a description",
        "Pulling salary, seniority and required skills out of a posting when "
        "the page does not state them in a machine-readable way.",
        prefer=("freeinference", "nim", "gemini", "anthropic"),
    ),
    Role(
        "learn", "Working out how a site works",
        "Writing a harvest or crawl recipe from stored samples. A button you "
        "press occasionally, so it should take the free provider rather than "
        "spend the budget the scoring passes need.",
        prefer=("freeinference", "nim", "gemini", "anthropic"),
    ),
)

ROLES_BY_KEY = {role.key: role for role in ROLES}

# The setting value meaning "use this role's preference order".
AUTO = "auto"

# NIM models worth offering. The same list the runs page compares against —
# kept here because this is now the module that answers "which models exist".
NIM_MODELS = (
    "z-ai/glm-5.2",
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
)


def tunable_key(role_key: str) -> str:
    return f"model_{role_key}"


def _providers() -> dict:
    """Every provider with credentials, NIM included."""
    from app.llm.providers import configured_providers, nim_provider

    providers = dict(configured_providers())
    if (settings.NVIDIA_NIM_API_KEY or "").strip():
        providers["nim"] = nim_provider()
    return providers


def available(role_key: str = "") -> list[tuple[str, str]]:
    """
    Every `(value, label)` a role may be set to, "auto" first.

    Only providers that are actually configured appear. Offering a model whose
    key is not set would produce a setting that saves cleanly and then fails on
    the first call, which is the worst moment to find out.
    """
    role = ROLES_BY_KEY.get(role_key)
    options: list[tuple[str, str]] = [
        (AUTO, "Auto — best configured for this job")
    ]

    providers = _providers()
    order = role.prefer if role else tuple(sorted(providers))
    for name in order:
        provider = providers.get(name)
        if provider is None:
            continue
        models = NIM_MODELS if name == "nim" else (provider.model,)
        for model in models:
            if not model:
                continue
            options.append((f"{name}:{model}", f"{name} · {model}"))
    return options


def choices(role_key: str = "") -> list[str]:
    """Just the values, for a `choice` tunable."""
    return [value for value, _ in available(role_key)]


def resolve(profile_data: dict | None, role_key: str):
    """
    The provider and model this role should use right now, or None.

    None means nothing is configured that can serve it, and callers treat that
    the way they already treat a provider outage — the feature is unavailable
    rather than broken.
    """
    role = ROLES_BY_KEY.get(role_key)
    if role is None:
        return None

    providers = _providers()
    if not providers:
        return None

    from app.services.tunables import value as tunable

    chosen = str(tunable(profile_data or {}, tunable_key(role_key)) or AUTO)
    if chosen != AUTO and ":" in chosen:
        name, _, model = chosen.partition(":")
        provider = providers.get(name)
        if provider is not None and model:
            return replace(provider, model=model)
        # Configured for a provider whose key has since been removed. Falling
        # through to auto beats failing: the setting is stale, the work is not.
        logger.info(
            "model_roles: %s is set to %r, which is not configured — using auto",
            role_key, chosen,
        )

    for name in role.prefer:
        provider = providers.get(name)
        if provider is not None:
            return provider
    return next(iter(providers.values()), None)


def call(profile_data: dict | None, role_key: str, messages: list[dict],
         temperature: float = 0.1, max_tokens: int = 800) -> str:
    """
    Make this role's call, falling back down its preference order.

    One place rather than five. Every caller previously assembled its own
    credentials, which is how `learn` ended up on the paid provider while the
    free one sat configured and idle.
    """
    import contextlib

    from app.llm.providers import call_provider
    from app.services import llm_log

    role = ROLES_BY_KEY.get(role_key)
    if role is None:
        raise RuntimeError(f"Unknown model role {role_key!r}.")

    # Label the log rows with the role, unless the caller already said
    # something more specific. Without this a role's calls are indistinguishable
    # in `llm_calls` — every one of them says "unknown" — so the first question
    # about a bad proposal ("what did the model actually reply?") could only be
    # answered by matching on the prompt text. An existing stage is left alone:
    # "score_job" tells you more than "match" does.
    labelled = (
        llm_log.stage(role_key)
        if llm_log.current_stage() in ("", "unknown")
        else contextlib.nullcontext()
    )

    first = resolve(profile_data, role_key)
    if first is None:
        raise RuntimeError("No LLM provider is configured.")

    providers = _providers()
    chain = [first] + [
        providers[name] for name in role.prefer
        if name in providers and providers[name].name != first.name
    ]

    last: Exception | None = None
    with labelled:
        for provider in chain:
            try:
                return call_provider(
                    provider, messages, temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                last = exc
                logger.warning("model_roles: %s on %s failed: %s",
                               role_key, provider.name, exc)
    raise last if last else RuntimeError("No LLM providers configured.")


def describe(profile_data: dict | None) -> list[dict]:
    """One row per role, for the settings page."""
    rows = []
    for role in ROLES:
        provider = resolve(profile_data, role.key)
        from app.services.tunables import value as tunable

        rows.append({
            "key": role.key,
            "tunable": tunable_key(role.key),
            "label": role.label,
            "help": role.help,
            "setting": str(tunable(profile_data or {}, tunable_key(role.key))
                           or AUTO),
            # What it would actually use, which is the question the page is
            # really being asked. "Auto" on its own tells you nothing.
            "provider": provider.name if provider else "",
            "model": provider.model if provider else "",
            "options": available(role.key),
        })
    return rows
