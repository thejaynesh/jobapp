"""
Read the draft back as the recruiter would, then fix what that finds.

Every document this app writes is a first draft. The generator makes one pass
per piece — bullets, summary, cover letter — and whatever comes out is what
gets compiled to PDF and sent. That is the one step of the pipeline with no
second look at all, which is odd given it is the step whose output a human
actually reads.

So: one call that is given the job description and the whole draft and asked
for concrete weaknesses, then a revision pass that hands each piece its own
notes. The calls are free; a cover letter that opens with a generic statement
of interest costs an application.

Three things make this worth the round trip rather than just raising the
temperature on the first draft:

* **It sees the whole application at once.** The bullets, the summary and the
  cover letter are written by three separate calls that cannot see each other,
  so the same accomplishment ends up as the headline of all three. Nothing
  before this could notice that.
* **It is asked for weaknesses, not for a rewrite.** A model asked to "improve
  this" produces a differently-worded draft of equal quality. Asked what a
  recruiter for *this* posting would object to, it produces objections.
* **It is allowed to say nothing.** A reviewer that must find three faults
  will invent three faults, and the revision will then damage a good draft to
  address them. `NONE` is an accepted answer and skips the revision entirely.

Nothing here can fail a generation. A critique that errors, comes back
unparseable, or comes back empty leaves the draft exactly as it was — the
draft is already the product, and this is an attempt to improve it.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Long enough that a bare "be more specific" doesn't trigger three revision
# calls, short enough that a real objection always survives.
_MIN_NOTE_CHARS = 25
# A reviewer with an unbounded list finds unbounded faults; past the first few
# it is padding, and padding is what makes a revision damage a good draft.
_MAX_NOTES = 4


def enabled() -> bool:
    return bool(getattr(settings, "SELF_REVIEW_ENABLED", True))


def _clean(notes) -> list[str]:
    """The notes worth acting on, deduplicated and capped."""
    out: list[str] = []
    seen: set[str] = set()
    for note in notes or []:
        text = " ".join(str(note).split())
        if len(text) < _MIN_NOTE_CHARS or text.upper() == "NONE":
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= _MAX_NOTES:
            break
    return out


def _draft_block(bullets, summary: str, cover_body: str) -> str:
    """The application as one text, the way a recruiter receives it."""
    lines = []
    if summary:
        lines.append(f"RESUME SUMMARY:\n{summary}\n")
    if bullets:
        lines.append("RESUME BULLETS:")
        for entry in bullets:
            if not isinstance(entry, dict):
                continue
            company = entry.get("company") or ""
            title = entry.get("title") or ""
            header = " — ".join(part for part in (title, company) if part)
            lines.append(f"  {header or 'Experience'}:")
            for bullet in entry.get("bullets") or []:
                lines.append(f"    - {bullet}")
        lines.append("")
    if cover_body:
        lines.append(f"COVER LETTER:\n{cover_body}")
    return "\n".join(lines).strip()


def critique(
    job_title: str,
    job_company: str,
    job_description: str,
    bullets,
    summary: str,
    cover_body: str,
    api_key: str,
    base_url: str,
    model: str,
    insights: dict | None = None,
) -> dict:
    """
    What a recruiter for this posting would object to.

    Returns `{"resume": [...], "cover_letter": [...]}`, either list possibly
    empty. Split rather than one list because the revision calls are separate:
    handing the bullet rewriter a note about the cover letter's opening is
    noise it will try to act on.

    Never raises.
    """
    from app.llm.providers import generation_chat

    draft = _draft_block(bullets, summary, cover_body)
    if not draft:
        return {"resume": [], "cover_letter": []}

    requirements = (insights or {}).get("requirements") or []
    keywords = (insights or {}).get("keywords") or []

    system_content = (
        "You are a hiring manager screening applications for one specific "
        "role. You are reading one candidate's resume content and cover "
        "letter. Name what is wrong with them.\n"
        "Look for, in this order:\n"
        "  - Requirements in the job description that the application never "
        "addresses, where the candidate's own material shows they could.\n"
        "  - Bullets that describe duties rather than results, or that have no "
        "concrete outcome where the evidence would support one.\n"
        "  - Generic phrasing that would read identically on an application "
        "for a different job.\n"
        "  - The same accomplishment used as the headline of the summary, the "
        "bullets and the cover letter — these were drafted separately and "
        "cannot see each other.\n"
        "  - A cover letter opening that is a statement of interest rather "
        "than a reason to keep reading.\n"
        "Rules:\n"
        "  - Every point must be actionable using ONLY material already in the "
        "draft. Never suggest adding experience, employers, numbers or "
        "technologies that are not there — that is asking for a lie.\n"
        "  - Be specific: quote the phrase you object to and say what to do "
        "instead. 'Be more specific' is not a point.\n"
        "  - If the draft is genuinely strong, say so. An empty list is a "
        "valid and useful answer; do not invent faults to fill the quota.\n"
        'Return ONLY a JSON object: {"resume": ["..."], "cover_letter": ["..."]}, '
        f"at most {_MAX_NOTES} points in each list."
    )

    user_content = (
        f"Job: {job_title} at {job_company}\n"
        + (f"Top requirements: {'; '.join(requirements)}\n" if requirements else "")
        + (f"Job keywords: {', '.join(keywords)}\n" if keywords else "")
        + f"\nJob description:\n{(job_description or '')[:6000]}\n"
        f"\n--- THE APPLICATION ---\n{draft}\n"
    )

    try:
        raw = generation_chat(
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            api_key=api_key, base_url=base_url, model=model,
            temperature=0.3, max_tokens=1200,
        )
    except Exception as exc:
        logger.warning("self_review: critique call failed: %s", exc)
        return {"resume": [], "cover_letter": []}

    # The same balanced-object hunt the matcher uses, for the same reason:
    # reasoning models wrap the object in their thinking and chattier ones put
    # a sentence either side.
    from app.services.matcher import _extract_json_object

    try:
        parsed = _extract_json_object(raw)
    except Exception as exc:
        logger.warning("self_review: unreadable critique (%s): %r", exc, (raw or "")[:160])
        return {"resume": [], "cover_letter": []}

    result = {
        "resume": _clean(parsed.get("resume")),
        "cover_letter": _clean(parsed.get("cover_letter")),
    }
    logger.info(
        "self_review: %d resume point(s), %d cover-letter point(s)",
        len(result["resume"]), len(result["cover_letter"]),
    )
    return result


def as_feedback(notes: list[str], user_feedback: str | None = None) -> str | None:
    """
    The critique in the shape the existing rewriters already accept.

    They all take a `feedback` string, which is how the user's own rewrite
    instructions reach them — so the revision pass is those same functions
    called again, rather than a second set of prompts to keep in step with the
    first. The user's words go last and are labelled as theirs: where the two
    disagree, the person wins.
    """
    if not notes:
        return user_feedback
    block = "A reviewer reading this draft against the job description raised:\n" + \
        "\n".join(f"- {note}" for note in notes) + \
        "\nAddress each point. Change nothing else, and add no claim that is " \
        "not already supported by the candidate's material."
    if user_feedback:
        return f"{block}\n\nThe candidate also asked for: {user_feedback}"
    return block
