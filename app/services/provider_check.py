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

It runs on a worker, not in the request that asks for it. Real calls to real
providers take real seconds — more when a single-slot provider is mid-call and
this one has to queue — and the proxy in front of the app gives an upstream
sixty seconds before returning a 504. A check that reports "everything is fine"
by timing out the page is worse than no check at all.
"""

import copy
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.llm.providers import Provider, call_provider, configured_providers

logger = logging.getLogger(__name__)

RESULT_KEY = "provider_check"
ACTIVE_STATUSES = ("queued", "running")
# Past this, a record still claiming to run is a worker that died, not a slow
# provider — every probe has a timeout well inside it.
STALE_AFTER_SECONDS = 300

# Short enough to cost nothing, specific enough that a wrong answer is visible.
PROBE = [{"role": "user", "content": "Reply with the single word: ready"}]
PROBE_MAX_TOKENS = 512
# Tighter than a working call's 90s. This is a reachability check: a provider
# that needs more than half a minute to say one word is a finding, not a wait.
PROBE_TIMEOUT_SECONDS = 30
# And it does not queue politely. Whether a single-slot provider is busy right
# now is part of what the check is reporting, so a few seconds is enough to
# distinguish "briefly contended" from "something is holding this open".
PROBE_GATE_WAIT_SECONDS = 5


def _probe(provider: Provider) -> dict:
    from app.services import llm_log

    started = time.monotonic()
    base = {
        "name": provider.name,
        "model": provider.model,
        "gated": provider.max_concurrency == 1,
    }
    try:
        with llm_log.stage("provider_check"):
            reply = call_provider(
                provider, PROBE, temperature=0.0, max_tokens=PROBE_MAX_TOKENS,
                timeout=PROBE_TIMEOUT_SECONDS, gate_wait=PROBE_GATE_WAIT_SECONDS,
            )
        text = (reply or "").strip()
        return {
            **base,
            "ok": bool(text),
            "ms": int((time.monotonic() - started) * 1000),
            # A reasoning model can spend the whole budget thinking and return
            # nothing, which is a real result worth seeing rather than a crash.
            "detail": text[:120] or "connected, but replied with nothing",
        }
    except Exception as exc:
        return {
            **base,
            "ok": False,
            "ms": int((time.monotonic() - started) * 1000),
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }


def check_providers() -> list[dict]:
    """One call per configured provider, plus the NIM primary."""
    results = [_probe(provider) for provider in configured_providers().values()]
    if settings.NVIDIA_NIM_API_KEY:
        results.append(_probe(Provider(
            name="nim",
            api_key=settings.NVIDIA_NIM_API_KEY,
            model=settings.NVIDIA_NIM_MODEL,
            base_url=settings.NVIDIA_NIM_BASE_URL,
        )))
    return results


# ---------------------------------------------------------------------------
# The record the page reads
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(db) -> dict | None:
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    return (profile.data.get(RESULT_KEY) if profile else None) or None


def store_state(db, payload: dict) -> None:
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    if profile is None:
        logger.warning("provider_check: no profile to store the result on")
        return
    data = copy.deepcopy(profile.data)
    data[RESULT_KEY] = payload
    profile.data = data
    db.commit()


def mark_queued(db) -> None:
    """
    Record the request before the worker sees it.

    Without this the panel has nothing to show between the click and the worker
    picking the task up, so "queued" and "never asked for" look identical — and
    the button appears to do nothing at all.
    """
    store_state(db, {"status": "queued", "queued_at": _now(), "results": []})


def _age_seconds(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))


def _humanise(seconds: int | None) -> str:
    if seconds is None:
        return "a moment"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def progress(record: dict | None) -> dict:
    """
    What the panel should say, and whether it should keep polling.

    `stalled` is the case worth naming: a worker killed mid-check leaves a
    record claiming to run forever, and polling that silently is
    indistinguishable from a provider being slow.
    """
    if not record:
        return {"active": False, "stalled": False, "stage": None,
                "waiting": "", "age": ""}
    age = _age_seconds(
        record.get("finished_at") or record.get("started_at") or record.get("queued_at")
    )
    if record.get("status") not in ACTIVE_STATUSES:
        return {"active": False, "stalled": False, "stage": record.get("status"),
                "waiting": "", "age": _humanise(age)}
    stalled = age is not None and age > STALE_AFTER_SECONDS
    return {"active": not stalled, "stalled": stalled, "stage": record["status"],
            "waiting": _humanise(age), "age": _humanise(age)}
