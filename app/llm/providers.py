"""
Multi-provider LLM routing.

Four providers are supported:
  - "freeinference" — OpenAI-compatible, free daily credit for researchers
  - "anthropic"     — Claude via the official Anthropic SDK (quality generation)
  - "gemini"        — Gemini via Google's OpenAI-compatible endpoint
  - "nim"           — NVIDIA NIM (OpenAI-compatible), the existing default

Document generation prefers quality-first (anthropic -> gemini -> primary),
while high-volume job matching uses the extra providers only as failover.
Providers without an API key configured are simply skipped.

FreeInference goes ahead of the paid providers in both chains. It costs nothing
until its daily credit runs out, and the chain already falls through on any
failure — including that one — so trying free before paid cannot cost anything
except the seconds spent finding out.
"""

import contextlib
import contextvars
import logging
from dataclasses import dataclass, replace

from app.config import settings

logger = logging.getLogger(__name__)

GENERATION_PREFERENCE = ["freeinference", "anthropic", "gemini"]
MATCHING_PREFERENCE = ["freeinference", "gemini", "anthropic"]

# Records which provider/model served each successful LLM call, so callers
# (e.g. document generation) can persist "who wrote this". Only active between
# start_llm_log() and collect_llm_log(); calls outside a log window are not
# tracked. Context-local so concurrent tasks don't mix logs.
_llm_call_log: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "llm_call_log", default=None
)


def provider_label(provider: "Provider") -> str:
    return f"{provider.name}/{provider.model}"


def start_llm_log() -> None:
    _llm_call_log.set([])


def collect_llm_log() -> list[str]:
    """Unique provider/model labels used since start_llm_log(), in first-use order."""
    log = _llm_call_log.get() or []
    _llm_call_log.set(None)
    seen: list[str] = []
    for label in log:
        if label not in seen:
            seen.append(label)
    return seen


def _record_llm_use(provider: "Provider") -> None:
    log = _llm_call_log.get()
    if log is not None:
        log.append(provider_label(provider))


@dataclass(frozen=True)
class Provider:
    name: str
    api_key: str
    model: str
    base_url: str = ""  # empty for the Anthropic SDK
    # Requests this endpoint will accept at once; 0 means it does not care.
    # Above zero, calls queue through a Redis gate — this app runs two worker
    # processes and overlaps matching with generation by design, so a
    # single-slot provider would otherwise refuse every second caller.
    max_concurrency: int = 0
    # Whether a call here can appear on a bill. Only paid calls count against
    # MAX_PAID_MATCH_CALLS_PER_CYCLE; a provider with a fixed free daily
    # allowance cannot produce a surprise, so capping it just wastes credit.
    paid: bool = True


def configured_providers() -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if settings.FREEINFERENCE_API_KEY:
        providers["freeinference"] = Provider(
            name="freeinference",
            api_key=settings.FREEINFERENCE_API_KEY,
            model=settings.FREEINFERENCE_MODEL,
            base_url=settings.FREEINFERENCE_BASE_URL,
            max_concurrency=settings.FREEINFERENCE_MAX_CONCURRENCY,
            paid=False,
        )
    if settings.ANTHROPIC_API_KEY:
        providers["anthropic"] = Provider(
            name="anthropic",
            api_key=settings.ANTHROPIC_API_KEY,
            model=settings.ANTHROPIC_MODEL,
        )
    if settings.GEMINI_API_KEY:
        providers["gemini"] = Provider(
            name="gemini",
            api_key=settings.GEMINI_API_KEY,
            model=settings.GEMINI_MODEL,
            base_url=settings.GEMINI_BASE_URL,
        )
    return providers


# A working call gets a generous ceiling; a reachability probe does not want to
# wait that long to learn a provider is unreachable.
DEFAULT_TIMEOUT_SECONDS = 90


def _call_anthropic(provider: Provider, messages: list[dict], max_tokens: int,
                    timeout: float = DEFAULT_TIMEOUT_SECONDS,
                    entry=None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=provider.api_key)
    system = "\n\n".join(
        m["content"] for m in messages if m.get("role") == "system"
    )
    chat_messages = [m for m in messages if m.get("role") != "system"]

    kwargs: dict = {
        "model": provider.model,
        "max_tokens": max_tokens,
        "messages": chat_messages,
        "timeout": float(timeout),
    }
    if system:
        kwargs["system"] = system
    # Note: no temperature/top_p — current Claude models reject sampling params.
    response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("Anthropic refused the request")
    text = "".join(b.text for b in response.content if b.type == "text")
    if entry is not None:
        usage = getattr(response, "usage", None)
        entry.finish(text, finish_reason=getattr(response, "stop_reason", None))
        if usage is not None:
            entry.prompt_tokens = getattr(usage, "input_tokens", None)
            entry.completion_tokens = getattr(usage, "output_tokens", None)
    return text


def _call_openai_compatible(
    provider: Provider, messages: list[dict], temperature: float, max_tokens: int,
    timeout: float = DEFAULT_TIMEOUT_SECONDS, entry=None,
) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=provider.api_key, base_url=provider.base_url)
    response = client.chat.completions.create(
        model=provider.model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    message = response.choices[0].message
    text = getattr(message, "content", None) or ""
    if entry is not None:
        # Reasoning models put their working in a separate field and leave
        # `content` empty when the ceiling runs out mid-thought. Storing both
        # separates "the model is bad at this" from "the budget was too small".
        entry.finish(text, reasoning=getattr(message, "reasoning_content", None),
                     raw=response)
    return text


def call_provider(
    provider: Provider,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 512,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    gate_wait: float | None = None,
) -> str:
    from app.services import llm_log

    with _concurrency_gate(provider, gate_wait):
        with llm_log.call(provider.name, provider.model, messages,
                          temperature=temperature, max_tokens=max_tokens) as entry:
            if provider.name == "anthropic":
                result = _call_anthropic(provider, messages, max_tokens, timeout, entry)
            else:
                result = _call_openai_compatible(
                    provider, messages, temperature, max_tokens, timeout, entry
                )
    _record_llm_use(provider)
    return result


@contextlib.contextmanager
def _concurrency_gate(provider: Provider, wait: float | None = None):
    """
    Queue behind other callers when the endpoint only takes one at a time.

    A gate that will not open in time raises, and the caller's failover chain
    treats that like any other provider failure — which is the right reading:
    a provider busy for two minutes is one that cannot serve this call.
    """
    if provider.max_concurrency != 1:
        yield
        return
    from app.services import llm_gate

    with llm_gate.hold(provider.name, wait=wait):
        yield


def matching_fallbacks() -> list[Provider]:
    """
    Providers to try (in order) when the primary matching provider fails.
    Matching is high-volume JSON scoring, so the Anthropic entry uses the cheap
    match model (Haiku by default) rather than the generation model.
    """
    providers = configured_providers()
    chain = []
    for name in MATCHING_PREFERENCE:
        if name not in providers:
            continue
        provider = providers[name]
        match_model = _MATCH_MODELS.get(name)
        if match_model:
            # dataclasses.replace, so a provider's concurrency limit and paid
            # flag survive the model swap — losing max_concurrency here would
            # silently ungate every matching call, which is most of them.
            provider = replace(provider, model=match_model(settings) or provider.model)
        chain.append(provider)
    return chain


# Matching is high-volume JSON scoring, so providers that offer a cheaper or
# faster sibling model use it here instead of their generation model.
_MATCH_MODELS = {
    "anthropic": lambda s: getattr(s, "ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5"),
    "freeinference": lambda s: getattr(s, "FREEINFERENCE_MATCH_MODEL", ""),
}


def generation_chat(
    messages: list[dict],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.1,
    max_tokens: int = 512,
) -> str:
    """
    Chat completion for document generation: try quality providers first
    (Anthropic, then Gemini), then fall back to the passed-in primary
    (NVIDIA NIM) credentials. Signature matches the old single-provider
    chat_completion so call sites and tests are unchanged.
    """
    providers = configured_providers()
    chain: list[Provider] = [
        providers[name] for name in GENERATION_PREFERENCE if name in providers
    ]
    chain.append(Provider(name="primary", api_key=api_key, model=model, base_url=base_url))

    last_exc: Exception | None = None
    for provider in chain:
        try:
            result = call_provider(
                provider, messages, temperature=temperature, max_tokens=max_tokens
            )
            if provider.name != "primary":
                logger.info("generation_chat served by %s (%s)", provider.name, provider.model)
            return result
        except Exception as exc:
            last_exc = exc
            logger.warning("generation_chat: provider %s failed: %s", provider.name, exc)
    raise last_exc if last_exc else RuntimeError("no LLM providers configured")
