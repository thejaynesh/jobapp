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
    "compensationBreakdown",
)
_SALARY_MIN_KEYS = ("minSalary", "min", "minValue", "minAmount", "from")
_SALARY_MAX_KEYS = ("maxSalary", "max", "maxValue", "maxAmount", "to")
_CURRENCY_KEYS = ("currencyCode", "currency", "currencyIso")

# LinkedIn ids arrive as bare numbers or wrapped in an urn.
_URN_ID_RE = re.compile(r"(\d{6,})")

# A payload nests deeply; without a ceiling a cyclic or pathological structure
# would walk forever.
_MAX_DEPTH = 12
_MAX_NODES = 20000


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


def _first_number(node: dict, keys: tuple) -> float | None:
    if not isinstance(node, dict):
        return None
    for key in keys:
        if key in node:
            found = _number(node[key])
            if found is not None:
                return found
    return None


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


def _looks_like_job(node: dict) -> bool:
    """
    A title and a company, plus something to identify it by.

    All three are required together on purpose. A title alone matches every
    heading in the payload, and a company alone matches the sidebar.
    """
    if not isinstance(node, dict):
        return False
    if not _first(node, _TITLE_KEYS):
        return False
    if not _first(node, _COMPANY_KEYS):
        return False
    return bool(_job_id(node) or _first(node, _URL_KEYS))


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


def _normalize(node: dict, source: str = HARVEST_SOURCE) -> dict | None:
    title = _first(node, _TITLE_KEYS)
    company = _first(node, _COMPANY_KEYS)
    if not title or not company:
        return None

    job_id = _job_id(node)
    url = _first(node, _URL_KEYS)
    if not url and job_id and source == HARVEST_SOURCE:
        # Reconstructing beats dropping the job: this URL shape has been stable
        # for years and is what the site itself links to. Only for LinkedIn —
        # no other source's ids belong in a linkedin.com URL.
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    if not url:
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
        **_salary(node),
    }


def extract_jobs(payload, source: str = HARVEST_SOURCE) -> list[dict]:
    """
    Every job-shaped object anywhere in a JSON payload.

    Deduplicated within the payload: the same posting commonly appears in both
    a card list and a detail blob in one response.

    `source` exists because this shape-based read is useful well beyond the
    browser harvest it was written for — any aggregator with an undocumented
    JSON endpoint can be read this way, and a redesign that moves the nesting
    around keeps working.
    """
    if not isinstance(payload, (dict, list)):
        return []

    found: dict[str, dict] = {}
    for node in _walk(payload):
        if not _looks_like_job(node):
            continue
        job = _normalize(node, source=source)
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
        try:
            with db.begin_nested():
                source = data.get("source") or HARVEST_SOURCE
                existing = find_existing_job(
                    db, source, url, source_job_id, dedupe_hash
                )
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

                    # Counted by whether the row got better, not by which branch
                    # it went down. The panel calls this number "enriched".
                    counts["merged" if improved else "skipped"] += 1
                    continue

                # Already seen, judged and retired. Same reasoning as the
                # fetcher's check: an archived posting is one we have an answer
                # about, and re-inserting it buys a scoring call to reach that
                # same answer again.
                if was_archived(db, source, url, source_job_id, dedupe_hash):
                    counts["skipped"] += 1
                    continue

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
                    experience_level="mid",
                    status=JobStatus.new,
                    fetched_at=now,
                    dedupe_hash=dedupe_hash,
                )
                # The same rule a second sighting gets, on a row where every
                # column it looks at is still null. It is strictly more than the
                # pay band this used to take: a card naming an employment type
                # or a posting date had both thrown away on insert and then
                # re-derived from prose by an LLM call later.
                enrich_from(job, data)
                db.add(job)
                db.flush()
                counts["inserted"] += 1
        except Exception as exc:
            logger.warning("harvest: could not store %r at %s: %s", title, company, exc)
            counts["invalid"] += 1

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
