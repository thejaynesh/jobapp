"""
Every LLM request and its reply, stored where they can be read side by side.

The existing log lines say a call happened and how it ended, which is the one
thing the result already tells you. What was missing is the pair. "The resume
came out empty" is either a prompt that asked for nothing useful or a reply that
gave nothing back, and after the fact — with the prompt assembled from a profile
and a job description that have both since changed — there is no way to
reconstruct either one.

Three rules this follows, all of them about not making things worse:

* It never raises. A logging failure that breaks a document generation would be
  a strictly worse outcome than the missing log it was trying to fix.
* It writes on its own session. The caller is usually mid-transaction — matching
  commits per job, generation holds one open across six calls — and a rollback
  there must not take the record of what went wrong with it. That is exactly the
  case the log exists for.
* It has a ceiling. Prompts carry whole job descriptions and profiles, so fields
  are truncated at a fixed size and old rows are pruned. A diagnostic that
  fills the disk stops being a diagnostic.
"""

import contextlib
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

TRUNCATION_NOTE = "\n\n[truncated by the LLM log]"


@dataclass(frozen=True)
class Context:
    stage: str = "unknown"
    job_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None


_context: contextvars.ContextVar[Context] = contextvars.ContextVar(
    "llm_log_context", default=Context()
)


@contextlib.contextmanager
def stage(name: str, job_id=None, application_id=None):
    """
    Label the calls made inside this block.

    Without it every row says "unknown", and a document generation is six calls
    with six different jobs — an empty resume is a question about exactly one of
    them. Context-local, so concurrent tasks cannot mislabel each other's calls.
    """
    current = _context.get()
    token = _context.set(Context(
        stage=name,
        job_id=job_id if job_id is not None else current.job_id,
        application_id=(application_id if application_id is not None
                        else current.application_id),
    ))
    try:
        yield
    finally:
        _context.reset(token)


def current_stage() -> str:
    return _context.get().stage


def _enabled() -> bool:
    return bool(getattr(settings, "LLM_LOG_ENABLED", True))


def _limit() -> int:
    return max(500, int(getattr(settings, "LLM_LOG_MAX_CHARS", 20000)))


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = str(text)
    limit = _limit()
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_NOTE


def _clip_messages(messages) -> list:
    """Same ceiling, applied per message, with the shape preserved."""
    clipped = []
    for message in (messages or []):
        if isinstance(message, dict):
            clipped.append({
                "role": str(message.get("role", "")),
                "content": _clip(message.get("content")) or "",
            })
        else:
            clipped.append({"role": "", "content": _clip(message) or ""})
    return clipped


def _usage(response) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    return (getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None))


def record(
    *,
    provider: str,
    model: str,
    messages,
    response: str | None = None,
    reasoning: str | None = None,
    finish_reason: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    duration_ms: int = 0,
    ok: bool = True,
    error: str | None = None,
) -> None:
    """Store one call. Never raises — see the module docstring."""
    if not _enabled():
        return
    try:
        from app.database import SessionLocal
        from app.models.llm_call import LLMCall

        context = _context.get()
        db = SessionLocal()
        try:
            db.add(LLMCall(
                stage=context.stage[:40],
                provider=(provider or "")[:40],
                model=(model or "")[:160],
                messages=_clip_messages(messages),
                response=_clip(response),
                reasoning=_clip(reasoning),
                finish_reason=(finish_reason or None) and str(finish_reason)[:40],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=int(duration_ms),
                ok=ok,
                error=_clip(error),
                job_id=context.job_id,
                application_id=context.application_id,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("llm_log: could not record a call: %s", exc)


@contextlib.contextmanager
def call(provider: str, model: str, messages, temperature=None, max_tokens=None):
    """
    Time a call and store it whichever way it ends.

    Used as `with llm_log.call(...) as entry: ... entry.finish(response)`. A call
    that raises is recorded too — a failure with its prompt attached is often
    the most useful row in the table.
    """
    started = time.monotonic()
    entry = _Entry(provider=provider, model=model)
    try:
        yield entry
    except Exception as exc:
        record(
            provider=provider, model=model, messages=messages,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=False, error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        record(
            provider=provider, model=model, messages=messages,
            response=entry.response, reasoning=entry.reasoning,
            finish_reason=entry.finish_reason,
            prompt_tokens=entry.prompt_tokens,
            completion_tokens=entry.completion_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=True,
        )


class _Entry:
    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self.response: str | None = None
        self.reasoning: str | None = None
        self.finish_reason: str | None = None
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None

    def finish(self, response: str | None, *, reasoning: str | None = None,
               finish_reason: str | None = None, raw=None) -> None:
        self.response = response
        self.reasoning = reasoning
        self.finish_reason = finish_reason
        if raw is not None:
            self.prompt_tokens, self.completion_tokens = _usage(raw)
            if self.finish_reason is None:
                try:
                    self.finish_reason = raw.choices[0].finish_reason
                except Exception:
                    pass

    def note_model(self, model: str) -> None:
        """For callers that only learn the served model from the reply."""
        if model:
            self.model = model


def prune(db, keep: int | None = None) -> int:
    """
    Drop the oldest rows beyond the retention limit.

    Prompts carry whole job descriptions, so this table grows faster than
    anything else in the schema. Returns how many were removed.
    """
    from app.models.llm_call import LLMCall

    keep = keep if keep is not None else int(getattr(settings, "LLM_LOG_KEEP_ROWS", 2000))
    if keep <= 0:
        return 0
    total = db.query(LLMCall).count()
    if total <= keep:
        return 0
    cutoff = (
        db.query(LLMCall.created_at)
        .order_by(LLMCall.created_at.desc())
        .offset(keep - 1)
        .limit(1)
        .scalar()
    )
    if cutoff is None:
        return 0
    removed = (
        db.query(LLMCall)
        .filter(LLMCall.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        logger.info("llm_log: pruned %d old calls (keeping %d)", removed, keep)
    return removed
