"""
Your stories, told once in STAR form and used wherever a story is asked for.

"Tell us about a time you disagreed with a teammate", "describe a project you
are proud of": application forms ask for stories, and interviews ask for
little else. A bullet is the headline of one; the situation, what you had to
do, what you did and what came of it are only in your head, and the drafted
answers could only work from headlines.

A story is kept in the profile (`stories`): a title, the four STAR parts, the
skills it shows and, optionally, the experience or project it comes from. A
story tied to an entry that is switched out of resumes is left out with it.

Where they are used:

* drafted answers get the stories most relevant to the question, as evidence
  the model may draw on, and their figures count as known;
* the application page lists the stories most relevant to the posting under
  "Stories to have ready", for interview preparation.

Relevance is word overlap, weighted towards the story's skills and title: it
has to run on every page load and every question, and a story's own words are
a good enough index of what it is about.
"""

import re

STORE_KEY = "stories"
PARTS = ("situation", "task", "action", "result")
_WORD = re.compile(r"[a-z][a-z0-9+#.]*[a-z0-9+#]|[a-z]")
_STOP = frozenset("""
a an and are as at be but by for from has have i in is it its me my of on or our so that the
their them they this to was we were what when where which who why will with you your about
tell describe time give example please how did do does
""".split())


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def all_stories(profile_data: dict | None) -> list[dict]:
    return [s for s in (profile_data or {}).get(STORE_KEY) or []
            if isinstance(s, dict) and s.get("id")]


def usable(profile_data: dict) -> list[dict]:
    """Stories whose entry, if they name one, is still in the (filtered) profile."""
    entry_ids = {e.get("id") for section in ("experience", "projects")
                 for e in profile_data.get(section) or []}
    return [s for s in all_stories(profile_data)
            if not s.get("entry_id") or s["entry_id"] in entry_ids]


def score(story: dict, words: set[str]) -> int:
    skills = set().union(*(_words(s) for s in story.get("skills") or [])) if story.get("skills") else set()
    title = _words(story.get("title", ""))
    body = _words(" ".join(story.get(p, "") for p in PARTS))
    return 3 * len(skills & words) + 2 * len(title & words) + len(body & words)


def relevant(profile_data: dict, text: str, limit: int = 3) -> list[dict]:
    """The stories most about `text` (a question, a posting), best first, none that share nothing."""
    words = _words(text)
    ranked = sorted(((score(s, words), i, s) for i, s in enumerate(usable(profile_data))),
                    key=lambda t: (-t[0], t[1]))
    return [s for n, _, s in ranked if n > 0][:limit]


def as_evidence(stories: list[dict]) -> str:
    lines = []
    for story in stories:
        lines.append(f"STORY — {story.get('title') or 'untitled'}:")
        for part in PARTS:
            if story.get(part):
                lines.append(f"  {part.title()}: {story[part]}")
    return "\n".join(lines)


def from_form(form) -> dict:
    get = lambda k: " ".join(str(form.get(k) or "").split())  # noqa: E731
    return {
        "title": get("title")[:200],
        **{part: str(form.get(part) or "").strip()[:2000] for part in PARTS},
        "skills": [s.strip() for s in get("skills").split(",") if s.strip()][:20],
        "entry_id": get("entry_id") or None,
    }
