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

# State names in full. Two-word names are matched as phrases; the rest on word
# boundaries (see `_region_keyword_match`).
_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "connecticut", "delaware",
    "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "utah", "vermont", "west virginia",
    "wisconsin", "wyoming", "washington state", "washington, dc",
)

REGIONS: dict[str, dict] = {
    "usa": {
        "label": "United States",
        "search": ["United States"],
        "adzuna": ["us"],
        "jobicy_geo": "usa",
        "keywords": [
            # "america" on its own is not here, and deliberately: it matches
            # "South America", "Latin America" and "Central America", all of
            # which are somewhere else. "north america" is unambiguous and is
            # what location fields actually say. Nothing is lost — the region
            # is already named three other ways below, plus 25 cities and
            # every state code.
            "united states", "usa", "u.s.", "north america", "new york", "nyc",
            "san francisco", "bay area", "seattle", "austin", "boston",
            "chicago", "los angeles", "denver", "atlanta", "miami",
            "washington dc", "california", "texas", "colorado", "georgia",
            "virginia", "north carolina", "silicon valley", "palo alto",
            "mountain view", "san jose", "sunnyvale", "redmond", "bellevue",
            # Every state by name. Five used to be listed, so "Cambridge,
            # Massachusetts" had no US evidence but a UK city name, and was
            # rejected for a US-only profile. "washington" and "georgia" stay
            # out as bare words — see above for the DC and state entries.
            *_US_STATE_NAMES,
        ],
        "abbrevs": _US_STATE_ABBREVS,
        "country_codes": ["US", "USA"],
    },
    "canada": {
        "label": "Canada",
        "search": ["Canada"],
        "adzuna": ["ca"],
        "jobicy_geo": "canada",
        "keywords": [
            "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary",
            "waterloo", "ontario", "quebec", "british columbia", "alberta",
            "mississauga", "edmonton", "winnipeg", "halifax", "victoria, bc",
            "manitoba", "saskatchewan", "nova scotia", "new brunswick",
            "newfoundland",
        ],
        # Province codes, so "London, ON" reads as Canada rather than as the
        # UK city it shares a name with.
        "abbrevs": ["ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE"],
        "country_codes": ["CA"],
    },
    "uk": {
        "label": "United Kingdom",
        "search": ["London, United Kingdom", "United Kingdom"],
        "adzuna": ["gb"],
        "jobicy_geo": "uk",
        "keywords": [
            "united kingdom", "london", "england", "scotland", "manchester",
            "cambridge", "oxford", "edinburgh", "bristol", "glasgow", "leeds",
        ],
        "abbrevs": ["UK"],
        "country_codes": ["UK", "GB"],
    },
    "europe": {
        "label": "Europe",
        "search": ["Europe", "Berlin, Germany", "Amsterdam, Netherlands"],
        # Adzuna is per-country in Europe; query the largest tech markets.
        "adzuna": ["de", "nl", "fr", "es", "it", "pl"],
        "jobicy_geo": "europe",
        "keywords": [
            "germany", "berlin", "munich", "netherlands", "amsterdam",
            "france", "paris", "ireland", "dublin", "spain", "madrid",
            "barcelona", "portugal", "lisbon", "poland", "warsaw", "krakow",
            "sweden", "stockholm", "denmark", "copenhagen", "switzerland",
            "zurich", "austria", "vienna", "belgium", "brussels", "europe",
            "italy", "rome", "milan", "czech", "prague", "finland", "helsinki",
            "norway", "oslo", "romania", "bucharest", "hungary", "budapest",
            "greece", "athens", "estonia", "tallinn", "luxembourg",
        ],
        "abbrevs": ["EU", "EMEA"],
        "country_codes": ["DE", "NL", "FR", "ES", "IT", "PL", "IE", "PT", "SE", "DK", "CH", "AT", "BE", "CZ", "FI", "NO", "RO", "HU", "GR", "EE", "LU", "EU"],
    },
    "india": {
        "label": "India",
        "search": ["Bengaluru, India", "India"],
        "adzuna": ["in"],
        "jobicy_geo": "india",
        "keywords": [
            "india", "bangalore", "bengaluru", "hyderabad", "mumbai", "pune",
            "delhi", "chennai", "gurgaon", "gurugram", "noida", "kolkata",
        ],
        "abbrevs": [],
        "country_codes": ["IN"],
    },
    "australia": {
        "label": "Australia",
        "search": ["Sydney, Australia", "Australia"],
        "adzuna": ["au"],
        "jobicy_geo": "australia",
        "keywords": [
            "australia", "sydney", "melbourne", "brisbane", "perth",
            "canberra", "adelaide",
        ],
        "abbrevs": [],
        "country_codes": ["AU"],
    },
    "new_zealand": {
        "label": "New Zealand",
        "search": ["Auckland, New Zealand", "New Zealand"],
        "adzuna": ["nz"],
        "jobicy_geo": "new-zealand",
        "keywords": ["new zealand", "auckland", "wellington", "christchurch"],
        "abbrevs": ["NZ"],
        "country_codes": ["NZ"],
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

# Free-text spellings users type for a region ("united states", "US", "america",
# "EU", ...) → region key. Superset of the legacy names used by normalize_prefs.
_REGION_ALIASES: dict[str, str] = _LEGACY_REGION_NAMES | {
    "america": "usa", "u.s.": "usa", "u.s.a.": "usa", "united states of america": "usa",
    "states": "usa",
    "england": "uk", "great britain": "uk", "britain": "uk",
    "eu": "europe", "european union": "europe",
    "aus": "australia",
    "nz": "new_zealand",
}


def resolve_region_key(text: str) -> str | None:
    """Map free text like 'united states' or 'US' to a region key, else None."""
    if not isinstance(text, str):
        return None
    return _REGION_ALIASES.get(text.strip().lower())


MAX_SEARCH_LOCATIONS = 8  # bounds query fan-out per search-based source


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
    """
    Well-formed location strings for search-based job sources. Every selected
    region contributes its primary search term first, so with many regions the
    cap trims secondary terms — never a whole region.
    """
    result: list[str] = []

    def _add(term: str) -> None:
        if term and term not in result and len(result) < MAX_SEARCH_LOCATIONS:
            result.append(term)

    regions = prefs.get("regions") or []
    for region in regions:                      # primaries first
        _add(REGIONS[region]["search"][0])
    for entry in prefs.get("custom") or []:
        _add(entry)
    if prefs.get("remote_ok"):
        _add("Remote")
    for region in regions:                      # secondaries fill leftover room
        for term in REGIONS[region]["search"][1:]:
            _add(term)

    if not result:
        result = ["Remote", "United States"]
    return result


def adzuna_countries(prefs: dict) -> list[str]:
    countries: list[str] = []
    for region in prefs.get("regions") or []:
        for code in REGIONS[region].get("adzuna") or []:
            if code not in countries:
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
    """
    Whether this location text names somewhere in this region.

    Keywords are matched on word boundaries, not as bare substrings. As
    substrings they were wrong in both directions: "usa" is inside
    **Jer·usa·lem**, so every job in Israel matched the United States, and
    "america" is inside "South America". Because `location_allowed` tests the
    user's own regions first and returns on the first hit, those came back
    True — the filter admitted them and each one cost a scoring call.

    Multi-word keywords keep a plain containment test. "new york" cannot
    appear inside a longer word, and `\\b` around a phrase with a space in it
    buys nothing.
    """
    return _region_keyword_match(region, text_lower) or _region_abbrev_match(
        region, text
    )


def _region_keyword_match(region: str, text_lower: str) -> bool:
    """A place name from this region, on word boundaries."""
    for kw in REGIONS[region]["keywords"]:
        if " " in kw or "." in kw:
            if kw in text_lower:
                return True
        elif re.search(rf"\b{re.escape(kw)}\b", text_lower):
            return True
    return False


def _region_abbrev_match(region: str, text: str) -> bool:
    """
    A 2-letter code from this region: case-sensitive, word-bounded.

    Much weaker evidence than a place name, because US state codes collide
    with ISO-3166 country codes — CA is California and Canada, DE is Delaware
    and Germany, IN is Indiana and India, IL is Illinois and Israel, MT is
    Montana and Malta, PA is Pennsylvania and Panama. So this is only consulted
    after every region's place names have had their say; see
    `location_allowed`.
    """
    return any(re.search(rf"\b{ab}\b", text) for ab in REGIONS[region]["abbrevs"])


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
    has_text = isinstance(location_text, str) and location_text.strip()
    if is_remote and prefs.get("remote_ok") and not has_text:
        return True
    if not has_text:
        return None
    text = location_text.strip()
    text_lower = text.lower()

    if any(c in text_lower for c in custom):
        return True

    named_pref = [r for r in regions if _region_keyword_match(r, text_lower)]
    others = [r for r in REGIONS if r not in regions]
    named_other = [r for r in others if _region_keyword_match(r, text_lower)]

    if (is_remote or "remote" in text_lower) and prefs.get("remote_ok"):
        # "Remote – India" is remote for somebody in India. Rejected only when
        # the text names somewhere else and nowhere wanted, and does not say it
        # is open to everyone; anything less certain still passes.
        if (named_other and not named_pref
                and not _WORLDWIDE_RE.search(text_lower)
                and not any(_region_abbrev_match(r, text) or _country_code_match(r, text)
                            for r in regions)):
            return False
        return True

    # A place name beats a code, but city names are not unique: Cambridge,
    # Vienna, Dublin, Melbourne, Paris and Athens are all also in the United
    # States. A code the named region cannot account for is the tiebreak —
    # "Cambridge, MA" names a UK city, but MA is not a UK code, so it is the
    # Massachusetts one. "Toronto, CA" and "Berlin, DE" stay rejected, because
    # CA and DE are Canada's and Germany's own codes.
    #
    # Only `False` filters, so any doubt resolves to None: a wrongly-rejected
    # job is never seen, and a wrongly-admitted one costs one scoring call.
    if named_pref:
        if _unexplained_code(others, named_pref, text):
            return None
        return True
    if named_other:
        if _unexplained_code(regions, named_other, text):
            return True
        return False
    if any(_region_abbrev_match(r, text) for r in regions):
        return True
    if any(_region_abbrev_match(r, text) for r in others):
        return False
    return None


_WORLDWIDE_RE = re.compile(r"worldwide|anywhere|global|any location|all locations", re.I)


def _country_code_match(region: str, text: str) -> bool:
    """The region's own ISO code, as a word: "Remote - US", "Berlin, DE"."""
    return any(re.search(rf"\b{code}\b", text)
               for code in REGIONS[region].get("country_codes", []))


def _unexplained_code(candidates: list[str], named: list[str], text: str) -> bool:
    """
    Whether one of `candidates`' codes appears and is not simply the country
    code of a region the text already names.
    """
    explained = {code for r in named for code in REGIONS[r].get("country_codes", [])}
    for region in candidates:
        for code in REGIONS[region]["abbrevs"]:
            if code not in explained and re.search(rf"\b{code}\b", text):
                return True
    return False


def describe_prefs(prefs: dict) -> str:
    """Human-readable summary for LLM prompts."""
    parts = [REGIONS[r]["label"] for r in prefs.get("regions") or []]
    parts += prefs.get("custom") or []
    if prefs.get("remote_ok"):
        parts.append("Remote")
    return ", ".join(parts) if parts else "No restriction"
