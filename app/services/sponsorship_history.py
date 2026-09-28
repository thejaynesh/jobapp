"""
Whether an employer has sponsored H-1B workers lately, from the Department of
Labor's public disclosure data.

A posting rarely says whether the employer sponsors, and when it does it is
quoted on the card already (`services.eligibility`). What an F-1 graduate
needs alongside that is the record: an employer that filed 1,400 certified
H-1B labor condition applications last year, 900 of them for computer
occupations, sponsors; one with none on file under its name probably does not.
Every H-1B petition starts with one of these applications, and DOL publishes
them all:

    https://www.dol.gov/agencies/eta/foreign-labor/performance
      → LCA_Disclosure_Data_FY2026_Q3.xlsx   (252 MB; the fiscal year to date)
      → LCA_Disclosure_Data_FY2025_Q4.xlsx   (79 MB; one quarter, for past years)

A past year is published a quarter per file; the current year as one file of
the year to date, replaced each quarter (measured 2026-09-28). So each
application is counted under the fiscal quarter of its decision date, not
under the file it came in, and a file speaks only for the quarters it holds
a real share of: the Q4 file's 1,864 June stragglers do not overwrite Q3. Loading the
same quarter from a later file replaces it, which is how a cumulative file
and a quarterly one never add up to twice the truth.

A 250 MB workbook is half a gigabyte of XML. It is read as a stream — the
shared strings, then the sheet a row at a time, keeping six columns — in about
two minutes and under 100 MB of memory, without a spreadsheet library.

Names are matched loosely, because the filings use legal names ("Amazon.com
Services LLC", "Meta Platforms, Inc") and postings use brands ("Amazon",
"Meta"): the same name with its legal suffixes gone, or failing that every
filed name that starts with it. The card says which names it counted, so a
wrong match is visible rather than silent.

USCIS's own Employer Data Hub counts approved petitions, which is closer to the
question, but uscis.gov refuses requests from servers outright; DOL's files
are the public record that answers.
"""

import bisect
import logging
import os
import re
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DOL_PAGE = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
STATE_KEY = "h1b_history"
_FILE = re.compile(r'href="([^"]*?LCA_Disclosure_Data_FY(\d{4})_Q([1-4])\.xlsx)"', re.I)
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COLUMNS = ("CASE_STATUS", "DECISION_DATE", "VISA_CLASS", "SOC_CODE", "NEW_EMPLOYMENT",
            "EMPLOYER_NAME")
_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 600
_MAX_FILES_PER_RUN = 3
# A file speaks for a quarter it holds at least this share of its biggest
# quarter's rows for. Measured: the FY2025 Q4 file's June stragglers are 1.6%
# of its September quarter; the year-to-date file's first quarter is 35% of
# its third. Relative to the biggest rather than to the total, so the first
# quarter of a whole year's file is not outvoted by the other three.
_AUTHORITY_SHARE = 0.2
_EXCEL_EPOCH = date(1899, 12, 30)

# Legal endings a posting leaves off: "Ernst & Young U.S. LLP", "Deloitte
# Consulting LLP", "JPMorgan Chase & Co.".
_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
    "company", "gmbh", "bv", "sa", "plc", "pvt", "pte", "holdings", "lp", "llp",
    "pllc", "pc", "na", "n", "a", "l", "p", "u", "s", "us", "usa", "and", "the",
})
_DBA = re.compile(r"\s+(?:d\s*/?\s*b\s*/?\s*a|doing business as)\s+.*$", re.I)
# Prefix matching needs a name with something to it: "ea" would match "each …".
_MIN_PREFIX_CHARS = 4
# Ignoring spaces ("walmart" in "Wal-Mart Associates") only for longer names,
# and only when nothing else matched: "citi" would take in "Citi Cellular".
_MIN_COMPACT_CHARS = 6


# --- Names ------------------------------------------------------------------

def employer_key(name: str | None) -> str:
    """A name with punctuation, legal suffixes and "doing business as" tails gone."""
    text = _DBA.sub("", name or "")
    text = re.sub(r"\([^)]*\)", " ", text).replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


# --- The files ---------------------------------------------------------------

def quarter_label(fy: int, q: int) -> str:
    return f"FY{fy} Q{q}"


def _parse_label(label: str) -> tuple[int, int]:
    match = re.match(r"FY(\d{4}) Q([1-4])$", label or "")
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def fiscal_quarter(day: date) -> tuple[int, int]:
    """The federal fiscal year and quarter of a date: October starts the year."""
    if day.month >= 10:
        return day.year + 1, 1
    return day.year, (day.month - 1) // 3 + 2


def disclosure_files(html: str) -> list[tuple[int, int, str]]:
    """The LCA disclosure workbooks the page links, newest first: (fy, q, url)."""
    found: dict[tuple[int, int], str] = {}
    for href, fy, q in _FILE.findall(html or ""):
        url = href if href.startswith("http") else f"https://www.dol.gov{href}"
        url = re.sub(r"(?<!:)//+", "/", url)
        found.setdefault((int(fy), int(q)), url)
    return [(fy, q, url) for (fy, q), url in sorted(found.items(), reverse=True)]


def _window(fy: int, q: int, quarters: int) -> list[tuple[int, int]]:
    """The `quarters` fiscal quarters ending with (fy, q), newest first."""
    out = []
    for _ in range(max(1, quarters)):
        out.append((fy, q))
        fy, q = (fy, q - 1) if q > 1 else (fy - 1, 4)
    return out


def _column(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n


def read_rows(path: str):
    """
    The workbook's rows as {column: text}, for the columns this module reads.

    Shared strings first (the sheet refers to them by index), then the sheet a
    row at a time, each row cleared from the tree once read — without that the
    parser keeps every row it has seen, which was 7 GB for one quarter.
    """
    with zipfile.ZipFile(path) as book:
        strings: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            with book.open("xl/sharedStrings.xml") as f:
                for _, el in ET.iterparse(f, events=("end",)):
                    if el.tag == _NS + "si":
                        strings.append("".join(t.text or "" for t in el.iter(_NS + "t")))
                        el.clear()
        sheet = next((n for n in book.namelist() if n.startswith("xl/worksheets/sheet")),
                     "xl/worksheets/sheet1.xml")
        header: dict[int, str] | None = None
        with book.open(sheet) as f:
            parent = None
            for event, el in ET.iterparse(f, events=("start", "end")):
                if event == "start":
                    if el.tag == _NS + "sheetData":
                        parent = el
                    continue
                if el.tag != _NS + "row":
                    continue
                values: dict[int, str] = {}
                for cell in el:
                    index = _column(cell.get("r") or "")
                    if header is not None and index not in header:
                        continue
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter(_NS + "t"))
                    else:
                        v = cell.find(_NS + "v")
                        value = v.text if v is not None and v.text is not None else ""
                        if kind == "s" and value:
                            value = strings[int(value)]
                    values[index] = value
                if parent is not None:
                    parent.clear()
                if header is None:
                    header = {i: name.strip() for i, name in values.items()
                              if name.strip() in _COLUMNS}
                    continue
                yield {header[i]: v for i, v in values.items()}


def _decision_day(raw) -> date | None:
    raw = str(raw if raw is not None else "").strip()
    if not raw:
        return None
    try:
        return _EXCEL_EPOCH + timedelta(days=int(float(raw)))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
    return None


def _int(raw) -> int:
    try:
        return max(0, int(float(raw or 0)))
    except (TypeError, ValueError):
        return 0


def aggregate(rows) -> tuple[dict, Counter]:
    """
    Certified H-1B applications per (quarter label, employer key), and how many
    H-1B rows of any outcome each quarter held — the second is what says which
    quarters a file speaks for.
    """
    counts: dict[tuple[str, str], dict] = {}
    per_quarter: Counter = Counter()
    for row in rows:
        if (row.get("VISA_CLASS") or "").strip() != "H-1B":
            continue
        day = _decision_day(row.get("DECISION_DATE"))
        if day is None:
            continue
        label = quarter_label(*fiscal_quarter(day))
        per_quarter[label] += 1
        if (row.get("CASE_STATUS") or "").strip().lower() != "certified":
            continue
        name = (row.get("EMPLOYER_NAME") or "").strip()
        key = employer_key(name)
        if not key:
            continue
        entry = counts.setdefault((label, key), {"names": Counter(), "certified": 0,
                                                 "computer": 0, "new_employment": 0})
        entry["names"][name] += 1
        entry["certified"] += 1
        entry["computer"] += (row.get("SOC_CODE") or "").strip().startswith("15-")
        entry["new_employment"] += _int(row.get("NEW_EMPLOYMENT"))
    return counts, per_quarter


def authoritative(per_quarter: Counter) -> set[str]:
    """The quarters a file holds enough of its rows in to speak for."""
    biggest = max(per_quarter.values(), default=0)
    return {label for label, n in per_quarter.items() if biggest and n >= _AUTHORITY_SHARE * biggest}


def store(db: Session, counts: dict, quarters: set[str]) -> int:
    """Replace those quarters' rows with these counts. Returns rows written."""
    from app.models.h1b_filing import H1bFiling

    if not quarters:
        return 0
    db.query(H1bFiling).filter(H1bFiling.quarter.in_(quarters)).delete(synchronize_session=False)
    rows = [
        {"quarter": label, "employer_key": key[:500],
         "employer_name": entry["names"].most_common(1)[0][0][:500],
         "certified": entry["certified"], "computer": entry["computer"],
         "new_employment": entry["new_employment"]}
        for (label, key), entry in counts.items() if label in quarters
    ]
    for start in range(0, len(rows), 5000):
        db.bulk_insert_mappings(H1bFiling, rows[start:start + 5000])
    return len(rows)


def _download(url: str, dest) -> None:
    with httpx.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            dest.write(chunk)


def load_file(db: Session, url: str) -> dict:
    """One workbook, downloaded to a temporary file, read, counted and stored."""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as dest:
            _download(url, dest)
        counts, per_quarter = aggregate(read_rows(path))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    quarters = authoritative(per_quarter)
    written = store(db, counts, quarters)
    return {"url": url, "quarters": sorted(quarters, key=_parse_label), "rows": written}


# --- Keeping it current ------------------------------------------------------

def refresh(db: Session, profile_data: dict | None = None) -> dict:
    """
    Load the files the window needs and has not got, and drop quarters that
    have left it. A few requests when nothing is new; a few minutes when DOL
    has published a quarter.
    """
    from app.models.h1b_filing import H1bFiling
    from app.services.tunables import value

    if not value(profile_data, "h1b_history_enabled"):
        return {"skipped": "off"}
    resp = httpx.get(DOL_PAGE, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    files = disclosure_files(resp.text)
    if not files:
        return {"error": "the disclosure page lists no LCA workbooks"}

    newest_fy, newest_q, _ = files[0]
    window = _window(newest_fy, newest_q, int(value(profile_data, "h1b_history_quarters")))
    labels = [quarter_label(fy, q) for fy, q in window]
    state = dict((profile_data or {}).get(STATE_KEY) or {})
    loaded = set(state.get("files") or [])
    have = {row[0] for row in db.query(H1bFiling.quarter).distinct()}

    todo: list[str] = []
    for fy, q in window:
        # Its own file, or the newest of its year, which holds the year to date.
        url = next((u for f, n, u in files if (f, n) == (fy, q)), None) \
            or next((u for f, n, u in files if f == fy and n > q), None)
        if url and url not in loaded and url not in todo \
                and (quarter_label(fy, q) not in have or (fy, q) == (newest_fy, newest_q)):
            todo.append(url)

    report = {"window": labels, "loaded": []}
    for url in todo[:_MAX_FILES_PER_RUN]:
        try:
            result = load_file(db, url)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("sponsorship_history: %s failed: %s", url, exc)
            report.setdefault("errors", []).append(f"{url}: {exc}")
            continue
        loaded.add(url)
        report["loaded"].append(result)
        logger.info("sponsorship_history: %s → %s (%d rows)", url.rsplit("/", 1)[-1],
                    ", ".join(result["quarters"]), result["rows"])

    pruned = db.query(H1bFiling).filter(H1bFiling.quarter.notin_(labels)) \
        .delete(synchronize_session=False)
    report["pruned_rows"] = pruned
    report["state"] = {"files": sorted(loaded), "window": labels,
                       "checked_at": datetime.now(timezone.utc).isoformat()}
    reset_cache()
    return report


# --- Reading it ----------------------------------------------------------------

_CACHE: dict = {"at": 0.0, "value": None}
_CACHE_SECONDS = 600
# Replaced in tests (see conftest), which render job cards by the hundred.
_reader = None


def needs_sponsorship(profile_data: dict | None) -> bool:
    """Whether the profile's screening answer says the candidate will need it."""
    from app.services import screening

    return screening.answers(profile_data or {}).get("sponsorship_required", "") \
        .lower().startswith("yes")


def read_snapshot(db: Session | None = None) -> dict | None:
    """
    The window's totals per employer, or None when the feature is off or holds
    no data. Opens its own session when not given one.
    """
    from app.models.h1b_filing import H1bFiling
    from app.models.profile import Profile
    from app.services.tunables import value

    own = db is None
    if own:
        from app.database import SessionLocal

        db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        data = dict(profile.data or {}) if profile else {}
        if not value(data, "h1b_history_enabled"):
            return None
        quarters = sorted({row[0] for row in db.query(H1bFiling.quarter).distinct()},
                          key=_parse_label, reverse=True)
        quarters = quarters[: int(value(data, "h1b_history_quarters"))]
        if not quarters:
            return None
        totals: dict[str, dict] = {}
        for key, name, certified, computer, new in db.query(
                H1bFiling.employer_key, H1bFiling.employer_name, H1bFiling.certified,
                H1bFiling.computer, H1bFiling.new_employment).filter(
                H1bFiling.quarter.in_(quarters)):
            entry = totals.setdefault(key, {"name": name, "certified": 0, "computer": 0,
                                            "new_employment": 0})
            entry["certified"] += certified
            entry["computer"] += computer
            entry["new_employment"] += new
        ordered = sorted(quarters, key=_parse_label)
        compact = {key.replace(" ", ""): key for key in totals}
        return {
            "totals": totals,
            "keys": sorted(totals),
            "compact": compact,
            "compact_keys": sorted(compact),
            "period": ordered[0] if len(ordered) == 1 else f"{ordered[0]} – {ordered[-1]}",
            "needs_sponsorship": needs_sponsorship(data),
        }
    finally:
        if own:
            db.close()


def _starting_with(ordered: list[str], prefix: str) -> list[str]:
    out = []
    for candidate in ordered[bisect.bisect_left(ordered, prefix):]:
        if not candidate.startswith(prefix):
            break
        out.append(candidate)
    return out


def snapshot(db: Session | None = None) -> dict | None:
    now = time.monotonic()
    if _CACHE["at"] and now - _CACHE["at"] < _CACHE_SECONDS:
        return _CACHE["value"]
    try:
        value = (_reader or read_snapshot)(db)
    except Exception as exc:
        logger.warning("sponsorship_history: could not read the filings: %s", exc)
        value = None
    _CACHE.update(at=now, value=value)
    return value


def reset_cache() -> None:
    _CACHE.update(at=0.0, value=None)


def for_company(company: str | None, db: Session | None = None) -> dict | None:
    """
    The employer's H-1B record over the window, or None when there is no data
    to say anything with. `certified` is 0 when no filed name matches.
    """
    snap = snapshot(db)
    if not snap:
        return None
    key = employer_key(company)
    if not key:
        return None
    totals = snap["totals"]
    # The name itself, and every filed name that goes on from it: "Amazon" is
    # "Amazon.com Services", "Amazon Web Services" and "Amazon Development
    # Center" — and a stray "Amazon LLC" with two filings besides.
    matched = [key] if key in totals else []
    if len(key) >= _MIN_PREFIX_CHARS:
        matched += _starting_with(snap["keys"], key + " ")
    if not matched and len(key.replace(" ", "")) >= _MIN_COMPACT_CHARS:
        compact = key.replace(" ", "")
        matched = [snap["compact"][c] for c in _starting_with(snap["compact_keys"], compact)]
    entries = sorted((totals[k] for k in matched), key=lambda e: -e["certified"])
    return {
        "certified": sum(e["certified"] for e in entries),
        "computer": sum(e["computer"] for e in entries),
        "new_employment": sum(e["new_employment"] for e in entries),
        "names": [e["name"] for e in entries[:3]],
        "more_names": max(0, len(entries) - 3),
        "period": snap["period"],
        "needs_sponsorship": snap["needs_sponsorship"],
    }
