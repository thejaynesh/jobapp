"""
Jobs the browser saw, handed over without anybody fetching anything.

When you browse LinkedIn normally, the page asks its own API for job cards and
gets back far more than it renders — full descriptions, applicant counts, salary
bands. A content script reads those responses as they arrive and posts them
here. No extra requests are made, so there is nothing to rate-limit and nothing
to detect; the traffic is a person using the site.

This matters most for LinkedIn specifically. The guest API the server polls
returns ten cards a page and needs a separate request per description, which is
what makes `LINKEDIN_MAX_DETAIL_FETCHES` the real ceiling on that source.
Voyager returns descriptions inline, so the ceiling disappears.

Parsing is deliberately shape-based rather than path-based
------------------------------------------------------------
The obvious implementation reads `elements[].jobCardUnion.*.jobPosting.title`.
That breaks the first time LinkedIn reorganizes its response, and it breaks
silently — an empty harvest looks identical to an idle browser.

So instead this walks the whole payload and picks out any object that *looks*
like a job: something with a title, a company, and an id or a URL. Field names
are matched from a list of aliases. A redesign that moves the nesting around
keeps working; only a rename of every field at once would defeat it, and that
is exactly the kind of change that shows up as a sudden drop to zero rather
than as quiet corruption.
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.models.job import Job, JobStatus
from app.services.deduplication import (
    compute_dedupe_hash,
    enrich_from,
    was_archived,
    find_existing_job,
    merge_description,
    merge_or_skip,
)
from app.services.descriptions import clean as clean_description
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

# Where harvested jobs say they came from. Its own source name so the yield is
# visible next to the API sources rather than blended into them.
HARVEST_SOURCE = "linkedin_harvest"

# The extractor is shape-based and therefore host-agnostic; only the
# interceptor's registration decided it saw LinkedIn and nothing else. Now that
# it can be registered per site, each host gets its own source name — otherwise
# Indeed's yield disappears into LinkedIn's number and neither can be judged.
# Keep in step with `HARVEST_SITES` in extension/sites.js: a host the extension
# harvests but this does not name still works — the extractor never looked at
# the host — but its yield lands in LinkedIn's bucket, where nobody can judge
# it. See docs/HARVEST.md.
HARVEST_SOURCES = {
    "linkedin.com": HARVEST_SOURCE,
    "indeed.com": "indeed_harvest",
    "glassdoor.com": "glassdoor_harvest",
    "myworkdayjobs.com": "workday_harvest",
    "dice.com": "dice_harvest",
    "ziprecruiter.com": "ziprecruiter_harvest",
    "wellfound.com": "wellfound_harvest",
    "builtin.com": "builtin_harvest",
    "simplyhired.com": "simplyhired_harvest",
    "monster.com": "monster_harvest",
    "otta.com": "otta_harvest",
    "welcometothejungle.com": "otta_harvest",
    "jobright.ai": "jobright_harvest",
    "tsenta.com": "tsenta_harvest",
    # Tsenta's board is served by an API on a different domain entirely
    # (`api.autojobs.me/api/v1/jobs/recommendations`), and a harvested payload
    # is filed under the host it came *from*. Without this line its jobs would
    # be counted as LinkedIn's — the fallback source — and its samples would be
    # filtered off the panel as belonging to no board of ours, which is the
    # same mistake in the opposite direction from the ad-tech hosts.
    "autojobs.me": "tsenta_harvest",
    "joinhandshake.com": "handshake_harvest",
    "hiring.cafe": "hiringcafe_harvest",
    # Where hiring.cafe redirects to, and therefore the host every payload
    # from it actually arrives under.
    "hiringcafe.com": "hiringcafe_harvest",
    "amazon.jobs": "amazon_harvest",
    "google.com": "google_harvest",
    "my.greenhouse.io": "greenhouse_harvest",
    "workingnomads.com": "workingnomads_harvest",
    "jobspresso.co": "jobspresso_harvest",
}


def source_for_url(url: str | None) -> str:
    """
    Which harvest source a payload belongs to, from the page it came off.

    Falls back to the LinkedIn name rather than inventing a source: an
    unrecognised host means the interceptor was registered somewhere this
    doesn't know about yet, and a wrong-but-known bucket is easier to notice
    and correct than a new one appearing silently.
    """
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").lower()
    for domain, source in HARVEST_SOURCES.items():
        if host == domain or host.endswith(f".{domain}"):
            return source
    return HARVEST_SOURCE

# Field aliases, most specific first. Several are checked because one payload
# calls it `companyName` and another nests it under `companyDetails`.
#
# Names from LinkedIn's Voyager come first because that payload is the one this
# was written against, then Indeed's mosaic payload, Glassdoor's GraphQL one,
# and Workday's CXS. They are simply appended: the reader tries them in order
# and takes the first that is present, so adding a host costs a few strings
# rather than a parser.
_TITLE_KEYS = (
    "title", "jobTitle", "jobPostingTitle", "name",
    "displayTitle", "normTitle", "jobTitleText",  # Indeed
    "jobTitleText", "listingTitle",               # Glassdoor
)
# `name` earns its place — plenty of boards call the job's title that — and it
# is also the weakest alias here by a wide margin, because *everything* in a
# normalized payload has a name. Handshake's response is the demonstration: of
# 407 objects the reader recognised as jobs, 52 were postings and 355 were
# enums, employers and industries whose only qualification was a `name` and a
# company inherited from an enclosing object.
#
# They did no harm while a URL was required, since none of them had one. That
# made the URL rule load-bearing for something it was never about, and the
# moment a posting URL is reconstructed from an id — which is the whole point
# of `_POSTING_URL` — those 355 become 355 junk rows per payload.
_WEAK_TITLE_KEYS = ("name",)
_STRONG_TITLE_KEYS = tuple(k for k in _TITLE_KEYS if k not in _WEAK_TITLE_KEYS)

# Where a posting lives, for a board that identifies one by id and never gives
# a link. Without an entry here such a board loses every job it has: the
# reader walks the payload, recognises the postings, and `_normalize` refuses
# them all for want of a URL. Handshake lost 407 objects a payload that way and
# Greenhouse's aggregate board lost its whole board until `publicUrl` was added
# to `_URL_KEYS`.
#
# Deliberately short, and it should only grow from a URL somebody has actually
# opened. A guessed template is worse than a dropped job by some distance: the
# URL becomes the row's identity and one of its three dedupe keys, so a wrong
# one writes rows that point nowhere *and* cannot be merged with the real
# posting when a source that does give links finds it later. Dropping a job
# costs that job until the next visit. Inventing its address corrupts the
# record permanently.
_POSTING_URL = {
    # Stable for years, and what the site's own cards link to.
    HARVEST_SOURCE: "https://www.linkedin.com/jobs/view/{id}/",
    # Handshake's GraphQL `Job` nodes carry a numeric `id` and no link at all.
    "handshake_harvest": "https://app.joinhandshake.com/jobs/{id}",
    # Wellfound's job pages are /jobs/<id>-<slug>, and /jobs/<id> redirects
    # to them; the id is the `identifier.value` of the page's JobPosting.
    "wellfound_harvest": "https://wellfound.com/jobs/{id}",
    # Indeed's cards link through a click tracker (`/rc/clk?jk=…&…`), relative
    # and full of per-visit parameters. The `jobkey` is the posting, and
    # `viewjob?jk=` is its canonical page — stable, and the same address the
    # next visit will produce, so the two sightings merge.
    "indeed_harvest": "https://www.indeed.com/viewjob?jk={id}",
}

# Sources whose own posting link is worse than the address rebuilt from the id
# (see Indeed above). Everywhere else a link the payload gives wins.
_PREFER_POSTING_URL = frozenset({"indeed_harvest"})

# Where a relative link on each board points. A payload read from the page's
# embedded data carries paths like `/job-listing/…`, and stored as-is those are
# addresses that lead nowhere.
_BOARD_ORIGIN = {
    "indeed_harvest": "https://www.indeed.com",
    "glassdoor_harvest": "https://www.glassdoor.com",
    "ziprecruiter_harvest": "https://www.ziprecruiter.com",
    "simplyhired_harvest": "https://www.simplyhired.com",
    "monster_harvest": "https://www.monster.com",
    "dice_harvest": "https://www.dice.com",
    "wellfound_harvest": "https://wellfound.com",
    "builtin_harvest": "https://builtin.com",
}
_COMPANY_KEYS = (
    "companyName", "company", "companyUrn", "primarySubtitle", "subtitle",
    "employerName", "truncatedCompany",           # Indeed / Glassdoor
    "hiringOrganization", "employer",
)
_LOCATION_KEYS = (
    # `locations` is an array of strings; `_text` takes the first, which is the
    # primary posting location. Greenhouse's job-seeker board uses it.
    "formattedLocation", "locationName", "location", "locations",
    "secondarySubtitle",
    "secondaryDescription",
    "formattedLocationFull", "jobLocationCity", "locationsText",  # Indeed
    "locationName", "locationString",                             # Glassdoor
    "locationsText", "primaryLocation",                           # Workday
)
_DESCRIPTION_KEYS = (
    "description", "jobDescription", "descriptionText",
    "snippet", "jobDescriptionText",              # Indeed
    "descriptionFragments", "jobDescriptionHtml",  # Glassdoor
)
_URL_KEYS = (
    "jobPostingUrl", "applyUrl", "companyApplyUrl", "url", "link",
    "jobUrl", "viewJobLink", "externalPath",      # Indeed / Workday
    "seoJobLink", "jobViewUrl",                   # Glassdoor (relative)
    # Greenhouse's aggregate board. Worth more than the average alias: it holds
    # the *employer's own* board URL — job-boards.greenhouse.io/<slug>/jobs/<id>
    # — which is both what a person should apply through and the slug the
    # fetcher needs to read that whole company by API afterwards.
    #
    # Its absence was not a missing nicety. `_normalize` requires a URL, so
    # every job on that board was read, found to have none, and dropped.
    "publicUrl",
)
_ID_KEYS = (
    "jobPostingId", "entityUrn", "trackingUrn", "referenceId", "id",
    "jobkey", "jobKey",                           # Indeed
    "listingId", "jobListingId",                  # Glassdoor
    "bulletFields",                               # Workday requisition ids
)
_REMOTE_KEYS = (
    "workplaceType", "workRemoteAllowed", "workplaceTypes",
    "remoteWorkModelType", "isRemote", "remoteType",
    # Greenhouse's board: "remote" | "hybrid" | "in_person". Reading it matters
    # because remote is a filter the search itself was set to, so a job that
    # came back remote and got stored as on-site contradicts the query.
    "workType",
)
# Voyager sends pay the guest API never does, in a nested object whose exact
# path moves around. Read shape-first like everything else here: find the
# object that has a min or a max and a currency, wherever it is sitting.
_SALARY_KEYS = (
    "salaryInsights", "compensation", "baseSalary", "payRange", "salary",
    # Handshake's band. It was listed under `_PAY_TEXT_KEYS` only, where
    # `_text` of a dict is the empty string — so a board publishing a
    # structured min and max had it read as prose, found nothing, and stored
    # no pay at all.
    "salaryRange",
    "compensationBreakdown",
)

# Boards that state pay in minor units, and the divisor to get currency out.
#
# Handshake sends `{"min": 10000000, "max": 13000000, "currency": "USD"}` for a
# job paying $100,000-$130,000. Read as dollars that is a ten-million-dollar
# salary, which would then be indexed, filtered on and sorted by — a guessed
# salary is worse than a missing one, because the filter acts on it.
#
# Per source rather than by magnitude, for the same reason `_POSTING_URL` is:
# guessing "that number looks too big, divide it" is right until a board pays
# in yen. This only grows from a band somebody has read against the posting.
_SALARY_SCALE = {
    "handshake_harvest": 100,
}

# Below this, an annual figure is not an annual figure.
#
# Handshake's `paySchedule` cannot be trusted to say which: of 21 bands with
# one, three claimed `HOURLY_WAGE` while stating $75,000, $100,000 and
# $100,000 for Full-Time permanent roles — employers leaving the first option
# in a dropdown. And there is nowhere to record the period anyway: `jobs` has
# `salary_min` and no `salary_period`, so an hourly 25 and an annual 25,000
# would be stored identically.
#
# So only bands that can only be annual are kept. A genuine hourly rate is
# dropped rather than written down as a salary, which is the same trade the
# rest of this module makes: no number beats a wrong one.
_MIN_PLAUSIBLE_ANNUAL = 10_000
_SALARY_MIN_KEYS = ("minSalary", "min", "minValue", "minAmount", "from")
_SALARY_MAX_KEYS = ("maxSalary", "max", "maxValue", "maxAmount", "to")
_CURRENCY_KEYS = ("currencyCode", "currency", "currencyIso")

# LinkedIn ids arrive as bare numbers or wrapped in an urn.
_URN_ID_RE = re.compile(r"(\d{6,})")

# A payload nests deeply; without a ceiling a cyclic or pathological structure
# would walk forever.
# Guards against a pathological payload, not against a big one. Both numbers
# were chosen when a job payload was a card list; a modern board answers with a
# GraphQL response whose jobs sit ten or more levels down inside a document of
# a hundred thousand nodes, and the old ceilings stopped the walk before it
# reached them. Every host the walker reads successfully has small payloads
# (Tsenta's biggest sample is 7KB) and every host it reads nothing from has
# large ones (Handshake 161KB, Hiring Cafe 992KB) — which is not a coincidence
# worth defending.
#
# The payload is already parsed and in memory by the time this runs, so walking
# all of it costs a traversal and nothing else.
_MAX_DEPTH = 24
_MAX_NODES = 200_000


def _text(value) -> str:
    """
    A string out of whatever shape the field arrived in.

    Voyager writes rich text as `{"text": "...", "attributes": [...]}`, and
    company names sometimes as `{"name": "..."}`.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "name", "localizedName", "title"):
            if key in value:
                return _text(value[key])
    if isinstance(value, list) and value:
        return _text(value[0])
    return ""


def _first(node: dict, keys: tuple) -> str:
    for key in keys:
        if key in node:
            found = _text(node[key])
            if found:
                return found
    return ""


# An opaque posting id that is not a number. LinkedIn's are numeric urns;
# Indeed's `jobkey` is a 16-character alphanumeric string, and reading only
# numbers left every harvested Indeed job with no id — which drops it to
# URL-only dedupe, so the same posting re-inserts itself whenever the URL
# picks up a different tracking parameter.
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def _job_id(node: dict) -> str:
    for key in _ID_KEYS:
        raw = _text(node.get(key))
        if not raw:
            continue
        match = _URN_ID_RE.search(raw)
        if match:
            return match.group(1)
        if raw.isdigit():
            return raw
        # Requiring a digit keeps this from matching an ordinary word that
        # happens to be sitting under a key named "id".
        if _OPAQUE_ID_RE.match(raw) and any(c.isdigit() for c in raw):
            return raw
    return ""


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, dict):
        # Voyager wraps money as {"amount": "150000", "currencyCode": "USD"}.
        for key in ("amount", "value"):
            if key in value:
                return _number(value[key])
        return None
    try:
        parsed = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


# "$190,978 - $231,050", "$85,000 - $100,000", "£60k – £75k". A range written
# for a person to read, which is how Greenhouse's board states pay — there are
# no min/max keys to find, so `_salary` alone came back empty on every row.
_PAY_RANGE_RE = re.compile(
    r"([$£€]?)\s*([\d,]+(?:\.\d+)?)\s*(k?)"
    # A dash, or the word "to" — an alternation rather than a character class,
    # so "to" has to be the word and not any letter out of t/o.
    r"(?:\s*[-–—]\s*|\s+to\s+)"
    r"[$£€]?\s*([\d,]+(?:\.\d+)?)\s*(k?)",
    re.I,
)
_PAY_SINGLE_RE = re.compile(r"([$£€])\s*([\d,]+(?:\.\d+)?)\s*(k?)", re.I)
_CURRENCY_BY_SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR"}


def _amount(digits: str, suffix: str) -> float | None:
    try:
        value = float(digits.replace(",", ""))
    except (TypeError, ValueError):
        return None
    if suffix.lower() == "k":
        value *= 1000
    return value if value > 0 else None


def _salary_from_text(text: str) -> dict:
    """Pay out of a human-readable range, or {} if there is none in there."""
    if not text:
        return {}
    match = _PAY_RANGE_RE.search(text)
    if match:
        symbol, low_digits, low_k, high_digits, high_k = match.groups()
        # A currency symbol or a `k` has to be present, or a range of anything
        # reads as money: "2 to 5 years experience" parses perfectly well as
        # 2–5, and a band of 2 sitting in the salary columns is worse than an
        # empty one — a filter would act on it.
        if not symbol and not (low_k or high_k):
            return {}
        low = _amount(low_digits, low_k)
        high = _amount(high_digits, high_k)
        if low is None and high is None:
            return {}
        if low is not None and high is not None and high < low:
            low, high = high, low
        return {
            "salary_min": low if low is not None else high,
            "salary_max": high,
            "salary_currency": _CURRENCY_BY_SYMBOL.get(symbol or "", None),
        }

    single = _PAY_SINGLE_RE.search(text)
    if single:
        symbol, digits, suffix = single.groups()
        value = _amount(digits, suffix)
        if value is not None:
            # A lone figure is the floor, not a ceiling — the same reading
            # `_salary` gives one, so a filter on the top of the band does not
            # silently exclude it.
            return {"salary_min": value, "salary_max": None,
                    "salary_currency": _CURRENCY_BY_SYMBOL.get(symbol or "", None)}
    return {}


# Keys whose value is a pay range written as prose rather than as numbers.
_PAY_TEXT_KEYS = ("payRanges", "payRange", "salaryRange", "compensationRange",
                  "salaryText", "payText")


def _salary(node: dict) -> dict:
    """
    Pay, from wherever in this node's subtree it happens to live.

    The guest API never sends this at all, which is most of why harvesting is
    worth turning on: the browser sees the band and the server cannot. Searched
    by shape rather than by path for the same reason as everything else here —
    a redesign that moves the nesting keeps working.
    """
    for key in _SALARY_KEYS:
        block = node.get(key)
        if block is None:
            continue
        for candidate in _walk(block):
            low = _first_number(candidate, _SALARY_MIN_KEYS)
            high = _first_number(candidate, _SALARY_MAX_KEYS)
            if low is None and high is None:
                continue
            if low is None:
                low = high  # a lone figure is the floor, not a ceiling
            if high is not None and low is not None and high < low:
                low, high = high, low
            currency = _first(candidate, _CURRENCY_KEYS) or _first(node, _CURRENCY_KEYS)
            return {
                "salary_min": low,
                "salary_max": high,
                "salary_currency": (currency or "").upper()[:8] or None,
            }

    # No min/max anywhere. Some boards only ever state pay as prose.
    for key in _PAY_TEXT_KEYS:
        found = _salary_from_text(_text(node.get(key)))
        if found:
            return found
    return {}


def _annual_salary(node: dict, source: str) -> dict:
    """
    `_salary`, converted out of minor units and kept only when it is annual.

    Two corrections, both measured rather than assumed. The scale comes from
    `_SALARY_SCALE` and the plausibility floor from `_MIN_PLAUSIBLE_ANNUAL` —
    see those for why a board's own `paySchedule` is not trusted to say which
    period a band is in.

    Returns `{}` rather than a partial row: a salary filter reading a floor
    with no ceiling, or an hourly rate filed as a salary, does more damage than
    an empty column. Every other source is unaffected — the scale defaults to
    1 and the floor only ever drops a band we could not have recorded the
    period of anyway.
    """
    found = _salary(node)
    if not found:
        return {}

    scale = _SALARY_SCALE.get(source, 1)
    low = found.get("salary_min")
    high = found.get("salary_max")
    if scale != 1:
        low = low / scale if isinstance(low, (int, float)) else low
        high = high / scale if isinstance(high, (int, float)) else high

    # Judged on the floor, which is the number a filter compares against.
    if not isinstance(low, (int, float)) or low < _MIN_PLAUSIBLE_ANNUAL:
        return {}

    return {
        "salary_min": low,
        "salary_max": high,
        "salary_currency": found.get("salary_currency"),
        # This function's whole job is to keep only the bands that are annual —
        # `_SALARY_SCALE` un-scales minor units and `_MIN_PLAUSIBLE_ANNUAL`
        # drops anything that reads as a rate. Having decided that, it has to
        # say so: the pay filter reads `salary_annual_*`, those are derived
        # from the period, and a NULL period means this band is excluded from
        # the filter despite the posting stating pay.
        "salary_period": "year",
    }


def _first_number(node: dict, keys: tuple) -> float | None:
    if not isinstance(node, dict):
        return None
    for key in keys:
        if key in node:
            found = _number(node[key])
            if found is not None:
                return found
    return None


def _sponsorship(node: dict) -> dict:
    """
    What the posting's own screening fields say about visa sponsorship.

    `eligibility.scan` derives this by running regexes over prose, which is the
    only option when prose is all there is. Handshake states it outright:

        studentScreen: {
            willingToSponsorCandidate: False,
            acceptsCptCandidates: True,
            acceptsOptCandidates: True,
            workAuthNotDisclosed: False,
        }

    A field beats an inference. The regex has to decide whether "we are unable
    to offer sponsorship at this time" is negated, is boilerplate, or is about
    some other role in the same advert; this is the employer answering the
    question on a form.

    `workAuthNotDisclosed` is honoured rather than read past: a screen the
    employer declined to fill in says nothing, and recording "will not sponsor"
    from an unanswered form would be worse than leaving the column empty.

    Advisory only, like the prose version — `sponsorship_direction` is
    displayed and never filtered or scored on. Returns `{}` when the posting
    carries no screen, which is every board but this one so far.
    """
    screen = node.get("studentScreen")
    if not isinstance(screen, dict):
        return {}
    if screen.get("workAuthNotDisclosed") is True:
        return {}

    willing = screen.get("willingToSponsorCandidate")
    if not isinstance(willing, bool):
        return {}

    # The CPT/OPT answers ride along in the note because they are finer than
    # the yes/no: an employer that will not sponsor a visa may still take a
    # student on OPT, and those are very different answers to "can I apply".
    accepts = [
        name for name, key in (("CPT", "acceptsCptCandidates"),
                               ("OPT", "acceptsOptCandidates"))
        if screen.get(key) is True
    ]
    note = (
        "The employer's screening says they will sponsor a visa."
        if willing else
        "The employer's screening says they will not sponsor a visa."
    )
    if accepts:
        note += f" {' and '.join(accepts)} candidates are accepted."
    return {
        "sponsorship_note": note,
        "sponsorship_direction": "positive" if willing else "negative",
    }


def _is_remote(node: dict) -> bool:
    for key in _REMOTE_KEYS:
        value = node.get(key)
        if isinstance(value, bool):
            return value
        text = _text(value).lower()
        if "remote" in text:
            return True
    return False


def _walk(node, depth: int = 0, budget: list | None = None):
    """Every dict anywhere in the payload, depth- and size-capped."""
    if budget is None:
        budget = [_MAX_NODES]
    if depth > _MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1

    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1, budget)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1, budget)


# Keys whose value describes the employer of everything nested under it. A
# subset of `_COMPANY_KEYS`: `subtitle` and `primarySubtitle` are in there
# because LinkedIn puts the company in them *on a card*, and they mean nothing
# as a scope — a subtitle above a list is a heading, not an employer.
_COMPANY_SCOPE_KEYS = (
    "companyName", "company", "companyUrn", "employerName", "employer",
    "hiringOrganization", "organization",
)


def _walk_scoped(node, depth: int = 0, budget: list | None = None,
                 company: str = ""):
    """
    Every dict in the payload, each paired with the company named above it.

    The reason this exists rather than `_walk`. A board that answers with

        {"employer": {"name": "Acme"}, "postings": [{"title": ..., "id": ...}]}

    has said whose jobs these are exactly once, at the top, and the rule that a
    title and a company must sit in the *same* object cannot see it. That shape
    is not unusual — it is what any payload does when it groups postings under
    an employer, or resolves companies through a lookup table — and the cost of
    not reading it is every job in the response.

    The hint passed to a node is its *ancestors'* company, never its own:
    `_looks_like_job` tests the node itself first, and this is only the fallback
    for when that finds nothing.
    """
    if budget is None:
        budget = [_MAX_NODES]
    if depth > _MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1

    if isinstance(node, dict):
        yield node, company
        # Anything named here applies to everything below, and the nearest
        # naming wins — a company on the posting beats one on the page.
        inner = _first(node, _COMPANY_SCOPE_KEYS) or company
        for value in node.values():
            yield from _walk_scoped(value, depth + 1, budget, inner)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_scoped(item, depth + 1, budget, company)


def _looks_like_job(node: dict, company: str = "") -> bool:
    """
    A title and a company, plus something to identify it by.

    All three are required together on purpose. A title alone matches every
    heading in the payload, and a company alone matches the sidebar.

    `company` is one relaxation of "together": an employer named on an
    enclosing object counts for the postings inside it. The identifier is what
    keeps that honest — a heading that inherits a company still has no id and
    no link, so it is still not a job.

    And one tightening, for the node whose claim rests entirely on having a
    `name`. Two weak signals — a title that is only a name, and a company that
    is only inherited — used to add up to a job, and in a normalized payload
    they add up to every enum and lookup row in the response. Such a node has
    to corroborate: a posting says where it is or what it involves, and a
    `JobTypeEnum` says neither.
    """
    if not isinstance(node, dict):
        return False
    if not _first(node, _TITLE_KEYS):
        return False
    own_company = _first(node, _COMPANY_KEYS)
    if not (own_company or company):
        return False
    if not (_job_id(node) or _first(node, _URL_KEYS)):
        return False
    if not _first(node, _STRONG_TITLE_KEYS) and not own_company:
        return bool(_first(node, _LOCATION_KEYS)
                    or _first(node, _DESCRIPTION_KEYS))
    return True


# Greenhouse's board links each card twice: `publicUrl` goes wherever the
# employer chose to host the posting, and `viewJobPath` is always
# /jobs/<slug>/<id> on Greenhouse's own domain.
_GREENHOUSE_VIEW_PATH = re.compile(r"^/jobs/([A-Za-z0-9_.-]+)/(\d+)/?$")


def _greenhouse_board_url(node: dict) -> str:
    """
    The canonical Greenhouse URL for a card, when the card names its slug.

    Worth deriving because `publicUrl` is often the employer's own careers page
    — `ifit.com/careers?gh_jid=123` — which names the job but not the company
    slug. Two things are lost with it:

      * The description. A greenhouse.io/<slug>/jobs/<id> address is one free
        API call away from the full text; a bespoke careers page is a scrape
        that may or may not work.
      * The slug, which is that company's entire board on every future fetch
        cycle. That compounding is most of why this board is worth harvesting
        at all, and throwing it away over a URL shape would be a poor trade.
    """
    match = _GREENHOUSE_VIEW_PATH.match(_text(node.get("viewJobPath")))
    if not match:
        return ""
    slug, job_id = match.groups()
    return f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"


def _normalize(node: dict, source: str = HARVEST_SOURCE,
               company: str = "", refused: dict | None = None) -> dict | None:
    """
    One recognised node as a job row, or `None` with the reason recorded.

    `refused` is how the reason gets out. There are two ways to fail here and
    both used to be the same silence, which cost days on Handshake: 265
    title-bearing objects, 150 of them recognised as jobs, and a live harvest
    reporting `found: 0` a hundred and thirty-six times. A board losing every
    posting for want of a URL reported exactly what a board with no postings in
    it reported, so the search started from "the reader cannot read this
    payload" and went looking in the wrong place entirely.
    """
    title = _first(node, _TITLE_KEYS)
    # The node's own naming first, then whatever was named above it.
    company = _first(node, _COMPANY_KEYS) or company
    if not title or not company:
        if refused is not None:
            refused["no_company"] = refused.get("no_company", 0) + 1
        return None

    job_id = _job_id(node)
    url = _first(node, _URL_KEYS)
    if job_id and source in _PREFER_POSTING_URL:
        url = _POSTING_URL[source].format(id=job_id)
    if url and url.startswith("/") and not url.startswith("//"):
        origin = _BOARD_ORIGIN.get(source)
        url = f"{origin}{url}" if origin else ""
    if not url and job_id:
        # Reconstructing beats dropping the job, for a board whose posting URL
        # we actually know. See `_POSTING_URL` for why that list is short.
        template = _POSTING_URL.get(source)
        if template:
            url = template.format(id=job_id)
    if not url:
        # Recognised as a job and thrown away. `_looks_like_job` accepts an id
        # *or* a URL and this requires a URL, so every board that identifies a
        # posting by id alone loses all of them — which is what Handshake does,
        # and what Greenhouse's aggregate board did until `publicUrl` was added
        # to `_URL_KEYS`.
        if refused is not None:
            refused["no_url"] = refused.get("no_url", 0) + 1
        return None

    board_url = _greenhouse_board_url(node)
    return {
        "source": source,
        "source_job_id": job_id or None,
        "url": url,
        # Left out rather than set to the listing URL when there is nothing
        # better: `_target_url` prefers apply_url, and pointing it back at the
        # same address would only make enrichment look like it had a choice.
        **({"apply_url": board_url} if board_url and board_url != url else {}),
        "title": title,
        "company": company,
        "location": _first(node, _LOCATION_KEYS),
        "description": _first(node, _DESCRIPTION_KEYS),
        "is_remote": _is_remote(node),
        **_annual_salary(node, source),
        **_sponsorship(node),
        # Inferred, like every source adapter does it. The insert below
        # wrote the literal "mid" and never called the parser, so a
        # harvested "Senior Backend Engineer" was filed as mid-level.
        "experience_level": parse_experience_level(
            title, _first(node, _DESCRIPTION_KEYS) or ""),
    }


# ---------------------------------------------------------------------------
# schema.org JobPosting
# ---------------------------------------------------------------------------
#
# The format Google asks every job page to embed, so nearly every board has it:
# Wellfound, Greenhouse, Lever, Workday, careers sites generally. It is a
# standard, which makes it the one payload that should never need a model to
# read — and it was falling through the walker on two counts. Its company is
# an object (`hiringOrganization.name`), and it has no URL, because it
# describes the page it sits on. So every posting was recognised and dropped,
# and a model asked to "learn" it correctly answered "there is no url field".

_LD_PERIOD = {"YEAR": 1, "MONTH": 12, "WEEK": 52, "DAY": 260, "HOUR": 2080}


def _is_job_posting(node) -> bool:
    if not isinstance(node, dict):
        return False
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).lower() == "jobposting" for k in kinds)


def _ld_location(node: dict) -> str:
    places = node.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    for place in places:
        address = (place or {}).get("address") if isinstance(place, dict) else None
        if isinstance(address, str) and address.strip():
            return address.strip()
        if isinstance(address, dict):
            parts = [_text(address.get(key)) for key in
                     ("addressLocality", "addressRegion", "addressCountry")]
            text = ", ".join(part for part in parts if part)
            if text:
                return text
    return ""


def _ld_salary(node: dict) -> dict:
    block = node.get("baseSalary")
    if not isinstance(block, dict):
        return {}
    value = block.get("value")
    value = value if isinstance(value, dict) else {"value": value}
    low = _number(value.get("minValue") if value.get("minValue") is not None else value.get("value"))
    high = _number(value.get("maxValue"))
    if low is None:
        return {}
    factor = _LD_PERIOD.get(str(value.get("unitText") or "YEAR").upper())
    if not factor:
        return {}
    low, high = low * factor, (high * factor if high is not None else None)
    if low < _MIN_PLAUSIBLE_ANNUAL:
        return {}
    return {"salary_min": low, "salary_max": high,
            "salary_currency": (_text(block.get("currency")) or "").upper()[:8] or None,
            "salary_period": "year"}


def _from_job_posting(node: dict, source: str, page_url: str = "",
                      refused: dict | None = None) -> dict | None:
    """
    A schema.org JobPosting as a job row.

    The URL is the posting's own `url` when it has one, else the page it was
    embedded in (`page_url`), which is by definition the posting's page — the
    caller passes that only when the payload holds a single posting. Failing
    both, the id rebuilt through `_POSTING_URL`.
    """
    title = _text(node.get("title"))
    org = node.get("hiringOrganization")
    company = _text(org.get("name")) if isinstance(org, dict) else _text(org)
    if not title or not company:
        if refused is not None:
            refused["no_company"] = refused.get("no_company", 0) + 1
        return None

    ident = node.get("identifier")
    job_id = _text(ident.get("value")) if isinstance(ident, dict) else _text(ident)
    url = _text(node.get("url")) or page_url
    if not url and job_id and _POSTING_URL.get(source):
        url = _POSTING_URL[source].format(id=job_id)
    if url.startswith("/") and not url.startswith("//"):
        origin = _BOARD_ORIGIN.get(source)
        url = f"{origin}{url}" if origin else ""
    if not url:
        if refused is not None:
            refused["no_url"] = refused.get("no_url", 0) + 1
        return None

    remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    location = _ld_location(node)
    if remote:
        allowed = node.get("applicantLocationRequirements")
        allowed = allowed if isinstance(allowed, list) else [allowed]
        names = [_text(a.get("name")) for a in allowed if isinstance(a, dict)]
        names = [n for n in names if n]
        location = "Remote" + (f" ({', '.join(names[:3])})" if names else "")
    description = _text(node.get("description"))
    return {
        "source": source,
        "source_job_id": job_id or None,
        "url": url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "is_remote": remote or "remote" in location.lower(),
        **_ld_salary(node),
        "experience_level": parse_experience_level(title, description or ""),
    }


def extract_jobs(payload, source: str = HARVEST_SOURCE,
                 refused: dict | None = None, page_url: str = "") -> list[dict]:
    """
    Every job-shaped object anywhere in a JSON payload.

    Deduplicated within the payload: the same posting commonly appears in both
    a card list and a detail blob in one response.

    `source` exists because this shape-based read is useful well beyond the
    browser harvest it was written for — any aggregator with an undocumented
    JSON endpoint can be read this way, and a redesign that moves the nesting
    around keeps working.

    Pass `refused` — any dict — to learn what was recognised as a job and then
    thrown out, keyed by which of `_normalize`'s two rules did it. Returning
    nothing is the most common outcome this function has, and until this it was
    indistinguishable from a payload with no jobs in it.
    """
    if not isinstance(payload, (dict, list)):
        return []

    found: dict[str, dict] = {}
    postings = [node for node in _walk(payload) if _is_job_posting(node)]
    # The page's own address stands for a posting only when the page holds
    # one; a search page listing twenty would otherwise file all twenty under
    # the same URL and merge them into one.
    own_page = page_url if len(postings) == 1 else ""
    for node in postings:
        job = _from_job_posting(node, source, page_url=own_page, refused=refused)
        if job:
            found[job["source_job_id"] or job["url"]] = job
    seen_postings = {id(node) for node in postings}

    for node, company in _walk_scoped(payload):
        if id(node) in seen_postings:
            continue
        if not _looks_like_job(node, company=company):
            continue
        job = _normalize(node, source=source, company=company, refused=refused)
        if not job:
            continue
        key = job["source_job_id"] or job["url"]
        existing = found.get(key)
        # Keep the richest copy. A card and a detail blob for the same posting
        # differ mostly in whether the description came along.
        if not existing or len(job["description"]) > len(existing["description"]):
            found[key] = job
    return list(found.values())


# The pay band and apply URL a harvested card carries used to be applied by two
# private helpers here. They said the same thing as every other "take what we
# are missing" rule in the codebase and drifted from them anyway — the fetcher's
# version forgot to check `manual_fields` — so they now live in
# `deduplication.enrich_from` with the rest, and this module calls that.


def save_harvested_jobs(db, jobs: list[dict]) -> dict:
    """
    Store harvested jobs through the same dedupe rules as fetched ones.

    Orchestration is separate from the fetch cycle's rather than shared with it,
    because the two want different things: there is no staleness filter here (a
    posting the user is looking at right now is current by definition) and no
    per-source budget. The dedupe primitives underneath are the same ones, so a
    harvested job and a fetched job still collapse into one row.
    """
    counts = {"inserted": 0, "merged": 0, "skipped": 0, "invalid": 0}
    now = datetime.now(timezone.utc)

    def _store(data, title, company, url, location, description,
               source_job_id, dedupe_hash) -> str:
        """One posting, stored or merged. Returns the outcome to count."""
        source = data.get("source") or HARVEST_SOURCE
        existing = find_existing_job(db, source, url, source_job_id, dedupe_hash)
        if existing is not None:
            improved = enrich_from(existing, data)
            # The harvested copy usually carries a fuller description than the
            # guest API managed, which is the main reason this path exists.
            if url in existing.source_urls or (
                source_job_id
                and existing.source_job_id == source_job_id
                and existing.source == source
            ):
                if merge_description(existing, description):
                    improved.append("description")
            else:
                improved += merge_or_skip(db, existing, url, description,
                                          layer=3, data=data)
            # Counted by whether the row got better, not by which branch it
            # went down. The panel calls this number "enriched".
            return "merged" if improved else "skipped"

        # Already seen, judged and retired. Same reasoning as the fetcher's
        # check: an archived posting is one we have an answer about, and
        # re-inserting it buys a scoring call to reach that same answer again.
        if was_archived(db, source, url, source_job_id, dedupe_hash):
            return "skipped"

        job = Job(
            source=source,
            source_job_id=source_job_id,
            source_urls=[url],
            title=title,
            company=company,
            location=location,
            is_remote=bool(data.get("is_remote")),
            url=url,
            apply_url=data.get("apply_url") or None,
            description=description or None,
            experience_level=data.get("experience_level"),
            status=JobStatus.new,
            fetched_at=now,
            dedupe_hash=dedupe_hash,
        )
        # The same rule a second sighting gets, on a row where every column it
        # looks at is still null. It is strictly more than the pay band this
        # used to take: a card naming an employment type or a posting date had
        # both thrown away on insert and then re-derived from prose by an LLM
        # call later.
        enrich_from(job, data)
        db.add(job)
        db.flush()
        return "inserted"

    for data in jobs:
        title = (data.get("title") or "").strip()
        company = (data.get("company") or "").strip()
        url = (data.get("url") or "").strip()
        if not (title and company and url):
            counts["invalid"] += 1
            continue

        location = (data.get("location") or "").strip()
        description = clean_description(data.get("description") or "")
        source_job_id = data.get("source_job_id")
        dedupe_hash = compute_dedupe_hash(company, title, location)

        # Savepoint + flush per job. extract_jobs dedupes on id/url, but two
        # postings with different ids can share a dedupe_hash — and without a
        # flush the second one can't see the first's pending insert, so the
        # unique constraint fired at commit and the WHOLE batch was lost.
        # Flushing makes the duplicate visible to find_existing_job; the
        # savepoint contains anything that still slips through.
        #
        # Something still does, because this is the one ingest path that runs
        # concurrently: the extension forwards a payload per response and
        # several land at once across uvicorn workers, so a posting can be
        # inserted by *another request* in the window between this one's SELECT
        # and its INSERT. That is a unique-violation on `dedupe_hash` for a job
        # neither request did anything wrong with, and it was being counted as
        # `invalid` and dropped.
        #
        # Postgres reads committed, so a second attempt sees the row the other
        # request committed and resolves it as a merge. One retry is the whole
        # fix — a second collision would mean the row is gone again, which is
        # not something retrying harder solves.
        outcome = ""
        for attempt in (1, 2):
            try:
                with db.begin_nested():
                    outcome = _store(data, title, company, url, location,
                                     description, source_job_id, dedupe_hash)
                break
            except IntegrityError:
                if attempt == 1:
                    continue
                logger.warning(
                    "harvest: %r at %s collided twice and was dropped",
                    title, company,
                )
                outcome = "invalid"
            except Exception as exc:
                logger.warning("harvest: could not store %r at %s: %s",
                               title, company, exc)
                outcome = "invalid"
                break
        counts[outcome or "invalid"] += 1

    counts["boards"] = _mine_ats_boards(db, jobs)

    db.commit()
    if counts["inserted"] or counts["merged"] or counts["boards"]:
        logger.info(
            "harvest: %d new, %d enriched, %d already known, %d new ATS board(s)",
            counts["inserted"], counts["merged"], counts["skipped"],
            counts["boards"],
        )
    return counts


def _mine_ats_boards(db, jobs: list[dict]) -> int:
    """
    Company ATS boards named by the jobs we just harvested. Returns new ones.

    This is the half of harvesting that compounds, and it was missing entirely:
    the extractor saved the jobs and threw the slugs away.

    The asymmetry is the point. A harvested posting is one job, once. A
    Greenhouse slug is that company's *entire board* — every role they have
    open and every one they open later, with full descriptions, through a free
    API, on every future fetch cycle, with no browser involved. The two are not
    the same size of prize.

    It matters most on an aggregate board like `my.greenhouse.io`, which lists
    postings across every company on the platform: one pass over it is a slug
    mine, and each slug found there is a permanent new source. But it pays on
    any page — a LinkedIn posting linking to the company's Greenhouse apply URL
    names a slug just as well.

    Nothing here validates. `company_boards` records the slug as pending and
    `validate_pending` checks it against the live API before the fetch cycle
    ever uses it, which is the right place for that: a wrong slug found here
    should cost one 404 in a validation pass, not a broken source.
    """
    try:
        from app.services.ats_discovery import discover_from_jobs
        from app.services.company_boards import record_boards

        found = discover_from_jobs(jobs)
        if not found:
            return 0
        return record_boards(db, found, origin="harvest")
    except Exception as exc:
        # A posting that was saved is saved. Failing to mine a slug out of it
        # is not a reason to lose the harvest that found it.
        logger.warning("harvest: could not mine ATS boards: %s", exc)
        return 0
