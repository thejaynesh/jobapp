"""
Reading a site whose payload the shape-based walker cannot.

The walker takes any object holding a title, a company and an identifier. It
covers most boards without being told anything, and it fails in two ways it
cannot fix from the inside.

A payload can name its fields something no alias list knows, and nothing comes
out at all. Or it can be **normalized** — the job carries a reference to a
company that lives elsewhere in the response, under `included[]` or `entities`
— and then the walker matches `companyUrn` and stores
`urn:li:fsd_company:1234` as an employer name. That second one is worse,
because it looks like success: title, company and URL are all non-empty, every
check passes, and the row is wrong.

Following a reference across a payload is exactly what a walker cannot do, and
exactly what a recipe can.

A recipe is data, not code
--------------------------
The obvious implementation asks a model to write a parser and then runs it.
This asks for a filled-in schema instead, and interprets it here. The
difference is not mainly safety — it is that a recipe can be printed, diffed,
run against a stored sample and rejected before it goes anywhere near the
pipeline. None of that is true of a generated parser, and in six months nobody
can say why one broke.

    {
      "roots":  ["data.jobSearch.results", "included"],
      "fields": {"title": ["jobTitle"], "company": ["employer.name"]},
      "join":   {"ref": "companyRef", "table": "included",
                 "key": "entityUrn", "take": "name", "into": "company"}
    }

Nothing is trusted on the model's say-so
----------------------------------------
A proposal is validated against the samples it was written from before it is
allowed to run: it has to find jobs, and the jobs have to look like jobs rather
than like identifiers. A recipe that only works on the payload it was written
from is the obvious failure, which is why it is tried against every sample the
host has and not just one.

And the walker always remains the fallback. A recipe adds a way to read a site;
it must never be able to take one away.
"""

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_ROOTS = 6
MAX_ALIASES = 8

# A value that is an identifier rather than a name. The reason this module
# exists, so it is also what validation refuses to accept.
_LOOKS_LIKE_ID = re.compile(
    r"^(urn:|[0-9a-f]{8}-[0-9a-f]{4}|\d+$|[A-Za-z]+:\d+$)", re.I
)

FIELDS = ("title", "company", "location", "description", "url", "id")


# ---------------------------------------------------------------------------
# Reading a payload with a recipe
# ---------------------------------------------------------------------------

def _dig(node, path: str):
    """
    Follow a dotted path, stepping through lists as it goes.

    `results.jobs.title` reaches into every element of `jobs` when `jobs` is a
    list, because a path written by a model describes the shape it saw and a
    payload rarely says which levels are arrays.
    """
    current = [node]
    for part in str(path or "").split("."):
        if not part:
            continue
        nxt = []
        for item in current:
            if isinstance(item, list):
                # `jobLocation.0.address`: a model writes the index it saw.
                # Taking it literally beats stepping through every element
                # looking for a key named "0" and finding nothing.
                if part.isdigit():
                    index = int(part)
                    nxt.append(item[index] if index < len(item) else None)
                    continue
                nxt.extend(
                    entry.get(part) for entry in item if isinstance(entry, dict)
                )
            elif isinstance(item, dict):
                nxt.append(item.get(part))
        current = [value for value in nxt if value is not None]
        if not current:
            return []
    return current


def _first_text(node: dict, paths) -> str:
    from app.services.harvest import _text

    for path in (paths or [])[:MAX_ALIASES]:
        for found in _dig(node, path):
            text = _text(found)
            if text:
                return text
    return ""


def _candidates(payload, roots) -> list[dict]:
    """Every object a root path points at, deduplicated by identity."""
    out: list[dict] = []
    seen: set[int] = set()
    for path in (roots or [])[:MAX_ROOTS]:
        for found in _dig(payload, path):
            items = found if isinstance(found, list) else [found]
            for item in items:
                if isinstance(item, dict) and id(item) not in seen:
                    seen.add(id(item))
                    out.append(item)
    return out


def _lookup_table(payload, spec: dict) -> dict:
    """`{key: object}` for the side of a join that holds the real values."""
    table: dict[str, dict] = {}
    key = spec.get("key") or "entityUrn"
    for entry in _candidates(payload, [spec.get("table") or ""]):
        from app.services.harvest import _text

        value = _text(entry.get(key))
        if value:
            table[value] = entry
    return table


def apply_recipe(payload, recipe: dict, source: str, page_url: str = "") -> list[dict]:
    """
    Jobs out of one payload, following `recipe`. Never raises.

    `page_url` is the page the payload came from. A payload holding exactly one
    job and no link of its own — a posting page's embedded data — is that
    page's job, so the page's address is its link.

    Returns the same shape `harvest.extract_jobs` does, so everything
    downstream — dedupe, storage, slug mining — is untouched by which reader
    produced the rows.
    """
    if not isinstance(recipe, dict) or not isinstance(payload, (dict, list)):
        return []

    try:
        fields = recipe.get("fields") or {}
        join = recipe.get("join") or None
        table = _lookup_table(payload, join) if join else {}

        found: dict[str, dict] = {}
        for node in _candidates(payload, recipe.get("roots")):
            job = {
                name: _first_text(node, fields.get(name))
                for name in FIELDS
            }

            if join and table:
                from app.services.harvest import _text

                reference = _text(_first_text(node, [join.get("ref") or ""]))
                related = table.get(reference)
                if related is not None:
                    value = _text(related.get(join.get("take") or "name"))
                    if value:
                        job[join.get("into") or "company"] = value

            if not job["title"] or not job["company"]:
                continue

            key = job["id"] or job["url"] or f"#{len(found)}"
            existing = found.get(key)
            if not existing or len(job["description"]) > len(existing["description"]):
                found[key] = job

        if page_url and len(found) == 1:
            only = next(iter(found.values()))
            only["url"] = only["url"] or page_url

        return [
            {
                "source": source,
                "source_job_id": job["id"] or None,
                "url": job["url"],
                "title": job["title"],
                "company": job["company"],
                "location": job["location"],
                "description": job["description"],
                "is_remote": "remote" in (job["location"] or "").lower(),
            }
            for job in found.values()
            if job["url"]
        ]
    except Exception as exc:
        # A recipe that throws is a recipe that does not work. Saying so beats
        # taking the harvest down with it.
        logger.warning("harvest_recipes: recipe failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Deciding whether to believe one
# ---------------------------------------------------------------------------

def looks_like_a_name(value: str) -> bool:
    """
    Whether this reads as a company name rather than as an identifier.

    The check that makes the whole feature worth having: a recipe that resolves
    a company reference to `urn:li:fsd_company:1234` passes every other test in
    the pipeline, which is how bad data gets in looking like good data.
    """
    text = (value or "").strip()
    if len(text) < 2 or len(text) > 120:
        return False
    if _LOOKS_LIKE_ID.match(text):
        return False
    return bool(re.search(r"[A-Za-z]{2}", text))


def validate(payload_samples: list, recipe: dict, source: str = "harvest",
             page_urls: list | None = None) -> dict:
    """
    Try a recipe against real payloads. Returns what happened and a verdict.

    Tried against every sample the host has rather than one, because a recipe
    that works only on the payload it was written from is the failure this is
    most likely to see.
    """
    outcome = {"ok": False, "jobs": 0, "samples": len(payload_samples or []),
               "matched_samples": 0, "reason": ""}
    if not payload_samples:
        outcome["reason"] = "no samples to check against"
        return outcome

    total = 0
    matched = 0
    named = 0
    urls = list(page_urls or []) + [""] * len(payload_samples)
    for payload, page_url in zip(payload_samples, urls):
        jobs = apply_recipe(payload, recipe, source, page_url=page_url or "")
        if jobs:
            matched += 1
        total += len(jobs)
        named += sum(1 for job in jobs if looks_like_a_name(job["company"]))

    outcome["jobs"] = total
    outcome["matched_samples"] = matched

    if not total:
        outcome["reason"] = "found no jobs in any sample"
        return outcome
    if named < total * 0.6:
        # Most of what it called a company is an identifier — the exact bug
        # this is supposed to fix, arrived by a different route.
        outcome["reason"] = (
            f"only {named} of {total} companies read as names rather than ids"
        )
        return outcome

    outcome["ok"] = True
    outcome["reason"] = f"{total} job(s) across {matched} of {len(payload_samples)} samples"
    return outcome


# ---------------------------------------------------------------------------
# Asking a model for one
# ---------------------------------------------------------------------------

_PROMPT = """\
You are writing a extraction recipe for job postings in a JSON API response.

Below are up to three real responses from {host}. Our generic reader found no
usable jobs in them — either the field names are ones it does not know, or the
payload is normalized and the job references its company rather than naming it.

Work out where the job objects live and which keys hold each field.

Return ONLY this JSON, no prose:

{{
  "roots": ["dot.path.to.the.array.of.jobs"],
  "fields": {{
    "title":       ["key", "or.dotted.path"],
    "company":     ["key"],
    "location":    ["key"],
    "description": ["key"],
    "url":         ["key"],
    "id":          ["key"]
  }},
  "join": {{
    "ref": "key.on.the.job.holding.the.company.reference",
    "table": "dot.path.to.the.array.of.companies",
    "key": "key.on.that.array.matching.ref",
    "take": "key.holding.the.company.name",
    "into": "company"
  }},
  "note": "one sentence on what this payload looks like"
}}

Rules:
- Paths are relative to the whole response and may step through arrays.
- Omit "join" entirely unless the company really is a reference. Use it when
  the job holds something like an id or urn instead of a readable name.
- "company" must end up a readable name. If the only company value on the job
  object is an identifier, that is what "join" is for.
- Every field is a list; put the most likely key first.
- Omit a field you cannot find rather than guessing at it. In particular a
  response describing ONE posting often has no url — that is fine, the page's
  own address is used. Do not invent one.
- A path may index an array ("jobLocation.0.address.addressLocality"), but
  plain keys step through arrays by themselves.

Responses from {host}:

{samples}
"""


def _clean(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def propose(samples: list, host: str, profile_data: dict | None = None,
            located: list | None = None, hint: str = "") -> dict:
    """
    Ask a model how to read this host. Returns `{recipe, error}`.

    Never raises: this is a button, and a provider having a bad afternoon
    should cost the proposal rather than the page.
    """
    from app.services import model_roles

    payloads = [s.payload for s in (samples or [])][:3]
    if not payloads:
        return {"recipe": None, "error": "No samples stored for this host yet."}

    try:
        rendered = "\n\n---\n\n".join(
            json.dumps(payload, indent=1)[:12000] for payload in payloads
        )
        prompt = _PROMPT.format(host=host, samples=rendered)
        if located:
            where = ", ".join(f"{root}.{key}" if root else key for root, key in located[:6])
            prompt += (f"\n\nA job titled {hint!r} is on this page, and appears in "
                       f"these responses at: {where}. The job objects are there.\n")
        raw = model_roles.call(
            profile_data, "learn",
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2000,
        )
        parsed = json.loads(_clean(raw))
    except Exception as exc:
        logger.warning("harvest_recipes: could not propose for %s: %s", host, exc)
        return {"recipe": None, "error": f"Could not reach the model: {exc}"}

    if not isinstance(parsed, dict) or not parsed.get("roots"):
        return {"recipe": None, "error": "The model did not return a usable recipe."}

    # Normalised into the shape the interpreter expects, so a model that
    # answered with a bare string where a list belongs is not a failure.
    fields = {}
    for name in FIELDS:
        value = (parsed.get("fields") or {}).get(name)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list) and value:
            fields[name] = [str(v) for v in value[:MAX_ALIASES]]

    recipe = {
        "roots": [str(r) for r in (parsed.get("roots") or [])[:MAX_ROOTS]],
        "fields": fields,
        "note": str(parsed.get("note") or "")[:300],
    }
    join = parsed.get("join")
    if isinstance(join, dict) and join.get("ref") and join.get("table"):
        recipe["join"] = {
            "ref": str(join.get("ref")),
            "table": str(join.get("table")),
            "key": str(join.get("key") or "entityUrn"),
            "take": str(join.get("take") or "name"),
            "into": str(join.get("into") or "company"),
        }
    return {"recipe": recipe, "error": None}


# ---------------------------------------------------------------------------
# Storing them
# ---------------------------------------------------------------------------

def active_for(db, host: str) -> dict | None:
    """The recipe in use for this host, or None. Never raises."""
    if not host:
        return None
    try:
        from app.models.harvest_recipe import HarvestRecipe

        row = (
            db.query(HarvestRecipe)
            .filter(HarvestRecipe.host == host, HarvestRecipe.status == "active")
            .first()
        )
        return row.recipe if row else None
    except Exception as exc:
        logger.warning("harvest_recipes: could not read a recipe for %s: %s", host, exc)
        return None


def save(db, host: str, recipe: dict, outcome: dict, model: str = "") -> object:
    """
    Store a proposal, and activate it when validation was satisfied.

    Activating replaces whatever was active, in one transaction: the partial
    unique index allows exactly one per host, so retiring the old one is not
    tidiness, it is the only way the insert succeeds.
    """
    from app.models.harvest_recipe import HarvestRecipe

    accepted = bool(outcome.get("ok"))
    if accepted:
        (
            db.query(HarvestRecipe)
            .filter(HarvestRecipe.host == host, HarvestRecipe.status == "active")
            .update({"status": "rejected", "note": "superseded"},
                    synchronize_session=False)
        )

    row = HarvestRecipe(
        host=str(host)[:160],
        recipe=recipe,
        status="active" if accepted else "proposed",
        jobs_found=int(outcome.get("jobs") or 0),
        samples_tried=int(outcome.get("samples") or 0),
        note=str(outcome.get("reason") or "")[:2000],
        model=str(model or "")[:120] or None,
        activated_at=datetime.now(timezone.utc) if accepted else None,
    )
    db.add(row)
    db.commit()
    logger.info(
        "harvest_recipes: %s a recipe for %s (%s)",
        "activated" if accepted else "stored", host, outcome.get("reason"),
    )
    return row


# ---------------------------------------------------------------------------
# Choosing what to show the model, and reading a hint
# ---------------------------------------------------------------------------

_JOBBY_KEY = re.compile(
    r"^(job)?title$|jobtitle|posting|company|employer|hiringorg|location"
    r"|salary|compensation|description|apply|remote|workplace|seniority",
    re.I,
)


def jobbiness(payload, limit: int = 20_000) -> int:
    """
    How many keys in this payload are named like job fields.

    A payload with none is not a job list whatever else it is, and sending it
    to a model to find the jobs in wastes the call and returns "found no jobs",
    which reads as the model failing rather than the evidence being wrong.
    """
    count = 0
    stack = [payload]
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if _JOBBY_KEY.search(str(key)):
                    count += 1
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return count


def locate_text(payload, needle: str, limit: int = 8) -> list[tuple[str, str]]:
    """
    Where a piece of text you can see on the page lives in a payload.

    Returns `(path to the array holding the object, key within it)` pairs — a
    job title found at `data.jobs[3].title` comes back as `("data.jobs",
    "title")`, which is exactly a recipe's root and field. Paths are written
    without indices because `_dig` steps through arrays by itself.
    """
    needle = (needle or "").strip().lower()
    if len(needle) < 3:
        return []
    found: list[tuple[str, str]] = []

    def walk(node, path, array_path):
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                if isinstance(value, str) and needle in value.lower():
                    root = array_path if array_path is not None else path
                    rel = here[len(root) + 1:] if root and here.startswith(root + ".") else str(key)
                    pair = (root or "", rel)
                    if pair not in found:
                        found.append(pair)
                else:
                    walk(value, here, array_path)
        elif isinstance(node, list):
            for item in node[:200]:
                walk(item, path, path)

    walk(payload, "", None)
    return found


def _field_near(node: dict, keys, depth: int = 2,
                inner_keys=("name", "displayName", "title", "text")) -> str | None:
    """The first key in `keys` present on `node` or one level inside it."""
    if not isinstance(node, dict):
        return None
    for key in keys:
        if key in node and node[key] not in (None, "", [], {}):
            value = node[key]
            if isinstance(value, dict) and depth > 1:
                inner = _field_near(value, inner_keys, 1)
                if inner:
                    return f"{key}.{inner}"
                continue
            return key
    return None


def recipe_from_title(samples: list, title: str) -> dict | None:
    """
    A recipe built from a job title you can see on the page, with no model.

    The title says which array holds the jobs and which key is the title; the
    other fields are then the usual aliases looked up on that same object. It
    is only a draft — `validate` decides whether it reads real jobs.
    """
    from app.services import harvest

    for payload in samples:
        for root, key in locate_text(payload, title):
            objects = [o for o in _candidates(payload, [root]) if isinstance(o, dict)]
            if not objects:
                continue
            sample = objects[0]
            fields = {"title": [key]}
            for name, aliases in (
                ("company", harvest._COMPANY_KEYS), ("location", harvest._LOCATION_KEYS),
                ("description", harvest._DESCRIPTION_KEYS), ("url", harvest._URL_KEYS),
                ("id", harvest._ID_KEYS + ("identifier",)),
            ):
                # An id wrapped in an object (schema.org's `identifier`) keeps
                # it under `value`; a company's name is under `name`.
                inner = ("value", "id") if name == "id" else (
                    "name", "displayName", "title", "text")
                hit = _field_near(sample, aliases, inner_keys=inner)
                if hit:
                    fields[name] = [hit]
            # A link or an id is no longer demanded here: a posting page's own
            # data has neither, and `apply_recipe` gives it the page's address.
            if "company" in fields:
                return {"roots": [root] if root else [""], "fields": fields,
                        "note": f"built from the title {title!r} found at {root}.{key}"}
    return None


_TITLE_LIKE_KEY = re.compile(r"title|position|role|headline", re.I)


def title_candidates(payloads: list, limit: int = 12) -> list[str]:
    """
    Strings in these payloads that read like job titles.

    What you would otherwise have to find yourself: a hint only helps if it is
    a title the payload actually holds, and nobody can be expected to know
    what a stored response contains. So the values under title-shaped keys are
    offered — to the panel as click-to-use buttons, and to `learn`, which
    tries them on its own before it asks a model anything.
    """
    from app.services import harvest

    strong = {k.lower() for k in harvest._STRONG_TITLE_KEYS}
    found: list[str] = []
    seen: set[str] = set()

    def plausible(text: str) -> bool:
        text = text.strip()
        return (4 <= len(text) <= 120 and re.search(r"[A-Za-z]{3}", text)
                and not text.startswith(("http", "/", "{", "urn:"))
                and not _LOOKS_LIKE_ID.match(text) and text.count(" ") <= 14)

    def walk(node, depth=0):
        if len(found) >= limit or depth > 30:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key)
                if isinstance(value, str) and (name.lower() in strong or _TITLE_LIKE_KEY.search(name)):
                    text = value.strip()
                    if plausible(text) and text.lower() not in seen:
                        seen.add(text.lower())
                        found.append(text)
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:50]:
                walk(item, depth + 1)

    for payload in sorted(payloads or [], key=jobbiness, reverse=True):
        walk(payload)
    return found[:limit]


def learn(db, host: str, profile_data: dict | None = None, hint: str = "") -> dict:
    """
    Propose, validate and store in one go. What the button calls.

    `hint` is optional and is usually a job title you can see on the page.
    Found in a payload, it pins down where the jobs are without asking anyone,
    and when a model is still needed it is told exactly where to look.
    """
    from app.services import harvest_samples, model_roles

    samples = harvest_samples.for_host(db, host, limit=8)
    if not samples:
        return {"ok": False, "reason": "No samples stored for this host yet."}

    hint = (hint or "").strip()[:300]

    # The built-in reader first. It keeps learning formats (schema.org
    # JobPosting most recently), so a host can land on this list for a payload
    # it now reads — and a recipe for that would be a second copy of code that
    # already works. Its samples are cleared so the host leaves the list.
    from app.services.harvest import extract_jobs

    readable = sum(
        len(extract_jobs(row.payload, page_url=row.source_url or ""))
        for row in samples
    )
    if readable:
        harvest_samples.clear(db, host)
        db.commit()
        return {"ok": True, "jobs": readable,
                "reason": f"the built-in reader already reads these ({readable} "
                          "job(s)), so no recipe is needed — cleared them from the "
                          "list; new visits are read as they arrive"}
    # The most job-like payloads first. The store keeps whatever arrived, and
    # the first three were often analytics — which is what the model was shown.
    ranked = sorted(samples, key=lambda row: jobbiness(row.payload), reverse=True)
    payloads = [row.payload for row in ranked]
    page_urls = [row.source_url or "" for row in ranked]
    located = []
    if hint:
        located = [pair for payload in payloads for pair in locate_text(payload, hint)]
        ranked = sorted(ranked, key=lambda row: bool(locate_text(row.payload, hint)),
                        reverse=True)
        payloads = [row.payload for row in ranked]
        page_urls = [row.source_url or "" for row in ranked]
    elif not jobbiness(payloads[0]):
        return {"ok": False,
                "reason": f"none of the {len(samples)} stored payloads look like job "
                          "listings (no title, company or location fields) — they are "
                          "probably the site's analytics. Press Forget, open a search "
                          "on the site, then try again; or type a job title you can "
                          "see on the page into the box"}
    if hint and not located:
        return {"ok": False,
                "reason": f"{hint!r} does not appear in any of the {len(samples)} stored "
                          "payloads, so the jobs on that page arrived some other way. "
                          "Press Forget and visit the page again"}

    # A title — yours, or failing that each title-shaped value the payloads
    # hold — says where the jobs are. Tried without a model first: it is exact
    # when it works and costs nothing when it does not.
    tries = [(hint, "your hint")] if hint else [
        (title, "the payload itself") for title in title_candidates(payloads, 8)
    ]
    for title, source in tries:
        drafted = recipe_from_title(payloads, title)
        if not drafted:
            continue
        outcome = validate(payloads, drafted, page_urls=page_urls)
        if outcome["ok"]:
            row = save(db, host, drafted, outcome, model=f"read from {source}")
            return {"ok": True,
                    "reason": outcome["reason"] + f" (read from {source}, no model)",
                    "jobs": outcome["jobs"], "recipe": drafted, "id": str(row.id)}

    proposal = propose(ranked, host, profile_data, located=located, hint=hint)
    if proposal["error"]:
        return {"ok": False, "reason": proposal["error"]}

    provider = model_roles.resolve(profile_data, "learn")
    outcome = validate(payloads, proposal["recipe"], page_urls=page_urls)
    row = save(db, host, proposal["recipe"], outcome,
               model=provider.model if provider else "")
    return {
        "ok": outcome["ok"],
        "reason": outcome["reason"],
        "jobs": outcome["jobs"],
        "recipe": proposal["recipe"],
        "id": str(row.id),
    }


def listing(db, limit: int = 30) -> list:
    """Every recipe, newest first — what the panel shows."""
    from app.models.harvest_recipe import HarvestRecipe

    return (
        db.query(HarvestRecipe)
        .order_by(HarvestRecipe.created_at.desc())
        .limit(max(1, limit))
        .all()
    )
