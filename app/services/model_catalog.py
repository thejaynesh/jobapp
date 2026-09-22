"""
Which models each provider offers, as a list you edit on the settings page.

NVIDIA NIM and FreeInference add models every few weeks, and until this existed
the list the dropdowns offered was a tuple in the source — three copies of it,
in `model_roles`, `tunables` and the runs page. Trying a model released last
week meant a code change and a deploy, for what is a preference.

So the list is data, stored on the profile under `STORE_KEY`:

    {"nim": ["z-ai/glm-5.2", ...], "freeinference": ["glm-5.1", ...]}

A provider with nothing saved falls back to its built-in defaults, so a fresh
deployment has sensible choices without anyone visiting the page. The model the
environment names is always included, because it is the one in use until
somebody picks another and a dropdown that cannot show the current value lies
about it.

`discover()` asks the provider which models it serves (the OpenAI-compatible
`GET /models`), so keeping up is a button rather than a search through release
notes. It only ever *offers*; nothing is added until it is saved.
"""

import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

STORE_KEY = "model_catalog"

# The providers whose models can be listed, in the order the page shows them.
PROVIDERS: tuple[tuple[str, str], ...] = (
    ("nim", "NVIDIA NIM"),
    ("freeinference", "FreeInference"),
    ("gemini", "Gemini"),
    ("anthropic", "Anthropic"),
)
PROVIDER_LABELS = dict(PROVIDERS)

# Shipped defaults for NIM, which offers far more than anyone wants in a
# dropdown. These are the ones compared on the runs page before this list
# became editable.
DEFAULT_NIM_MODELS = (
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

# A model id as providers write them: "meta/llama-3.3-70b-instruct",
# "gemini-2.5-flash", "claude-opus-4-8", "org/model:tag". Anything else is a
# typo or a pasted sentence, and would render into a <select> as garbage.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")

# More than this and the dropdowns stop being usable; NIM's full catalogue is
# well over a hundred models, which is why discovery offers rather than adds.
MAX_MODELS_PER_PROVIDER = 60


def is_model_id(text: str) -> bool:
    """Whether this is shaped like a model id (or a "provider:model" pair)."""
    return bool(_MODEL_ID.match(text or ""))


def _env_models(provider: str) -> list[str]:
    """The model(s) the environment configures for this provider."""
    names = {
        "nim": ("NVIDIA_NIM_MODEL",),
        "freeinference": ("FREEINFERENCE_MODEL", "FREEINFERENCE_MATCH_MODEL"),
        "gemini": ("GEMINI_MODEL",),
        "anthropic": ("ANTHROPIC_MODEL", "ANTHROPIC_MATCH_MODEL"),
    }.get(provider, ())
    return [str(getattr(settings, name, "") or "").strip() for name in names]


def defaults(provider: str) -> list[str]:
    """What a provider offers when nobody has edited its list."""
    base = list(DEFAULT_NIM_MODELS) if provider == "nim" else []
    return _dedupe(_env_models(provider) + base)


def _dedupe(models) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        text = (model or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def parse(text: str) -> tuple[list[str], list[str]]:
    """
    A pasted list as (valid ids, rejected entries).

    One per line, or comma-separated, because both are how model lists get
    copied out of documentation. Rejects are returned rather than dropped
    silently, so the page can say which line it ignored.
    """
    entries = re.split(r"[\n,]+", text or "")
    valid: list[str] = []
    rejected: list[str] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        (valid if _MODEL_ID.match(entry) else rejected).append(entry)
    return _dedupe(valid)[:MAX_MODELS_PER_PROVIDER], rejected


def saved(profile_data: dict | None, provider: str) -> list[str] | None:
    """The user's list for this provider, or None if they have not set one."""
    stored = ((profile_data or {}).get(STORE_KEY) or {}).get(provider)
    if not isinstance(stored, list):
        return None
    return [m for m in stored if isinstance(m, str) and _MODEL_ID.match(m)]


def models(profile_data: dict | None, provider: str) -> list[str]:
    """
    Every model to offer for this provider, current environment model first.

    The saved list replaces the defaults rather than adding to them: removing a
    retired model from the list has to actually remove it.
    """
    chosen = saved(profile_data, provider)
    base = chosen if chosen is not None else defaults(provider)
    return _dedupe(_env_models(provider) + base)


def store(profile_data: dict | None, provider: str, model_ids: list[str]) -> dict:
    """The profile data with this provider's list replaced. Does not mutate."""
    import copy

    if provider not in PROVIDER_LABELS:
        raise ValueError(f"Unknown provider {provider!r}")
    updated = copy.deepcopy(profile_data or {})
    catalog = dict(updated.get(STORE_KEY) or {})
    catalog[provider] = _dedupe(model_ids)[:MAX_MODELS_PER_PROVIDER]
    updated[STORE_KEY] = catalog
    return updated


def reset(profile_data: dict | None, provider: str) -> dict:
    """The profile data with this provider back on its defaults."""
    import copy

    updated = copy.deepcopy(profile_data or {})
    catalog = dict(updated.get(STORE_KEY) or {})
    catalog.pop(provider, None)
    updated[STORE_KEY] = catalog
    return updated


def _endpoint(provider: str) -> tuple[str, str] | None:
    """(base_url, api_key) for an OpenAI-compatible model listing, if configured."""
    if provider == "nim":
        return settings.NVIDIA_NIM_BASE_URL, settings.NVIDIA_NIM_API_KEY
    if provider == "freeinference":
        return settings.FREEINFERENCE_BASE_URL, settings.FREEINFERENCE_API_KEY
    if provider == "gemini":
        return settings.GEMINI_BASE_URL, settings.GEMINI_API_KEY
    if provider == "anthropic":
        return "https://api.anthropic.com/v1", settings.ANTHROPIC_API_KEY
    return None


def discover(provider: str, timeout: float = 15.0) -> list[str]:
    """
    The model ids the provider says it serves right now.

    Raises with a readable message when the provider is not configured or does
    not answer, because the page shows that message to the person who pressed
    the button.
    """
    import httpx

    endpoint = _endpoint(provider)
    if endpoint is None:
        raise ValueError(f"Unknown provider {provider!r}")
    base_url, api_key = endpoint
    if not (api_key or "").strip():
        raise ValueError(f"{PROVIDER_LABELS[provider]} has no API key configured.")
    if not (base_url or "").strip():
        raise ValueError(f"{PROVIDER_LABELS[provider]} has no base URL configured.")

    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout,
                         params={"limit": 1000} if provider == "anthropic" else None)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        raise ValueError(
            f"Could not list {PROVIDER_LABELS[provider]} models: {exc}"
        ) from exc

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{PROVIDER_LABELS[provider]} returned no model list.")
    found = []
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else row
        if isinstance(model_id, str):
            # Gemini's OpenAI-compatible listing prefixes ids with "models/",
            # which its chat endpoint does not accept back.
            model_id = model_id.removeprefix("models/")
            if _MODEL_ID.match(model_id):
                found.append(model_id)
    return sorted(_dedupe(found))
