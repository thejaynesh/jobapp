"""
Working out how to walk a board, instead of being told.

`harvest_recipes` learns where the jobs are in a payload. This learns the step
before it: how to make the page show more of them.

Boards reach their second page three ways — a URL parameter, a scroll, or a
click on a numbered control — and using the wrong one harvests page one forever
while every number on the panel looks healthy. Each board's mechanism used to
be hand-written in `browse_plan.BOARDS`, which covers the boards somebody
happened to classify and silently under-crawls everything else.

The flow mirrors the harvest recipes exactly:

    a visit that cannot get past the first screen
        -> the extension sends back what the page offered
        -> a model proposes a mode
        -> validation refuses it if it does not check out against that evidence
        -> `browse_plan` reads the active recipe instead of the hardcoded board
        -> the visits it produces are watched, and a recipe that never gets
           anywhere retires itself

The validation is the load-bearing part, and more so here than for extraction.
A wrong extraction recipe stores a bad company name. A wrong *click* presses a
button on a logged-in job board, and "Withdraw application" is on some of those
pages. So a proposed selector has to match something in the sample, that
something has to read like pagination, and anything reading like an action is
refused outright — see `validate`.
"""

import json
import logging
import re

from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# The modes a recipe may claim. Closed set: free text here would eventually
# produce three spellings of "scroll".
MODES = ("scroll", "click", "url")

# Ceilings, applied to whatever a model asks for. These are the same numbers
# the extension clamps to, stated here so a nonsense proposal is refused at the
# point it is written rather than quietly trimmed at the point it runs.
MAX_CLICK_PAGES = 30
MAX_SCROLL_PASSES = 300
MAX_PAGE_SIZE = 200

# A control that looks like it moves through pages: a bare number, "next", or
# one of the arrow glyphs boards use instead of a word.
_PAGINATION_LABEL = re.compile(
    r"^(\d{1,4}|next(\s+page)?|older|more|show more|load more"
    r"|›|»|→|>|\.\.\.)$",
    re.I,
)

# A control that does something to your account or your application. Refused
# whatever else it looks like, because the cost of being wrong is not a bad row
# in a table — it is an action taken on a real board under a real login, and it
# is not undoable by re-running the crawl.
_DANGEROUS_LABEL = re.compile(
    r"delete|remove|withdraw|unsave|unsubscribe|sign\s*out|log\s*out"
    r"|apply|submit|send|accept|decline|reject|archive|report|block"
    r"|cancel|close\s+account|pay|purchase|upgrade|subscribe",
    re.I,
)

# Selector syntax we are prepared to hand to `querySelector`. Deliberately
# narrow — tag, class, id, and attribute predicates. It refuses anything with a
# paren or a brace, which rules out `:has()`, `:nth-child()` tricks and
# anything that is not really a selector at all.
_SELECTOR_OK = re.compile(r"^[A-Za-z0-9_\-\.\#\[\]\=\"'\^\$\*\|\~\s>+,:]+$")

_PARAM_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,40}$")


def _clean(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def _label_of(control: dict) -> str:
    """The text a person would read on this control."""
    if not isinstance(control, dict):
        return ""
    for key in ("aria", "aria_label", "title", "text", "label"):
        value = str(control.get(key) or "").strip()
        if value:
            return value
    return ""


def _matches_selector(control: dict, selector: str) -> bool:
    """
    Whether this sampled control is plausibly what the selector names.

    Not a CSS engine, and not pretending to be one: the sample carries a few
    attributes per control, so this checks that the selector's own literals
    appear among them. The real test is empirical — `note_outcome` retires a
    recipe whose visits never get anywhere — and this only has to stop a
    selector that has no relationship to the page at all.
    """
    if not isinstance(control, dict) or not selector:
        return False

    haystack = " ".join(
        str(control.get(key) or "")
        for key in ("tag", "cls", "id", "rel", "aria", "aria_label",
                    "title", "text", "testid", "role")
    ).lower()

    # The *values* a selector names, not its attribute names. Taking every
    # identifier out of `[data-testid='pagination-next']` yields "data" and
    # "testid" too, and no control carries those as a value — so a perfectly
    # good selector matched nothing and every click recipe was refused.
    needles = [
        piece.lower()
        for piece in (
            re.findall(r"['\"]([^'\"]+)['\"]", selector)        # [attr='value']
            + re.findall(r"\[[A-Za-z_-]+[~^$*|]?=([^\]'\"]+)\]", selector)  # [attr=value]
            + re.findall(r"\.([A-Za-z0-9_-]+)", selector)          # .class
            + re.findall(r"#([A-Za-z0-9_-]+)", selector)           # #id
        )
        if piece.strip()
    ]
    if needles:
        return all(piece in haystack for piece in needles)

    # A selector with no literal to check — `button`, `a[rel]` — is only
    # matched on its element name, which is weak. That is deliberate: this
    # check exists to reject a selector unrelated to the page, and the
    # empirical test in `note_outcome` is what catches one that is merely
    # wrong.
    tag = re.match(r"^([A-Za-z][A-Za-z0-9]*)", selector.strip())
    return bool(tag) and str(control.get("tag") or "").lower() == tag.group(1).lower()


def validate(evidence: dict, recipe: dict) -> dict:
    """
    Whether this recipe is safe and plausible for this page. `{ok, reason}`.

    Refuses rather than trims. A proposal outside these bounds is a proposal
    that did not understand the page, and quietly clamping it into range would
    hide that — the recipe would go active, do nothing useful, and look like
    the board had changed.
    """
    if not isinstance(recipe, dict):
        return {"ok": False, "reason": "The model did not return an object."}

    mode = str(recipe.get("mode") or "").strip().lower()
    if mode not in MODES:
        return {"ok": False, "reason": f"Unknown mode {mode!r}."}

    if mode == "scroll":
        passes = recipe.get("scroll_passes")
        if not isinstance(passes, int) or not 1 <= passes <= MAX_SCROLL_PASSES:
            return {"ok": False,
                    "reason": f"scroll_passes must be 1..{MAX_SCROLL_PASSES}."}
        return {"ok": True, "reason": f"Scrolls {passes} times."}

    if mode == "url":
        param = str(recipe.get("page_param") or "")
        if not _PARAM_OK.match(param):
            return {"ok": False, "reason": f"Implausible page_param {param!r}."}
        size = recipe.get("page_size")
        if not isinstance(size, int) or not 1 <= size <= MAX_PAGE_SIZE:
            return {"ok": False, "reason": f"page_size must be 1..{MAX_PAGE_SIZE}."}
        base = recipe.get("page_base", 0)
        if not isinstance(base, int) or base not in (0, 1):
            return {"ok": False, "reason": "page_base must be 0 or 1."}
        # The parameter has to be one the page actually uses, or one that is
        # absent — inventing `?page=2` on a board that pages by `start` gives a
        # URL that returns page one, five times, and looks like depth.
        seen = evidence.get("query") if isinstance(evidence, dict) else {}
        if isinstance(seen, dict) and seen and param not in seen:
            known = ", ".join(sorted(seen)) or "none"
            return {"ok": False,
                    "reason": f"{param!r} is not in this URL (has: {known})."}
        return {"ok": True, "reason": f"Pages by ?{param}="}

    # mode == "click"
    selector = str(recipe.get("selector") or "").strip()
    if not selector or len(selector) > 200:
        return {"ok": False, "reason": "A click recipe needs a selector."}
    if not _SELECTOR_OK.match(selector):
        return {"ok": False, "reason": "That selector has syntax we won't run."}

    pages = recipe.get("max_pages", 10)
    if not isinstance(pages, int) or not 1 <= pages <= MAX_CLICK_PAGES:
        return {"ok": False, "reason": f"max_pages must be 1..{MAX_CLICK_PAGES}."}

    controls = (evidence or {}).get("controls") or []
    hits = [c for c in controls if _matches_selector(c, selector)]
    if not hits:
        return {"ok": False,
                "reason": "That selector matches nothing the page offered."}

    # Every control it matches has to be a pagination control. One match that
    # reads like an action is enough to refuse the whole recipe: a selector
    # loose enough to catch both will eventually catch the wrong one first.
    for control in hits:
        label = _label_of(control)
        if _DANGEROUS_LABEL.search(label):
            return {"ok": False,
                    "reason": f"That selector matches {label!r}, which is an "
                              f"action rather than a page control."}
    readable = [c for c in hits if _PAGINATION_LABEL.match(_label_of(c))]
    if not readable:
        labels = ", ".join(repr(_label_of(c)) for c in hits[:3])
        return {"ok": False,
                "reason": f"Matches {labels}, which does not read like a page "
                          f"control."}

    return {"ok": True,
            "reason": f"Clicks {_label_of(readable[0])!r}, up to {pages} pages."}


_PROMPT = """\
You are working out how a job board shows its second page of results.

A crawler opened {url} and could not get past the first screenful. It scrolled
{passes} times and new results arrived {batches} time(s). Below is what the page
offered: the query parameters already in the URL, and every control that might
move between pages.

There are exactly three answers:

  scroll — there is no second page. The list grows as you move down it.
           Correct when scrolling did produce batches, or when there are no
           page controls at all.
  click  — numbered controls or a "next" button, with one address for every
           page. Correct when such a control is listed below.
  url    — the page number is a query parameter already visible in the URL.

Return ONLY this JSON, no prose. Include only the keys for the mode you chose:

{{
  "mode": "scroll" | "click" | "url",
  "scroll_passes": 150,
  "selector": "css selector for the NEXT-page control",
  "max_pages": 10,
  "page_param": "name of the query parameter",
  "page_size": 25,
  "page_base": 0,
  "note": "one sentence on how this board paginates"
}}

Rules:
- Choose "url" only if the parameter is already present in the query below.
  A parameter that is not there is a guess, and a wrong guess returns page one
  repeatedly while looking like depth.
- For "click", the selector must match the control that goes FORWARD one page —
  a "next" control, or the next number. Never a control that submits, applies,
  deletes, withdraws, or otherwise acts on the account.
- Prefer a selector built from a stable attribute (aria-label, rel, data-testid)
  over a generated class name.
- "page_base" is what the parameter reads on the first page: 0 for an offset,
  1 for an ordinal page number.
- If nothing below looks like a page control, answer "scroll".

URL: {url}
Query parameters already present: {query}
Scroll: {passes} passes, {batches} batches, page height {height}

Controls found on the page:

{controls}
"""


def propose(sample, profile_data: dict | None = None) -> dict:
    """
    Ask a model how this board paginates. Returns `{recipe, error}`.

    Goes through the `learn` role rather than naming a provider, which is the
    whole point of that indirection: this is a button somebody presses
    occasionally, so it should take the free provider and leave the paid budget
    to the scoring passes that run thousands of times. It was spending NIM
    calls purely because that is what the code it grew out of happened to pass.

    Never raises: a provider having a bad afternoon should cost the proposal
    rather than the page.
    """
    from app.services import model_roles

    if sample is None:
        return {"recipe": None, "error": "No crawl samples stored for this host."}

    evidence = sample.evidence or {}
    scroll = evidence.get("scroll") or {}
    prompt = _PROMPT.format(
        url=sample.source_url or f"https://{sample.host}/",
        query=json.dumps(evidence.get("query") or {}, ensure_ascii=False),
        passes=scroll.get("passes", 0),
        batches=scroll.get("batches", 0),
        height=scroll.get("doc_height", 0),
        controls=json.dumps(
            (evidence.get("controls") or [])[:40], indent=2, ensure_ascii=False,
        ),
    )

    try:
        raw = model_roles.call(
            profile_data, "learn",
            [{"role": "user", "content": prompt}],
            max_tokens=700,
        )
    except Exception as exc:
        logger.warning("crawl_recipes: proposal failed for %s: %s",
                       sample.host, exc)
        return {"recipe": None, "error": f"The model call failed: {exc}"}

    try:
        recipe = json.loads(_clean(raw))
    except Exception:
        return {"recipe": None, "error": "The model did not return JSON."}
    if not isinstance(recipe, dict):
        return {"recipe": None, "error": "The model did not return an object."}
    return {"recipe": recipe, "error": ""}


def record(db, host: str, source_url: str, evidence: dict,
           pages_reached: int = 0, batches: int = 0, note: str = ""):
    """
    Keep what a page offered, when a visit could not get past its first screen.

    Capped per host like the harvest samples, and for the same reason: this is
    evidence for a decision, not an archive. The newest one describes the page
    as it is now, which is the only version a recipe can be written against.
    """
    from app.models.crawl_recipe import CrawlSample

    host = (host or "").strip().lower()[:160]
    if not host or not isinstance(evidence, dict):
        return None

    row = CrawlSample(
        host=host,
        source_url=(source_url or "")[:1000] or None,
        evidence=evidence,
        pages_reached=max(0, int(pages_reached or 0)),
        batches=max(0, int(batches or 0)),
        note=(note or "")[:200] or None,
    )
    db.add(row)
    db.flush()
    _trim(db, host)
    return row


def _trim(db, host: str, keep: int = 3) -> None:
    from app.models.crawl_recipe import CrawlSample

    rows = (
        db.query(CrawlSample)
        .filter(CrawlSample.host == host)
        .order_by(CrawlSample.created_at.desc())
        .all()
    )
    dropped = rows[keep:]
    for row in dropped:
        db.delete(row)
    if dropped:
        # Flushed here rather than left pending. The session does not autoflush,
        # so an unflushed delete is invisible to the next call's query — the cap
        # then lags one behind and settles at `keep + 1`. The harvest samples
        # had the same bug for the same reason.
        db.flush()


def latest_sample(db, host: str):
    from app.models.crawl_recipe import CrawlSample

    return (
        db.query(CrawlSample)
        .filter(CrawlSample.host == (host or "").lower())
        .order_by(CrawlSample.created_at.desc())
        .first()
    )


def hosts_needing_a_recipe(db) -> list[str]:
    """Hosts with evidence stored and no active recipe — what the panel offers."""
    from app.models.crawl_recipe import CrawlRecipe, CrawlSample

    sampled = {row[0] for row in db.query(CrawlSample.host).distinct().all()}
    active = {
        row[0] for row in
        db.query(CrawlRecipe.host).filter(CrawlRecipe.status == "active").all()
    }
    return sorted(sampled - active)


def active_for(db, host: str) -> dict | None:
    """This host's live recipe, or None."""
    from app.models.crawl_recipe import CrawlRecipe

    host = (host or "").strip().lower()
    if not host:
        return None
    row = (
        db.query(CrawlRecipe)
        .filter(CrawlRecipe.host == host, CrawlRecipe.status == "active")
        .first()
    )
    return row.recipe if row else None


def save(db, host: str, recipe: dict, outcome: dict, model: str = ""):
    """Store a proposal, and activate it if validation was happy."""
    from app.models.crawl_recipe import CrawlRecipe

    host = (host or "").strip().lower()[:160]
    row = CrawlRecipe(
        host=host,
        recipe=recipe,
        status="active" if outcome.get("ok") else "rejected",
        note=(outcome.get("reason") or "")[:2000] or None,
        model=(model or "")[:120] or None,
    )
    if outcome.get("ok"):
        # One active recipe per host, enforced by a partial unique index —
        # retire the incumbent before the new one lands or the insert fails.
        db.query(CrawlRecipe).filter(
            CrawlRecipe.host == host, CrawlRecipe.status == "active",
        ).update({"status": "rejected"}, synchronize_session=False)
        row.activated_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    return row


def note_outcome(db, host: str, pages_reached: int) -> None:
    """
    Record how a visit under the active recipe went, and retire a useless one.

    The half that validation cannot do. A recipe is checked against a snapshot
    of the page, and a snapshot cannot say whether clicking that control
    actually advances anything — only the visit can. So a recipe that keeps
    landing on page one after a fair number of tries is withdrawn, which puts
    the board back on its hand-written setting and puts the host back on the
    panel's list of things to teach.
    """
    from app.models.crawl_recipe import CrawlRecipe

    host = (host or "").strip().lower()
    row = (
        db.query(CrawlRecipe)
        .filter(CrawlRecipe.host == host, CrawlRecipe.status == "active")
        .first()
    )
    if row is None:
        return

    row.tries = (row.tries or 0) + 1
    row.best_pages = max(row.best_pages or 0, int(pages_reached or 0))
    # Three tries before judging: one visit can reach a single page because the
    # board had one page of results that day, which is not the recipe's fault.
    if row.tries >= 3 and row.best_pages <= 1:
        row.status = "rejected"
        row.note = (
            f"Retired after {row.tries} visits that never got past page one. "
            f"{row.note or ''}"
        ).strip()[:2000]
        logger.info(
            "crawl_recipes: retired the recipe for %s — it never advanced", host,
        )
    db.commit()


def learn(db, host: str, profile_data: dict | None = None) -> dict:
    """Propose, validate and store in one go. What the button calls."""
    from app.services import model_roles

    sample = latest_sample(db, host)
    if sample is None:
        return {"ok": False,
                "reason": "Nothing stored for that host yet — crawl it once "
                          "so the extension can describe the page."}

    proposal = propose(sample, profile_data)
    if proposal["error"]:
        return {"ok": False, "reason": proposal["error"]}

    # Recorded so a recipe that turns out badly can be traced to the model that
    # wrote it, which is the first thing you want to know.
    provider = model_roles.resolve(profile_data, "learn")
    outcome = validate(sample.evidence or {}, proposal["recipe"])
    row = save(db, host, proposal["recipe"], outcome,
               model=provider.model if provider else "")
    return {
        "ok": outcome["ok"],
        "reason": outcome["reason"],
        "recipe": proposal["recipe"],
        "id": str(row.id),
    }


def listing(db, limit: int = 30) -> list:
    """Every crawl recipe, newest first — what the panel shows."""
    from app.models.crawl_recipe import CrawlRecipe

    return (
        db.query(CrawlRecipe)
        .order_by(CrawlRecipe.created_at.desc())
        .limit(max(1, limit))
        .all()
    )
