"""
Multi-provider LLM routing.

Three providers are supported:
  - "anthropic" — Claude via the official Anthropic SDK (quality generation)
  - "gemini"    — Gemini via Google's OpenAI-compatible endpoint
  - "nim"       — NVIDIA NIM (OpenAI-compatible), the existing default

Document generation prefers quality-first (anthropic -> gemini -> primary),
while high-volume job matching uses the extra providers only as failover.
Providers without an API key configured are simply skipped.
"""

import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

GENERATION_PREFERENCE = ["anthropic", "gemini"]
MATCHING_PREFERENCE = ["gemini", "anthropic"]


@dataclass(frozen=True)
class Provider:
    name: str
    api_key: str
    model: str
    base_url: str = ""  # empty for the Anthropic SDK


def configured_providers() -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
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


def _call_anthropic(provider: Provider, messages: list[dict], max_tokens: int) -> str:
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
        "timeout": 90.0,
    }
    if system:
        kwargs["system"] = system
    # Note: no temperature/top_p — current Claude models reject sampling params.
    response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("Anthropic refused the request")
    return "".join(b.text for b in response.content if b.type == "text")


def _call_openai_compatible(
    provider: Provider, messages: list[dict], temperature: float, max_tokens: int
) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=provider.api_key, base_url=provider.base_url)
    response = client.chat.completions.create(
        model=provider.model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=90,
    )
    return response.choices[0].message.content or ""


def call_provider(
    provider: Provider,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 512,
) -> str:
    if provider.name == "anthropic":
        return _call_anthropic(provider, messages, max_tokens)
    return _call_openai_compatible(provider, messages, temperature, max_tokens)


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
        if name == "anthropic":
            provider = Provider(
                name="anthropic",
                api_key=provider.api_key,
                model=getattr(settings, "ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5")
                or provider.model,
            )
        chain.append(provider)
    return chain


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
