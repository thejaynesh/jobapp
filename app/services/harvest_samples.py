"""
Keeping a payload we could not read, so that it can be read later.

The harvest reader is shape-based: it walks a response looking for anything
with a title, a company and an identifier. That is why a site nobody wrote a
parser for usually works on the first try — and when it does not, the payload
was thrown away in the same breath. "Forwarding, never finds jobs" was a
verdict with no evidence attached, and the only way to act on it was to open
DevTools yourself and go looking.

This keeps the evidence. Bounded, because the point is diagnosis and not
archival:

* **Truncated.** A payload can be three megabytes; a handful of job objects is
  enough to see how the site names its fields. Trimming is by structure rather
  than by string length, so what is kept is still valid JSON.
* **Capped per host.** A site failing every request would otherwise write a
  sample per response, all of them saying the same thing.
* **Expired.** A sample is worth having until the recipe is written.

And one rule that is about what these actually are: **they are responses to a
logged-in session.** They can carry the user's own name, their account id, the
name of whoever posted a job. That is the user's own data on the user's own
machine, which is why keeping it at all is reasonable — but it is also why it
is trimmed hard, capped, and expired rather than accumulated.
"""

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from app.config import live

logger = logging.getLogger(__name__)

# How much of one payload to keep. Enough to see the shape and a few real
# values; far short of the whole response.
MAX_SAMPLE_BYTES = 60_000

# How many items to keep from any one array. A search response holds twenty-five
# identical-shaped cards, and three of them teach exactly as much as all of
# them do.
MAX_ARRAY_ITEMS = 4

# How deep to descend. Deeper than the reader's own walk, since the thing being
# diagnosed is often that the interesting object is deeper than expected.
MAX_DEPTH = 14


def _keep() -> int:
    return max(1, int(getattr(live(), "HARVEST_SAMPLES_PER_HOST", 5)))


def _ttl_days() -> int:
    return max(1, int(getattr(live(), "HARVEST_SAMPLE_TTL_DAYS", 30)))


def endpoint_key(url: str) -> str:
    """Stable response route, without searches, tokens or page counters."""
    try:
        parsed = urlsplit(url or "")
        if not parsed.hostname:
            return ""
        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{20,}(?=/|$)|/\d+(?=/|$)", "/:id", parsed.path, flags=re.I)
        operation = parse_qs(parsed.query).get("operationName", [""])[0]
        suffix = "?operation=" + operation if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", operation) else ""
        return ((path or "/").rstrip("/") or "/")[:210] + suffix
    except ValueError:
        return ""


def fingerprint(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def shape_hash(payload) -> str:
    def shape(node, depth=0):
        if depth > MAX_DEPTH:
            return "deep"
        if isinstance(node, dict):
            return {str(k): shape(v, depth + 1) for k, v in sorted(node.items(), key=lambda p: str(p[0]))[:60]}
        if isinstance(node, list):
            return sorted({json.dumps(shape(v, depth + 1), sort_keys=True) for v in node[:MAX_ARRAY_ITEMS]})
        return type(node).__name__
    return fingerprint(shape(payload))


def sample_endpoint(sample) -> str:
    return sample.endpoint_key or endpoint_key(sample.source_url or "")


_REFERENCE_KEY = re.compile(r"(?:^id$|id$|urn$|ref$|reference$)", re.I)


def _references(value) -> set[str]:
    """Identifiers retained job examples may need to join to company records."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (str, int)) and (_REFERENCE_KEY.search(str(key)) or str(item).startswith("urn:")):
                found.add(str(item))
            elif isinstance(item, (dict, list)):
                found.update(_references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_references(item))
    return found


def trim(value, depth: int = 0, references: set[str] | None = None):
    """
    A structurally smaller copy of `value`, still valid JSON.

    Arrays are cut to the first few items rather than dropped: the shape of a
    list of jobs is the single most useful thing in a payload, and a length is
    not a shape. Strings are truncated because a description can be the whole
    response and teaches nothing about field names.
    """
    if depth > MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {str(k)[:80]: trim(v, depth + 1, references) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        selected = value[:MAX_ARRAY_ITEMS]
        if references:
            # A normalized company table need not list the retained jobs'
            # employers first. Keep matching identities as bounded extras.
            for item in value[MAX_ARRAY_ITEMS:]:
                if len(selected) >= MAX_ARRAY_ITEMS * 2:
                    break
                if isinstance(item, dict) and any(
                    _REFERENCE_KEY.search(str(key)) and isinstance(ident, (str, int)) and str(ident) in references
                    for key, ident in item.items()
                ):
                    selected.append(item)
        return [trim(item, depth + 1, references) for item in selected]
    if isinstance(value, str):
        return value[:600]
    return value


def _fits(payload) -> dict | list:
    """Trim until it is under the byte cap, or give up and keep the shape."""
    trimmed = trim(payload)
    for _ in range(2):
        references = _references(trimmed)
        if not references:
            break
        trimmed = trim(payload, references=references)
    try:
        if len(json.dumps(trimmed)) <= MAX_SAMPLE_BYTES:
            return trimmed
    except (TypeError, ValueError):
        return {"error": "payload was not serialisable"}

    # Still too big: keep only the top level's keys and their types, which is
    # enough to say where to look next.
    if isinstance(trimmed, dict):
        return {
            key: (type(value).__name__ if not isinstance(value, str) else value[:120])
            for key, value in list(trimmed.items())[:60]
        }
    return {"error": "payload too large to sample", "items": len(trimmed or [])}


def record(db, host: str, payload, *, source_url: str = "", found: int = 0,
           note: str = "", probe: bool = False, page_url: str = "") -> bool:
    """
    Keep bounded, distinct evidence across response endpoints and shapes.

    Displacing, and that is the change that matters. This used to refuse
    outright once a host held five, which made the *first five payloads a host
    ever sent* the only five it would ever be judged on — and the first few
    responses on a modern job board are its analytics, its feature flags and
    its session config, because those are what a page fetches before it fetches
    any jobs.

    The evidence store proved it: an ad-tech tag of 121 bytes, an analytics
    config, a status page of 17 bytes, a user account record. Sixteen attempts
    to learn a recipe for Handshake all reported "found no jobs in any sample",
    which was true and was about the samples rather than the board.

    Ranked by usefulness, and size is only the last word.

    A *forward* is a payload that named job fields and still could not be read
    — the exact thing a recipe is written from. A *probe* named none of them
    and is a guess. So a forward displaces a probe whatever their sizes, and a
    probe never displaces a forward: five guesses filling five slots is how
    JobRight came to be represented by five copies of a video SDK's config
    while its own listings were turned away for lack of room.

    Endpoint and shape diversity precede size. Keep an older working example
    while admitting new unread examples, so a repair can be checked against
    what previously worked. Identical responses refresh one observation.

    Never raises. This runs inside the harvest, and a sample that could not be
    written must not cost the jobs that were.
    """
    if not host or payload is None:
        return False
    if not bool(getattr(live(), "HARVEST_SAMPLES_ENABLED", True)):
        return False

    try:
        from app.models.harvest_recipe import HarvestSample

        size = len(json.dumps(payload, default=str))
        digest, shape = fingerprint(payload), shape_hash(payload)
        endpoint = endpoint_key(source_url)
        now = datetime.now(timezone.utc)

        held = (
            db.query(HarvestSample)
            .filter(HarvestSample.host == host)
            # Least useful first: probes before forwards, then smallest.
            .order_by(HarvestSample.probe.desc(), HarvestSample.bytes.asc())
            .all()
        )
        duplicate = next((row for row in held if row.fingerprint == digest
                          and sample_endpoint(row) == endpoint and bool(row.probe) == bool(probe)), None)
        if duplicate:
            duplicate.last_seen_at = now
            duplicate.observations = (duplicate.observations or 1) + 1
            if found:
                duplicate.found = max(duplicate.found or 0, int(found))
                duplicate.note = str(note)[:200] or duplicate.note
            db.flush()
            return False
        if len(held) >= _keep():
            from app.services.harvest_recipes import jobbiness
            groups = Counter((sample_endpoint(row), row.shape_hash or shape_hash(row.payload)) for row in held)
            incoming_group = (endpoint, shape)
            diverse = incoming_group not in groups
            protected = {}
            for row in sorted(held, key=lambda r: r.created_at or now):
                group = (sample_endpoint(row), row.shape_hash or shape_hash(row.payload))
                if row.found:
                    protected.setdefault(group, row.id)
            candidates = [row for row in held if row.id not in protected.values()]
            replacing_healthy = not candidates
            if not candidates:
                # Healthy snapshots must not make a redesign uncapturable.
                if found or not jobbiness(payload):
                    return False
                candidates = [min(held, key=lambda r: r.last_seen_at or r.created_at or now)]
            # A new endpoint/shape displaces redundant evidence first. Within
            # one shape retain old and new examples so a repair has a baseline.
            weakest = min(candidates, key=lambda row: (
                not row.probe, bool(jobbiness(row.payload)),
                groups[(sample_endpoint(row), row.shape_hash or shape_hash(row.payload))] <= 1,
                row.bytes, row.created_at or now))
            useful = bool(jobbiness(payload))
            redundant = groups[(sample_endpoint(weakest), weakest.shape_hash or shape_hash(weakest.payload))] > 1
            # Prefer to preserve working evidence, without letting a full
            # set of healthy snapshots suppress every later redesign.
            if bool(weakest.probe) != bool(probe):
                # Different kinds, so the kind decides and size does not come
                # into it: a forward displaces a probe however small, and a
                # probe never displaces a forward however large.
                if probe:
                    return False
            elif not (useful and (replacing_healthy or diverse and redundant or groups[incoming_group] >= 2
                                   or not jobbiness(weakest.payload))) and weakest.bytes >= size:
                return False
            db.delete(weakest)
            db.flush()
            logger.info(
                "harvest_samples: %s was full; dropped a %d-byte %s for a "
                "%d-byte %s", host, weakest.bytes,
                "probe" if weakest.probe else "forward", size,
                "probe" if probe else "forward",
            )

        db.add(HarvestSample(
            host=str(host)[:160],
            source_url=(str(source_url)[:1000] or None),
            page_url=str(page_url)[:1000] or None,
            endpoint_key=endpoint, fingerprint=digest, shape_hash=shape,
            last_seen_at=now, observations=1,
            payload=_fits(payload),
            bytes=size,
            found=int(found or 0),
            note=(str(note)[:200] or None),
            probe=bool(probe),
        ))
        # Flushed so the count above sees it next time. A failing site sends
        # payload after payload inside one request cycle, and without this the
        # cap never engages — every call counts the rows already committed and
        # none of the ones queued beside it.
        db.flush()
        logger.info(
            "harvest_samples: kept a %d-byte payload from %s that yielded %d job(s)",
            size, host, found,
        )
        return True
    except Exception as exc:
        logger.warning("harvest_samples: could not keep a sample from %s: %s", host, exc)
        return False


def for_host(db, host: str, limit: int = 5, endpoint: str | None = None) -> list:
    """This host's samples, newest first — what a recipe is proposed from."""
    from app.models.harvest_recipe import HarvestSample

    rows = (
        db.query(HarvestSample)
        .filter(HarvestSample.host == host)
        .order_by(HarvestSample.created_at.desc())
        .all()
    )
    if endpoint is not None:
        rows = [row for row in rows if sample_endpoint(row) == endpoint]
    return rows[:max(1, limit)]


def _related(host: str, domains: set[str]) -> bool:
    """Whether this host is one of ours, or lives under one that is."""
    host = (host or "").strip().lower()
    if not host:
        return False
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in domains if domain
    )


def worth_learning(db) -> set[str]:
    """
    The domains a job payload could plausibly have come from.

    Every board loads a dozen third parties, all of them answering in
    structured JSON, and the probe forwards near misses on purpose — so
    FullStory, Bugsnag, PostHog, StackAdapt, ZoomInfo, Cognito and Segment all
    end up with samples stored under their own hostnames. Thirteen of the
    fifteen hosts in the store were telemetry, each one offering a button that
    would spend a model call working out how to read a session token.

    The interceptor now declines to probe off-site, but that runs on a browser
    that may be an old build and is not this server's to trust. The list is
    cheap to check here and the answer is the same either way.
    """
    from app.services import browser_tasks
    from app.services.harvest import HARVEST_SOURCES

    domains = set(HARVEST_SOURCES)
    try:
        domains |= browser_tasks.reading_hosts(db)
    except Exception:
        # A profile that cannot be read costs the extra hosts, not the list.
        pass
    return domains


def hosts(db, all_hosts: bool = False) -> list[dict]:
    """
    Hosts with samples waiting, and whether anything has been learned yet.

    The worklist: a host here with no active recipe is a site sending payloads
    nobody can read.

    Narrowed to boards we actually browse — see `worth_learning`. Pass
    `all_hosts` to see everything that was stored, which is what you want when
    asking why a site is *missing* from the list.
    """
    from sqlalchemy import func

    from app.models.harvest_recipe import HarvestRecipe, HarvestSample

    rows = (
        db.query(
            HarvestSample.host,
            func.count(HarvestSample.id),
            func.max(HarvestSample.created_at),
        )
        .filter(HarvestSample.found == 0)
        .group_by(HarvestSample.host)
        .order_by(func.count(HarvestSample.id).desc())
        .all()
    )
    active = {
        row[0] for row in
        db.query(HarvestRecipe.host).filter(HarvestRecipe.status == "active").all()
    }
    ours = worth_learning(db)
    # Unknown first-party job evidence is the onboarding case, not telemetry.
    from app.services.harvest_recipes import jobbiness
    evidenced = {row.host for row in db.query(HarvestSample).filter(HarvestSample.found == 0).all()
                 if jobbiness(row.payload) and not row.probe}
    return [
        {"host": host, "samples": int(count or 0), "last_seen": last,
         "has_recipe": host in active}
        for host, count, last in rows
        if all_hosts or _related(host, ours) or host in evidenced
    ]


def drop_unrelated(db) -> int:
    """
    Delete samples from hosts no board of ours could have produced.

    A one-off for what the probe collected before it learned to stay on-site,
    and a safety net afterwards for a browser still running an old build.

    Deletes nothing unless some browser has told us what it is reading. The
    hard-coded source list alone is not enough to delete on: a board nobody has
    got round to adding to it is exactly the kind this is meant to help with,
    and destroying its evidence to tidy a panel would be the wrong trade in the
    wrong direction.
    """
    from app.models.harvest_recipe import HarvestSample
    from app.services import browser_tasks

    try:
        if not browser_tasks.reading_hosts(db):
            return 0
    except Exception:
        return 0

    ours = worth_learning(db)
    stored = {row[0] for row in db.query(HarvestSample.host).distinct().all()}
    junk = [host for host in stored if not _related(host, ours)]
    from app.services.harvest_recipes import jobbiness
    evidence = {row.host for row in db.query(HarvestSample).filter(HarvestSample.host.in_(junk)).all()
                if not row.probe and jobbiness(row.payload)}
    junk = [host for host in junk if host not in evidence]
    if not junk:
        return 0
    removed = (
        db.query(HarvestSample)
        .filter(HarvestSample.host.in_(junk))
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        logger.info(
            "harvest_samples: dropped %d sample(s) from %d unrelated host(s)",
            removed, len(junk),
        )
    return removed


def clear(db, host: str) -> int:
    """Explicitly forget a host's captured samples."""
    from app.models.harvest_recipe import HarvestSample

    removed = (
        db.query(HarvestSample)
        .filter(HarvestSample.host == host)
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed


def prune(db) -> int:
    """Expire samples nobody turned into a recipe. Returns how many went."""
    from app.models.harvest_recipe import HarvestSample
    from sqlalchemy import func

    cutoff = datetime.now(timezone.utc) - timedelta(days=_ttl_days())
    removed = (
        db.query(HarvestSample)
        .filter(func.coalesce(HarvestSample.last_seen_at, HarvestSample.created_at) < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        logger.info("harvest_samples: expired %d old sample(s)", removed)
    return removed
