"""
ATS company-slug auto-discovery.

Jobs fetched from aggregators (LinkedIn, JSearch, HN, The Muse, ...) frequently
link to the company's own ATS board (boards.greenhouse.io/<slug>, jobs.lever.co/
<slug>, ...). Those boards are the best sources we have — full descriptions,
direct apply links, no scraping — so every fetch cycle scans the fetched jobs'
URLs and descriptions for ATS links, persists the discovered slugs on the
profile, and feeds them into the next cycle's direct board fetches.
"""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

# Default cap on auto-discovered slugs per ATS. Cheap boards (one request per
# company) can carry many; per-company-expensive ATSes are capped lower below.
MAX_SLUGS_PER_ATS = 100
DISCOVERY_CAPS = {
    "workday": 15,          # searches × per-job detail calls per tenant
    "smartrecruiters": 30,  # per-posting detail calls per company
    "bamboohr": 30,
    "icims": 25,
    "teamtailor": 50,
    "jobvite": 50,
}


def _discovery_cap(ats: str) -> int:
    return DISCOVERY_CAPS.get(ats, MAX_SLUGS_PER_ATS)

# Several shapes per ATS: the public board URL people link to, the embed widget
# a company drops into its own careers page, and the API endpoint that widget
# calls. Careers pages very often only ever reveal the latter two.
ATS_PATTERNS: dict[str, list[re.Pattern]] = {
    "greenhouse": [
        # Embed widget: boards.greenhouse.io/embed/job_board?for=<slug>
        re.compile(r"greenhouse\.io/embed/job_board[^\"'\s]*[?&]for=([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"greenhouse\.io/(?:v1/)?boards/([A-Za-z0-9_-]{2,})", re.I),
        # EU-hosted boards (job-boards.eu.greenhouse.io) are served by the same
        # API as every other, so they need nothing more than to be recognised.
        re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]{2,})", re.I),
    ],
    "lever": [
        # jobs.eu.lever.co boards live only on api.eu.lever.co; the adapter
        # falls back to it when the US API has never heard of a slug.
        re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"api\.(?:eu\.)?lever\.co/v0/postings/([A-Za-z0-9_-]{2,})", re.I),
    ],
    "ashby": [
        re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-]{2,})", re.I),
        re.compile(r"ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.\-]{2,})", re.I),
    ],
    "smartrecruiters": [
        re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]{2,})", re.I),
    ],
    "workable": [
        re.compile(r"apply\.workable\.com/(?:api/)?([A-Za-z0-9-]{2,})", re.I),
    ],
    "recruitee": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.recruitee\.com", re.I),
    ],
    "icims": [
        # Both host shapes in the wild, normalized to the bare company slug.
        # The second pattern refuses the `careers-` prefix explicitly: without
        # that it also matches `careers-globex.icims.com` and registers
        # "careers-globex" as a second, duplicate board for the same company.
        re.compile(r"https?://careers-([A-Za-z0-9-]{2,})\.icims\.com", re.I),
        re.compile(r"https?://(?!careers-)([A-Za-z0-9-]{2,})\.icims\.com", re.I),
    ],
    "bamboohr": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.bamboohr\.com", re.I),
    ],
    "teamtailor": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.teamtailor\.com", re.I),
    ],
    "jobvite": [
        # jobs.jobvite.com/<slug> only. click.jobvite.com is the click tracker
        # (see link_resolver._TRACKER_DOMAINS), and reading a slug out of one
        # would register the tracker itself as a company board.
        re.compile(r"jobs\.jobvite\.com/(?:careers/)?([A-Za-z0-9_-]{2,})", re.I),
    ],
    "rippling": [
        # ats.rippling.com/<slug>/jobs/<id>, past any locale segment (/en-US/).
        re.compile(r"ats\.rippling\.com/(?:api/v2/board/)?(?:[a-z]{2}-[A-Za-z]{2}/)?"
                   r"([A-Za-z0-9_-]{2,})(?:/|$)", re.I),
    ],
    "pinpoint": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.pinpointhq\.com", re.I),
    ],
    "personio": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.jobs\.personio\.(?:de|com)", re.I),
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.jobs\.personio-int\.com", re.I),
    ],
}

# Workday boards need a tenant:host:site triple, extracted from URLs like
# https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/...
# or, for the tenants Workday serves from its shared host,
# https://wd5.myworkdaysite.com/en-US/recruiting/microchiphr/External/job/...
# — the same tenant answers on microchiphr.wd5.myworkdayjobs.com, so both
# shapes give the spec the adapter already reads.
_WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]{2,})\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    re.I,
)
_WORKDAY_SITE_RE = re.compile(
    r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/"
    r"([a-z0-9-]{2,})/([A-Za-z0-9_-]+)",
    re.I,
)
# Paths on a Workday host that are not career sites. The site is judged
# against these and nothing else: "careers", "search" and "External" are
# ordinary site names (theocc:wd5:careers, expedia:wd108:search), and the
# general slug blocklist, written for vendor slugs, was throwing them away.
# Nor is there a length rule: Citi's main site, 2,000 postings, is "2".
_WORKDAY_NOT_SITES = frozenset({"wday", "login", "robots", "userhome", "recruiting"})

# ATSes whose board is a careers *host* — often the employer's own domain —
# rather than a slug on the vendor's. Each pattern reads one posting link and
# returns the board spec its adapter takes. They are deliberately specific to
# the URL shape each platform emits, because the host alone says nothing; and
# a host that is a job board rather than an employer is refused whatever the
# path looks like (`_employer_host`). Every board found still has to pass its
# validation probe before it is polled.
_HOST_BOARD_PATTERNS: list[tuple[str, re.Pattern, "callable"]] = [
    # …oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26007181
    ("oracle", re.compile(
        r"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com)"
        r"/hcmUI/CandidateExperience/[A-Za-z_-]+/sites/([A-Za-z0-9_]+)", re.I),
     lambda m: f"{m.group(1).lower()}:{m.group(2)}"),
    # SuccessFactors Career Site Builder: /job/<Title-Slug>/<9-10 digit id>/
    ("successfactors", re.compile(
        r"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)+)/job/[^/\s\"'<>?#]+/\d{6,12}/", re.I),
     lambda m: m.group(1).lower()),
    # Phenom: /us/en/job/R-275650/…
    ("phenom", re.compile(
        r"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([a-z]{2})/([a-z]{2})/job/[A-Za-z0-9_-]+",
        re.I),
     lambda m: f"{m.group(1).lower()}/{m.group(2).lower()}/{m.group(3).lower()}"),
    # iCIMS careers-home ("Jibe") sites: /careers-home/jobs/<id>, or /jobs/<id>
    # carrying the `icims` marker SimplifyJobs adds to them.
    ("jibe", re.compile(
        r"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)+)/careers-home/jobs/\d+", re.I),
     lambda m: m.group(1).lower()),
    ("jibe", re.compile(
        r"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)+)/jobs/\d+/?\?(?:[^\s\"'<>]*&)?icims=1", re.I),
     lambda m: m.group(1).lower()),
    # …and iCIMS's own host for them, which needs no marker: dish.jibeapply.com
    ("jibe", re.compile(r"https?://([a-z0-9-]+\.jibeapply\.com)/jobs/\d+", re.I),
     lambda m: m.group(1).lower()),
    # Eightfold: {company}.eightfold.ai, or a custom host's /careers/job/<long id>
    ("eightfold", re.compile(r"https?://([a-z0-9-]+\.eightfold\.ai)/careers", re.I),
     lambda m: m.group(1).lower()),
    ("eightfold", re.compile(
        r"https?://([a-z0-9-]+(?:\.[a-z0-9-]+)+)/careers/job/\d{10,}", re.I),
     lambda m: m.group(1).lower()),
]


def _employer_host(host: str) -> bool:
    """False for a job board or search engine that merely links to postings."""
    from app.services.company_domain import AGGREGATOR_HOSTS

    host = host.lower().split(":", 1)[0]
    return not any(host == d or host.endswith(f".{d}") for d in AGGREGATOR_HOSTS)


# All ATS kinds we can fetch directly (patterned single-slug ones plus workday
# and the host-based ones).
ALL_ATS = frozenset(ATS_PATTERNS) | {"workday"} | {a for a, _, _ in _HOST_BOARD_PATTERNS}

# Things that match the URL patterns but are not a company board.
#
# Two kinds, and the second is the one that cost us. Structural path segments
# ("embed", "api") were always here. The job boards and trackers were not, so
# `greenhouse/linkedin`, `greenhouse/appcast` and `greenhouse/stepstone` all
# got registered as companies and polled every cycle for months — a slug
# scraped off an aggregator's own page, which was never a company to begin
# with.
SLUG_BLOCKLIST = frozenset({
    # Structural
    "embed", "api", "static", "assets", "www", "app", "jobs", "j", "widget",
    "careers", "share", "hire", "docs", "help", "blog", "wday", "job", "login",
    "search", "apply", "posting", "postings", "board", "boards", "company",
    "companies", "index", "home", "about", "contact", "support", "status",
    "cdn", "media", "images", "img", "signup", "signin", "auth", "account",
    # Aggregators, job boards and click trackers — never employers
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "simplyhired", "monster",
    "appcast", "recruitics", "stepstone", "justjoin", "justjoinit", "adzuna",
    "jooble", "careerjet", "talent", "neuvoo", "dice", "wellfound", "angellist",
    "remoteok", "weworkremotely", "themuse", "himalayas", "jobicy", "arbeitnow",
    "remotive", "hiringcafe", "workatastartup", "ycombinator", "levels",
    "builtin", "otta", "welcometothejungle", "totaljobs", "reed", "seek",
})

# Kept under the old private name so nothing that imported it breaks.
_SLUG_BLOCKLIST = SLUG_BLOCKLIST


def _extract_slugs(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if not text:
        return found
    for ats, patterns in ATS_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                slug = match.group(1).lower().rstrip(".")
                if slug and slug not in _SLUG_BLOCKLIST:
                    found.setdefault(ats, set()).add(slug)
    workday = [(m.group(1), m.group(2), m.group(3)) for m in _WORKDAY_RE.finditer(text)]
    workday += [(m.group(2), m.group(1), m.group(3)) for m in _WORKDAY_SITE_RE.finditer(text)]
    for tenant, host, site in workday:
        tenant, host = tenant.lower(), host.lower()
        if tenant not in _SLUG_BLOCKLIST and site.lower() not in _WORKDAY_NOT_SITES:
            found.setdefault("workday", set()).add(f"{tenant}:{host}:{site}")
    for ats, pattern, spec_of in _HOST_BOARD_PATTERNS:
        for match in pattern.finditer(text):
            spec = spec_of(match)
            if _employer_host(spec.split("/", 1)[0]):
                found.setdefault(ats, set()).add(spec)
    return found


def extract_slugs(text: str) -> dict[str, set[str]]:
    """Public wrapper: every ATS company slug referenced anywhere in `text`."""
    return _extract_slugs(text)


def discover_from_jobs(raw_jobs: list[dict]) -> dict[str, set[str]]:
    """
    ATS slugs referenced by a batch of fetched jobs, uncapped and unmerged —
    for callers that persist boards themselves (see services.company_boards).
    Scans each job's listing URL, its resolved apply URL, and its description.
    """
    found: dict[str, set[str]] = {}
    for job in raw_jobs:
        # A job fetched from an ATS shouldn't rediscover its own board — but it
        # can name another one. A Phenom site's postings apply through the
        # Workday or SuccessFactors board behind it, and that board is worth
        # polling directly.
        own = job.get("source") if job.get("source") in ALL_ATS else None
        text = "\n".join(filter(None, (
            job.get("url"), job.get("apply_url"),
            job.get("description") if own is None else None,
        )))
        for ats, slugs in _extract_slugs(text).items():
            if ats != own:
                found.setdefault(ats, set()).update(slugs)
    return found


def discover_ats_slugs(raw_jobs: list[dict], existing: dict | None = None) -> dict[str, list[str]]:
    """
    Scan fetched jobs for ATS board links and merge newly found company slugs
    into the existing mapping. Returns {"greenhouse": [...], "lever": [...], ...}
    with per-ATS caps (newest discoveries are dropped first when full).
    """
    merged: dict[str, list[str]] = {
        ats: list(slugs or []) for ats, slugs in (existing or {}).items()
        if ats in ALL_ATS
    }

    new_count = 0
    for job in raw_jobs:
        # A job already fetched from an ATS shouldn't rediscover itself.
        if job.get("source") in ALL_ATS:
            continue
        text = "\n".join(filter(None, (
            job.get("url"), job.get("apply_url"), job.get("description"),
        )))
        new_count += _merge_found(merged, _extract_slugs(text))

    if new_count:
        logger.info(
            "ats_discovery: %d new company slugs — %s",
            new_count,
            {ats: len(slugs) for ats, slugs in merged.items()},
        )
    return merged


def _merge_found(merged: dict[str, list[str]], found: dict[str, set[str]]) -> int:
    added = 0
    for ats, slugs in found.items():
        bucket = merged.setdefault(ats, [])
        cap = _discovery_cap(ats)
        for slug in sorted(slugs):
            if slug not in bucket and len(bucket) < cap:
                bucket.append(slug)
                added += 1
    return added


def harvest_slugs_from_lists(urls: list[str], existing: dict | None = None) -> dict[str, list[str]]:
    """
    Pull ATS company slugs out of community-maintained job lists (e.g. the
    SimplifyJobs new-grad README) — each list is one document full of direct
    apply links pointing at Greenhouse/Lever/Ashby/Workday/... boards.
    Returns the existing mapping merged with everything harvested (capped).
    """
    merged: dict[str, list[str]] = {
        ats: list(slugs or []) for ats, slugs in (existing or {}).items()
        if ats in ALL_ATS
    }
    for url in urls:
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("slug harvest failed for %s: %s", url, exc)
            continue
        added = _merge_found(merged, _extract_slugs(resp.text))
        logger.info("slug harvest: %d new slugs from %s", added, url)
    return merged


def _note_career_link(career_links: dict, link: str, company: str) -> None:
    from app.services.ats_sniffer import company_host

    host = company_host(link)
    if not host or not _employer_host(host):
        return
    prior = career_links.get(host)
    # A later row is a newer posting, likelier still open for the sniffer to
    # confirm; a Greenhouse ID beats none at all.
    if prior is None or "gh_jid=" in link or "gh_jid=" not in prior["url"]:
        career_links[host] = {"url": link, "company": company}


def harvest_boards_from_lists(
    urls: list[str],
    career_links: dict[str, dict] | None = None,
) -> tuple[dict[str, set[str]], dict[tuple[str, str], str]]:
    """
    Every ATS board named by a set of community lists, uncapped, with names.

    Returns `(found, names)`: `{ats: {slug}}`, and the company each board was
    listed under where the list says, keyed by `(ats, slug)`.

    Uncapped because the caller is the board registry, which validates each
    board before polling it and ranks them by yield afterwards. The capped
    `harvest_slugs_from_lists` fed the profile blob that predates the
    registry, where a cap was the only thing keeping the list short — and
    with it, one list's worth of Workday tenants was fifteen, forever.

    A `.json` URL is read as a SimplifyJobs listings file, row by row, which
    is where the company name comes from; anything else is read as text.

    `career_links`, when given, collects the rows no pattern recognised that
    sit on an employer's own site: `{host: {"url", "company"}}`, one posting
    per host, one carrying a Greenhouse `gh_jid` when there is one. Those are
    for `ats_sniffer`, which can often find the board behind them.
    """
    found: dict[str, set[str]] = {}
    names: dict[tuple[str, str], str] = {}
    for url in urls:
        try:
            if url.lower().split("?", 1)[0].endswith(".json"):
                from app.services.sources.simplify import rows

                before = sum(len(v) for v in found.values())
                for row in rows(url):
                    company = str(row.get("company_name") or "").strip()
                    link = str(row.get("url") or "")
                    extracted = _extract_slugs(link)
                    for ats, slugs in extracted.items():
                        found.setdefault(ats, set()).update(slugs)
                        if company:
                            for slug in slugs:
                                names.setdefault((ats, slug), company)
                    if career_links is not None and not extracted:
                        _note_career_link(career_links, link, company)
                added = sum(len(v) for v in found.values()) - before
            else:
                resp = httpx.get(url, timeout=30, follow_redirects=True)
                resp.raise_for_status()
                before = sum(len(v) for v in found.values())
                for ats, slugs in _extract_slugs(resp.text).items():
                    found.setdefault(ats, set()).update(slugs)
                added = sum(len(v) for v in found.values()) - before
        except Exception as exc:
            logger.warning("board harvest failed for %s: %s", url, exc)
            continue
        logger.info("board harvest: %d boards from %s", added, url)
    return found, names


def merged_slugs(configured_csv: str, discovered: dict | None, ats: str) -> list[str]:
    """Configured (env) slugs first, then discovered ones, deduplicated."""
    result: list[str] = []
    seen: set[str] = set()
    for slug in [s.strip() for s in (configured_csv or "").split(",")]:
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            result.append(slug)
    for slug in (discovered or {}).get(ats, []) or []:
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            result.append(slug)
    return result


# Which settings field carries each ATS's configured slugs.
ATS_CONFIG_FIELDS = {
    "greenhouse": "GREENHOUSE_COMPANY_SLUGS",
    "lever": "LEVER_COMPANY_SLUGS",
    "ashby": "ASHBY_COMPANY_SLUGS",
    "smartrecruiters": "SMARTRECRUITERS_COMPANY_SLUGS",
    "workable": "WORKABLE_COMPANY_SLUGS",
    "recruitee": "RECRUITEE_COMPANY_SLUGS",
    "workday": "WORKDAY_TENANTS",
    "icims": "ICIMS_COMPANY_SLUGS",
    "bamboohr": "BAMBOOHR_COMPANY_SLUGS",
    "teamtailor": "TEAMTAILOR_COMPANY_SLUGS",
    "jobvite": "JOBVITE_COMPANY_SLUGS",
    "personio": "PERSONIO_COMPANY_SLUGS",
    "oracle": "ORACLE_BOARDS",
    "successfactors": "SUCCESSFACTORS_BOARDS",
    "phenom": "PHENOM_BOARDS",
    "eightfold": "EIGHTFOLD_BOARDS",
    "jibe": "JIBE_BOARDS",
    "rippling": "RIPPLING_COMPANY_SLUGS",
    "pinpoint": "PINPOINT_COMPANY_SLUGS",
}

# Bound per-cycle fetch time: cheap one-request-per-company boards can carry
# many slugs; per-company-expensive ATSes get tighter totals. Board fetches run
# concurrently (see sources.base.fetch_boards_concurrently), so these are far
# more generous than when each slug cost a serial round trip.
MAX_TOTAL_SLUGS_PER_ATS = 300
TOTAL_SLUG_CAPS = {
    "smartrecruiters": 80,  # per-posting detail calls per company
    "bamboohr": 80,         # per-posting detail calls per company
    # Two host shapes tried per slug, and a full HTML page parsed each time.
    "icims": 60,
    "teamtailor": 120,
    "jobvite": 120,
    # Large employers: a few list requests, then up to 20 descriptions (Oracle);
    # one feed of up to tens of megabytes (SuccessFactors); a search per role at
    # 10–50 a page, then up to 15 descriptions (Eightfold, Phenom).
    "oracle": 150,
    "successfactors": 80,
    "phenom": 80,
    "eightfold": 60,
    # A five-second crawl delay per site, so fewer sites a cycle.
    "jibe": 40,
}

# Caps that are a setting of their own rather than a constant here. Workday was
# 30 tenants a cycle — for the ATS behind 27% of US new-grad postings and
# 30–38% of large employers, with ~1,100 registered tenants never polled. It is
# on the settings page now, next to the concurrency that makes it affordable.
_CAP_SETTINGS = {"workday": ("WORKDAY_MAX_TENANTS", 150)}


def _total_cap(ats: str, cfg=None) -> int:
    """This ATS's per-cycle board budget, from `cfg` (the cycle's settings)."""
    if cfg is None:
        from app.config import settings as cfg

    default = int(getattr(cfg, "ATS_MAX_SLUGS_PER_ATS", MAX_TOTAL_SLUGS_PER_ATS))
    setting = _CAP_SETTINGS.get(ats)
    if setting:
        return max(0, int(getattr(cfg, setting[0], setting[1])))
    capped = TOTAL_SLUG_CAPS.get(ats)
    return min(capped, default) if capped is not None else default


def slug_caps(cfg=None) -> dict[str, int]:
    """The per-cycle slug budget for each ATS."""
    return {ats: _total_cap(ats, cfg) for ats in ATS_CONFIG_FIELDS}


def configured_ats_slugs(cfg) -> dict[str, list[str]]:
    """The raw configured slugs per ATS from settings."""
    result = {}
    for ats, field in ATS_CONFIG_FIELDS.items():
        result[ats] = [
            s.strip() for s in (getattr(cfg, field, "") or "").split(",") if s.strip()
        ]
    return result


def build_ats_slugs(
    cfg,
    discovered: dict | None = None,
    validated_configured: dict | None = None,
    registry: dict | None = None,
) -> dict[str, list[str]]:
    """
    Assemble the final slug list per ATS for one fetch cycle:
    Configured boards take priority. An available registry owns all other
    selection, including seeds already imported there. Without a registry,
    fall back to seed and legacy discovery lists. Deduplicated and capped.
    """
    from app.services.ats_seeds import SEED_ATS_SLUGS

    configured = (
        validated_configured if validated_configured is not None
        else configured_ats_slugs(cfg)
    )
    use_seeds = getattr(cfg, "ATS_SEED_COMPANIES", True)

    result: dict[str, list[str]] = {}
    for ats in ATS_CONFIG_FIELDS:
        cap = _total_cap(ats, cfg)
        seen: set[str] = set()
        merged: list[str] = []
        if registry is not None:
            # Empty is authoritative too: appending legacy discoveries would
            # re-poll rejected/retired boards, and prepending seeds would pin
            # the same Workday companies into every slot forever.
            layers = [configured.get(ats, []), registry.get(ats, []) or []]
        else:
            layers = [
                configured.get(ats, []),
                SEED_ATS_SLUGS.get(ats, []) if use_seeds else [],
                (discovered or {}).get(ats, []) or [],
            ]
        for layer in layers:
            for slug in layer:
                if slug and slug.lower() not in seen and len(merged) < cap:
                    seen.add(slug.lower())
                    merged.append(slug)
        result[ats] = merged
    return result
