"""
Roles worth adding to the target list, read out of the profile.

`target_roles` is the narrowest gate in the pipeline and the one nobody thinks
to revisit. It is typed once during setup, from whatever titles were on the
user's mind that afternoon, and then quietly decides what the whole system is
allowed to see. A skill picked up since — Flutter, Terraform, a year of Swift —
never becomes a role, so the postings that name it are rejected on the title
before anything reads them.

Which is why the suggestion is grounded in the profile rather than in a list of
common job titles. The evidence for "Flutter Developer" is that the profile says
Flutter; a generic ladder of engineering titles would suggest it to everybody
and mean nothing.

What this deliberately does not do
----------------------------------
*It does not add anything.* Every suggestion is a proposal with a reason, and
the user accepts them one at a time. `target_roles` decides what the whole
pipeline sees, so a background job quietly widening it would change the meaning
of every number on every page with nothing to point at.

*It does not suggest a role already covered.* Not just an exact duplicate — one
the title gate would already admit. The gate passes a title that shares a single
meaningful word with any target role, so a profile holding "Backend Engineer"
already admits every "… Engineer" posting there is, and offering "Platform
Engineer" as an addition would be offering a change that changes nothing.

*It does not rank or score.* A suggestion the user rejects costs one glance.
Ordering them by a confidence this cannot actually measure would just dress the
guess up.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 8

# Words too generic to establish that a role is new. The title gate matches on a
# single shared word, so these are exactly the words that make one target role
# admit half the market — see `already_covered`.
_STOP = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "at", "for", "to", "with",
    "senior", "junior", "staff", "lead", "principal", "mid", "level", "i",
    "ii", "iii", "sr", "jr", "entry", "associate",
})

_PROMPT = """\
You are helping someone widen the job titles their search covers.

Their profile:
- Skills: {skills}
- Recent experience: {experience}
- Projects: {projects}
- Roles they already target: {roles}

Suggest up to {cap} ADDITIONAL job titles that employers actually post under,
which this person would be a credible applicant for based on the evidence above.

Rules:
- Every suggestion must be justified by something concrete in the profile. If
  they list Flutter, "Flutter Developer" is fair; if they do not, it is not.
- Suggest real posted titles, not descriptions. "Mobile Engineer", not
  "someone who builds apps".
- Do not repeat a role they already target, or an obvious rewording of one.
- Prefer titles that reach postings the current list would miss entirely.
- If the profile does not support any new titles, return an empty list. That is
  a valid and useful answer.

Return ONLY JSON:
{{"suggestions": [{{"title": "...", "why": "one short sentence naming the "
"evidence in their profile"}}]}}
"""


def _clean(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower())) - _STOP


def already_covered(title: str, roles: list[str]) -> bool:
    """
    Whether the title gate would already admit postings named like this.

    Mirrors `matcher._title_matches_roles`, which passes a title sharing one
    meaningful word with any target role. That looseness is the reason this
    check has to exist: a profile holding "Backend Engineer" already admits
    every "… Engineer" posting, so suggesting "Platform Engineer" would be
    suggesting a change with no effect, and a list of those is worse than an
    empty list — it looks like progress.
    """
    candidate = _words(title)
    if not candidate:
        return True
    return any(candidate & _words(role) for role in roles)


def _profile_summary(profile_data: dict) -> dict:
    skills = profile_data.get("skills") or {}
    flat: list[str] = []
    for group in skills.values():
        flat.extend(str(item) for item in (group or []))

    experience = [
        f"{item.get('role', '')} at {item.get('company', '')}".strip(" at")
        for item in (profile_data.get("experience") or [])[:5]
    ]
    projects = [
        str(item.get("name") or item.get("title") or "")
        for item in (profile_data.get("projects") or [])[:5]
    ]
    return {
        "skills": ", ".join(flat[:60]) or "none listed",
        "experience": "; ".join(x for x in experience if x) or "none listed",
        "projects": ", ".join(x for x in projects if x) or "none listed",
        "roles": ", ".join(profile_data.get("target_roles") or []) or "none set",
    }


def suggest(profile_data: dict, api_key: str, base_url: str, model: str) -> dict:
    """
    Titles worth adding, each with the evidence for it.

    Returns `{"suggestions": [...], "error": str | None}`. Never raises: this is
    a button on a settings page, and a provider having a bad afternoon should
    cost the suggestion rather than the page.
    """
    from app.services.matcher import chat_completion

    profile_data = profile_data or {}
    roles = [str(r) for r in (profile_data.get("target_roles") or [])]
    summary = _profile_summary(profile_data)

    if summary["skills"] == "none listed" and summary["experience"] == "none listed":
        return {
            "suggestions": [],
            "error": "Fill in some skills or experience first — there is nothing "
                     "here to base a suggestion on.",
        }

    prompt = _PROMPT.format(cap=MAX_SUGGESTIONS, **summary)

    try:
        raw = chat_completion(
            [{"role": "user", "content": prompt}],
            api_key, base_url, model,
            # Warmer than scoring. This is asked for variety — the point is
            # titles the user had not thought of — where a verdict on one job
            # wants the same answer every time.
            temperature=0.4,
        )
        parsed = json.loads(_clean(raw))
    except Exception as exc:
        logger.warning("role_suggest: could not get suggestions: %s", exc)
        return {"suggestions": [], "error": f"Could not reach the model: {exc}"}

    out: list[dict] = []
    seen: set[str] = set()
    for entry in (parsed.get("suggestions") or [])[: MAX_SUGGESTIONS * 2]:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()[:80]
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        out.append({
            "title": title,
            "why": str(entry.get("why") or "").strip()[:200],
            # Shown rather than filtered out. "This would already get through"
            # is a useful thing to learn about your own list, and hiding it
            # would leave the user wondering why an obvious title never
            # appears.
            "covered": already_covered(title, roles),
        })
        if len(out) >= MAX_SUGGESTIONS:
            break

    # New ones first: the covered ones are context, not the answer.
    out.sort(key=lambda entry: entry["covered"])
    return {"suggestions": out, "error": None}


def add_role(profile_data: dict, title: str) -> list[str]:
    """The target roles with `title` appended, if it is not already there."""
    roles = [str(r) for r in (profile_data.get("target_roles") or [])]
    text = str(title or "").strip()[:80]
    if text and text.lower() not in {r.lower() for r in roles}:
        roles.append(text)
    return roles
