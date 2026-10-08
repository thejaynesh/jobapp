"""
One address per ATS posting, however a source wrote it.

The save path's first and surest duplicate check is "this URL is already on a
stored job" (`deduplication.find_existing_job`, layer 1). It compared URLs as
written, and every source writes them its own way:

    SimplifyJobs   https://jobs.lever.co/weride/5cde0d09-…/apply
    Lever's API    https://jobs.lever.co/weride/5cde0d09-…
    SimplifyJobs   https://jobs.ashbyhq.com/applied/a837cbd6-…/application
    Greenhouse     https://boards.greenhouse.io/andurilindustries/jobs/5215629007?gh_jid=5215629007

and the content hash behind it could not make up the difference, because the
lists rewrite titles ("New Grads 2027 - Software Engineer" → "Software Engineer
New Grad") and name companies by brand where the board adapters name them by
slug. Measured on 2026-09-28 against the same postings read both ways: 90 of
97 Lever postings and 99 of 110 Ashby postings SimplifyJobs listed were stored
twice, and 22 of 189 Greenhouse ones.

So each ATS posting URL also yields its canonical address — the posting's own
page, with nothing a source adds — and that goes into `source_urls` beside the
URL as written. Two sightings of one posting then share an entry and layer 1
joins them, through the index it already uses.

Only ATS URLs whose posting id is in the path or a known parameter. Anything
else returns None and is compared as written, as before.
"""

import re
import hashlib
from urllib.parse import unquote, urlsplit, urlunsplit

# Employer readers can authoritatively correct their own posting. Aggregator
# cards remain enrichment evidence; their shorter summaries are not revisions.
AUTHORITATIVE_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
    "workday", "icims", "bamboohr", "teamtailor", "jobvite", "personio", "oracle",
    "successfactors", "phenom", "eightfold", "jibe", "rippling", "pinpoint",
    "jazzhr", "taleo", "paylocity", "avature", "amazon", "apple", "tiktok", "usajobs",
})


def board_scope(source: str, url: str | None, board: str | None = None) -> str:
    if source not in AUTHORITATIVE_SOURCES:
        return ""
    # The hostname exists on old rows too, so discovering a board slug later
    # does not change its identity. Canonical URLs handle path-scoped tenants.
    return (urlsplit(url or "").hostname or board or "").lower()


def listing_key(source: str, url: str, external_id=None, board=None) -> str:
    scope = board_scope(source, url, board)
    identity = str(external_id) if external_id not in (None, "") else canonical(url) or url
    return hashlib.sha256(f"{source}|{scope}|{identity}".encode()).hexdigest()


def job_key(source: str, url: str, external_id=None, apply_url=None, board=None) -> str:
    address = canonical(apply_url) or canonical(url)
    if address:
        return hashlib.sha256(f"canonical|{address}".encode()).hexdigest()
    if external_id not in (None, ""):
        return listing_key(source, url, external_id, board)
    parsed = urlsplit(url)
    address = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))
    return hashlib.sha256(f"url|{address}".encode()).hexdigest()


def conflicts(job, source: str, url: str, external_id=None, apply_url=None) -> bool:
    """Do not perpetuate aliases created by the old title-hash merge rule."""
    if source == job.source and external_id and job.source_job_id:
        if str(external_id) == str(job.source_job_id):
            return False
        if url == job.url:
            # An adapter may have begun namespacing a formerly local ID.
            return False
        incoming, original = canonical(url), canonical(job.url)
        return not (incoming and incoming == original)
    incoming = canonical(apply_url) or canonical(url)
    original = canonical(job.apply_url) or canonical(job.url)
    return bool(incoming and original and incoming != original)

_RULES: list[tuple[re.Pattern, "callable"]] = [
    # Greenhouse ids are global, so every form of link to one posting —
    # the board, the job-boards host, the embed, the API, or an employer's
    # own careers page carrying `gh_jid` — comes down to the embed address.
    (re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed/)[^/?#\s]+/jobs/(\d+)", re.I),
     lambda m: f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"),
    (re.compile(r"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/[^/?#\s]+/jobs/(\d+)", re.I),
     lambda m: f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"),
    (re.compile(r"greenhouse\.io/embed/job_app\?(?:[^#\s]*&)?token=(\d+)", re.I),
     lambda m: f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"),
    (re.compile(r"[?&]gh_jid=(\d+)", re.I),
     lambda m: f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"),
    (re.compile(r"jobs\.((?:eu\.)?)lever\.co/([^/?#\s]+)/([0-9a-f-]{36})", re.I),
     lambda m: f"https://jobs.{m.group(1).lower()}lever.co/{_slug(m.group(2))}/{m.group(3).lower()}"),
    (re.compile(r"jobs\.ashbyhq\.com/([^/?#\s]+)/([0-9a-f-]{36})", re.I),
     lambda m: f"https://jobs.ashbyhq.com/{_slug(m.group(1))}/{m.group(2).lower()}"),
    (re.compile(r"jobs\.smartrecruiters\.com/([^/?#\s]+)/(\d+)", re.I),
     lambda m: f"https://jobs.smartrecruiters.com/{_slug(m.group(1))}/{m.group(2)}"),
    (re.compile(r"apply\.workable\.com/([^/?#\s]+)/j/([0-9A-Za-z]+)", re.I),
     lambda m: f"https://apply.workable.com/{_slug(m.group(1))}/j/{m.group(2).upper()}"),
    # Workday: the locale segment and the apply step are the source's, the
    # site and the requisition path are the posting's.
    (re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                r"([A-Za-z0-9_-]+)/job/([^?#\s]+?)(?:/apply(?:/[^?#\s]*)?)?/?(?:[?#]|\s|$)", re.I),
     lambda m: (f"https://{m.group(1).lower()}.{m.group(2).lower()}.myworkdayjobs.com/"
                f"{m.group(3)}/job/{m.group(4)}")),
    (re.compile(r"jobs\.apple\.com/[a-z]{2}-[a-z]{2}/details/(\d+)", re.I),
     lambda m: f"https://jobs.apple.com/en-us/details/{m.group(1)}"),
    (re.compile(r"(?:lifeattiktok\.com/search|careers\.tiktok\.com/position)/(\d+)", re.I),
     lambda m: f"https://lifeattiktok.com/search/{m.group(1)}"),
    (re.compile(r"https?://([a-z0-9-]+\.icims\.com)/jobs/(\d+)", re.I),
     lambda m: f"https://{m.group(1).lower()}/jobs/{m.group(2)}/job"),
    # Avature, on its own hosts and employers' alike; a posting opens by id alone.
    (re.compile(r"https?://([a-z0-9.-]+)/(?:[a-z]{2}_[A-Z]{2}/)?([A-Za-z0-9_-]+)/JobDetail/"
                r"(?:[^/?#\s]+/)?(\d+)(?:[/?#]|\s|$)", re.I),
     lambda m: f"https://{m.group(1).lower()}/{m.group(2)}/JobDetail/{m.group(3)}"),
]


def _slug(text: str) -> str:
    return unquote(text).strip().lower()


def canonical(url: str | None) -> str | None:
    """The posting's canonical address, or None for a URL this can't place."""
    if not url:
        return None
    for pattern, build in _RULES:
        match = pattern.search(url)
        if match:
            return build(match)
    return None


def urls(url: str | None, apply_url: str | None = None) -> list[str]:
    """
    The addresses to record a posting under, and to look it up by: its URL as
    written, then the canonical address of it and of its apply link.

    The apply link only by its canonical address. As written it can be a
    careers page every posting at a company shares, and matching on that
    would fold different jobs into one.
    """
    out: list[str] = []
    for candidate in (url, canonical(url), canonical(apply_url)):
        if candidate and candidate not in out:
            out.append(candidate)
    return out
