"""
Structured location preferences.

The profile stores `location_preferences`:
    {"regions": ["usa", "canada", "uk"], "remote_ok": true, "custom": ["Dubai"]}

Older profiles only have free-text `target_locations`; `normalize_prefs` parses
those into the same shape, so both formats work everywhere.

The registry below drives three things:
  - search: the location strings sent to search-based sources (LinkedIn,
    Indeed, JSearch, Jooble, ...) — one or two well-formed strings per region
    instead of passing raw profile text verbatim,
  - adzuna: Adzuna's per-country API endpoints,
  - keywords: matching fetched jobs' location text back to a region so
    clearly-out-of-region jobs are dropped before spending an LLM call.
"""

import re

# 2-letter US state codes are matched case-sensitively with word boundaries
# ("Austin, TX") so they don't collide with ordinary words.
_US_STATE_ABBREVS = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC"
).split()

REGIONS: dict[str, dict] = {
    "usa": {
        "label": "United States",
        "search": ["United States"],
        "adzuna": "us",
        "jobicy_geo": "usa",
        "keywords": [
            "united states", "usa", "u.s.", "america", "new york", "nyc",
            "san francisco", "bay area", "seattle", "austin", "boston",
            "chicago", "los angeles", "denver", "atlanta", "miami",
            "washington dc", "california", "texas", "colorado", "georgia",
            "virginia", "north carolina", "silicon valley", "palo alto",
            "mountain view", "san jose", "sunnyvale", "redmond", "bellevue",
        ],
        "abbrevs": _US_STATE_ABBREVS,
    },
    "canada": {
        "label": "Canada",
        "search": ["Canada"],
        "adzuna": "ca",
        "jobicy_geo": "canada",
        "keywords": [
            "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary",
            "waterloo", "ontario", "quebec", "british columbia", "alberta",
            "mississauga", "edmonton",
        ],
        "abbrevs": [],
    },
    "uk": {
        "label": "United Kingdom",
        "search": ["London, United Kingdom", "United Kingdom"],
        "adzuna": "gb",
        "jobicy_geo": "uk",
        "keywords": [
            "united kingdom", "london", "england", "scotland", "manchester",
            "cambridge", "oxford", "edinburgh", "bristol", "glasgow", "leeds",
        ],
        "abbrevs": ["UK"],
    },
    "europe": {
        "label": "Europe (EU)",
        "search": ["Berlin, Germany", "Amsterdam, Netherlands"],
        "adzuna": "de",
        "jobicy_geo": "europe",
        "keywords": [
            "germany", "berlin", "munich", "netherlands", "amsterdam",
            "france", "paris", "ireland", "dublin", "spain", "madrid",
            "barcelona", "portugal", "lisbon", "poland", "warsaw", "krakow",
            "sweden", "stockholm", "denmark", "copenhagen", "switzerland",
            "zurich", "austria", "vienna", "belgium", "brussels", "europe",
        ],
        "abbrevs": ["EU"],
    },
    "india": {
        "label": "India",
        "search": ["Bengaluru, India", "India"],
        "adzuna": "in",
        "jobicy_geo": "india",
        "keywords": [
            "india", "bangalore", "bengaluru", "hyderabad", "mumbai", "pune",
            "delhi", "chennai", "gurgaon", "gurugram", "noida", "kolkata",
        ],
        "abbrevs": [],
    },
    "australia": {
        "label": "Australia",
        "search": ["Sydney, Australia", "Australia"],
        "adzuna": "au",
        "jobicy_geo": "australia",
        "keywords": ["australia", "sydney", "melbourne", "brisbane", "perth"],
        "abbrevs": [],
    },
}

REGION_OPTIONS = [(key, cfg["label"]) for key, cfg in REGIONS.items()]

# Legacy free-text entries that mean "no restriction", not a place.
_FILLER = frozenset({
    "open to all locations", "relocation ok", "anywhere", "any", "worldwide",
    "open to relocation", "flexible",
})

_LEGACY_REGION_NAMES = {
    cfg["label"].lower(): key for key, cfg in REGIONS.items()
} | {"usa": "usa", "us": "usa", "united states": "usa", "uk": "uk",
     "united kingdom": "uk", "london": "uk", "canada": "canada",
     "europe": "europe", "india": "india", "australia": "australia"}

MAX_SEARCH_LOCATIONS = 6  # bounds query fan-out per search-based source


def normalize_prefs(profile_data: dict) -> dict:
    """
    Return {"regions": [...], "remote_ok": bool, "custom": [...]} from either
    the structured `location_preferences` or legacy free-text `target_locations`.
    """
    prefs = profile_data.get("location_preferences")
    if isinstance(prefs, dict):
        return {
            "regions": [r for r in (prefs.get("regions") or []) if r in REGIONS],
            "remote_ok": bool(prefs.get("remote_ok", True)),
            "custom": [c for c in (prefs.get("custom") or []) if c],
        }

    regions: list[str] = []
    custom: list[str] = []
    remote_ok = False
    for entry in profile_data.get("target_locations") or []:
        low = entry.strip().lower()
        if not low or low in _FILLER:
            continue
        if low == "remote":
            remote_ok = True
        elif low in _LEGACY_REGION_NAMES:
            key = _LEGACY_REGION_NAMES[low]
            if key not in regions:
                regions.append(key)
        else:
            custom.append(entry.strip())
    return {"regions": regions, "remote_ok": remote_ok or not (regions or custom), "custom": custom}


def search_locations(prefs: dict) -> list[str]:
    """Well-formed location strings for search-based job sources."""
    result: list[str] = []
    for region in prefs.get("regions") or []:
        for term in REGIONS[region]["search"]:
            if term not in result:
                result.append(term)
    for entry in prefs.get("custom") or []:
        if entry not in result:
            result.append(entry)
    if prefs.get("remote_ok") and "Remote" not in result:
        result.append("Remote")
    if not result:
        result = ["Remote", "United States"]
    return result[:MAX_SEARCH_LOCATIONS]


def adzuna_countries(prefs: dict) -> list[str]:
    countries = []
    for region in prefs.get("regions") or []:
        code = REGIONS[region].get("adzuna")
        if code and code not in countries:
            countries.append(code)
    return countries or ["us"]


def jobicy_geos(prefs: dict) -> list[str | None]:
    geos = []
    for region in prefs.get("regions") or []:
        geo = REGIONS[region].get("jobicy_geo")
        if geo and geo not in geos:
            geos.append(geo)
    return geos or [None]


def _region_matches(region: str, text: str, text_lower: str) -> bool:
    cfg = REGIONS[region]
    if any(kw in text_lower for kw in cfg["keywords"]):
        return True
    # 2-letter codes: case-sensitive word-boundary match ("Austin, TX")
    return any(re.search(rf"\b{ab}\b", text) for ab in cfg["abbrevs"])


def location_allowed(location_text: str, is_remote: bool, prefs: dict) -> bool | None:
    """
    True  — the job's location matches the preferences,
    False — it clearly belongs to a different region,
    None  — undecidable from the text (let the LLM weigh it).
    """
    regions = prefs.get("regions") or []
    custom = [c.lower() for c in prefs.get("custom") or []]
    if not regions and not custom:
        return None  # no location restriction configured
    if is_remote and prefs.get("remote_ok"):
        return True
    if not isinstance(location_text, str) or not location_text.strip():
        return None
    text = location_text.strip()
    text_lower = text.lower()

    if "remote" in text_lower and prefs.get("remote_ok"):
        return True
    if any(c in text_lower for c in custom):
        return True
    for region in regions:
        if _region_matches(region, text, text_lower):
            return True
    # Clearly some OTHER known region → drop; otherwise undecidable.
    for region in REGIONS:
        if region not in regions and _region_matches(region, text, text_lower):
            return False
    return None


def describe_prefs(prefs: dict) -> str:
    """Human-readable summary for LLM prompts."""
    parts = [REGIONS[r]["label"] for r in prefs.get("regions") or []]
    parts += prefs.get("custom") or []
    if prefs.get("remote_ok"):
        parts.append("Remote")
    return ", ".join(parts) if parts else "No restriction"
