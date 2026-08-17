"""
How long the candidate has actually worked, derived from the dates they typed.

The matcher's rubric spends 25 of its 100 points on "judge required years
against the candidate's total", but nothing in the app ever asked for a number
of years — the experience form collects free-text start and end dates. So the
total was always zero and that quarter of the score was decided on a blank.

Rather than add a field people would have to keep in sync with the dates beside
it, this reads the dates. They're free text, so parsing is forgiving: "Sep
2024", "September 2024", "2024-09", "09/2024", "2024" and an empty or "Present"
end date all work. Anything unreadable yields None and is left out rather than
guessed at.
"""

import logging
import re
from datetime import date

logger = logging.getLogger(__name__)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ONGOING = {"present", "current", "now", "ongoing", "today", "to date"}

_YEAR_MONTH = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_MONTH_YEAR = re.compile(r"^(\d{1,2})[-/](\d{4})$")
_YEAR_ONLY = re.compile(r"^(\d{4})$")

DAYS_PER_YEAR = 365.25


def parse_month(text: str | None, *, default_ongoing: bool = False) -> date | None:
    """
    A free-text date as the first of its month.

    `default_ongoing` treats a blank value as "still there", which is what an
    empty end date means on a CV and not what an empty start date means.
    """
    cleaned = (text or "").strip().lower().replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or cleaned in _ONGOING or "present" in cleaned:
        return date.today() if (default_ongoing or cleaned in _ONGOING
                                or "present" in cleaned) else None

    words = cleaned.split(" ")
    if len(words) == 2 and words[0][:3] in _MONTHS and words[1].isdigit():
        year = int(words[1])
        if 1900 <= year <= 2200:
            return date(year, _MONTHS[words[0][:3]], 1)

    match = _YEAR_MONTH.match(cleaned)
    if match and 1 <= int(match.group(2)) <= 12:
        return date(int(match.group(1)), int(match.group(2)), 1)

    match = _MONTH_YEAR.match(cleaned)
    if match and 1 <= int(match.group(1)) <= 12:
        return date(int(match.group(2)), int(match.group(1)), 1)

    match = _YEAR_ONLY.match(cleaned)
    if match and 1900 <= int(match.group(1)) <= 2200:
        # Mid-year, so a bare "2022"–"2024" reads as two years rather than
        # accidentally claiming January-to-January precision.
        return date(int(match.group(1)), 7, 1)

    return None


def entry_span(entry: dict) -> tuple[date, date] | None:
    """The (start, end) this role covers, or None if the dates don't parse."""
    start = parse_month(entry.get("start_date"))
    if start is None:
        return None
    end = parse_month(entry.get("end_date"), default_ongoing=True)
    if end is None or end < start:
        return None
    return start, end


def entry_years(entry: dict) -> float | None:
    """
    Years for one role. An explicit `years` on the entry wins, so a profile
    that already carries one keeps it.
    """
    explicit = entry.get("years")
    if explicit not in (None, ""):
        try:
            return round(float(explicit), 1)
        except (TypeError, ValueError):
            logger.debug("experience: unreadable years %r", explicit)

    span = entry_span(entry)
    if span is None:
        return None
    return round((span[1] - span[0]).days / DAYS_PER_YEAR, 1)


def total_years(experience: list[dict]) -> float:
    """
    Total time worked, counting overlapping roles once.

    Summing each entry would inflate the total for anyone who held two roles at
    the same time — an internship during a degree, a contract alongside a job —
    and inflating it is the direction that hurts, since it pushes the candidate
    past the junior threshold that keeps senior-titled jobs out.
    """
    spans = sorted(
        (span for span in (entry_span(e) for e in experience or []) if span),
        key=lambda s: s[0],
    )
    # Entries with an explicit `years` but no parseable dates can't be merged
    # for overlap, but they are still real experience — dropping them the
    # moment any OTHER entry had dates undercounted the total.
    explicit = sum(
        entry_years(e) or 0
        for e in experience or []
        if entry_span(e) is None and e.get("years") not in (None, "")
    )
    if not spans:
        return round(explicit, 1)

    merged: list[list[date]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    days = sum((end - start).days for start, end in merged)
    return round(days / DAYS_PER_YEAR + explicit, 1)
