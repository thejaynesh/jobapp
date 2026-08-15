"""
Ask every configured LLM provider one trivial question, and report what happened.

Failover is invisible when it works and invisible when it doesn't. A provider
whose key is wrong, whose model id was renamed, or whose daily credit is spent
looks exactly like a provider that was simply never needed — the chain quietly
steps over it, matching carries on, and nothing anywhere says a word. That is
fine until the day the chain is all you have left.

So: one real call each, smallest possible prompt, sequential. Sequential
matters — a provider that only accepts one request at a time is exercised
through its gate here the same way it will be in production, which makes this
check evidence about the gate as well as about the provider.
"""

import logging
import time

from app.config import settings
from app.llm.providers import Provider, call_provider, configured_providers

logger = logging.getLogger(__name__)

# Short enough to cost nothing, specific enough that a wrong answer is visible.
PROBE = [{"role": "user", "content": "Reply with the single word: ready"}]
PROBE_MAX_TOKENS = 512


def _probe(provider: Provider) -> dict:
    started = time.monotonic()
    try:
        reply = call_provider(provider, PROBE, temperature=0.0,
                              max_tokens=PROBE_MAX_TOKENS)
        elapsed = int((time.monotonic() - started) * 1000)
        text = (reply or "").strip()
        return {
            "name": provider.name,
            "model": provider.model,
            "ok": bool(text),
            "ms": elapsed,
            # A reasoning model can spend the whole budget thinking and return
            # nothing, which is a real result worth seeing rather than a crash.
            "detail": text[:120] or "connected, but replied with nothing",
            "gated": provider.max_concurrency == 1,
        }
    except Exception as exc:
        return {
            "name": provider.name,
            "model": provider.model,
            "ok": False,
            "ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {exc}"[:200],
            "gated": provider.max_concurrency == 1,
        }


def check_providers() -> list[dict]:
    """One call per configured provider, plus the NIM primary."""
    results = []
    for provider in configured_providers().values():
        results.append(_probe(provider))

    if settings.NVIDIA_NIM_API_KEY:
        results.append(_probe(Provider(
            name="nim",
            api_key=settings.NVIDIA_NIM_API_KEY,
            model=settings.NVIDIA_NIM_MODEL,
            base_url=settings.NVIDIA_NIM_BASE_URL,
        )))
    return results
