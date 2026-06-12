import re


def parse_experience_level(title: str, description: str) -> str:
    """
    Infer seniority from job title and description text.

    Returns "entry", "mid", or "senior".
    """
    text = (title + " " + description).lower()

    senior_patterns = [
        r"\bsenior\b", r"\bsr\b", r"\blead\b", r"\bprincipal\b",
        r"\bstaff\b", r"\bdirector\b", r"\bvp\b",
    ]
    if any(re.search(p, text) for p in senior_patterns):
        return "senior"

    entry_patterns = [
        r"\bjunior\b", r"\bjr\b", r"\bentry[\s\-]level\b",
        r"\b0[\s\-]?[-–][\s\-]?[12]\s*years?\b", r"\bnew\s+grad\b",
        r"\bfresh(man|er)?\b",
    ]
    if any(re.search(p, text) for p in entry_patterns):
        return "entry"

    return "mid"
