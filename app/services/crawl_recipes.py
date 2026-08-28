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

# A control that looks like it moves *forward* through pages: a bare number,
# "next", one of the arrow glyphs boards use instead of a word, or any of the
# ways a board spells "there is more below this".
#
# Forward only, and that is not an oversight. The extension collects "Previous"
# and "First" as well, because they tell a model it is looking at a pagination
# row — but clicking one walks backwards through results already harvested, so
# they are evidence and never a target.
#
# Kept deliberately wider than it reads: "Next" alone is the rare case. Real
# boards label the control "Next page", "Next results", "Load more jobs", and
# an accept list that only knew the bare word refused perfectly good proposals
# for controls the page plainly offered.
_PAGINATION_LABEL = re.compile(
    r"^(\d{1,4}"
    r"|page\s*\d{1,4}"
    r"|next(\s+(page|results?|jobs?|\d{1,4}))?"
    r"|older|newer"
    r"|(load|show|see|view)\s+more(\s+\w+)?"
    r"|more(\s+(results?|jobs?))?"
    r"|›|»|→|>|\.\.\.|…)$",
    re.I,
)

# A control that goes the wrong way. Not dangerous, just useless — and worse
# than useless if it is the first thing the selector matches, because the
# extension clicks the first match: the crawl then walks backwards through
# results it already has and reports depth for it.
#
# It earns its own check now that the extension collects these. It did not
# before, which is exactly why a selector loose enough to catch the whole
# pagination row used to be safe by accident.
_BACKWARD_LABEL = re.compile(
    r"^(prev(ious)?|back|first|newest|‹|«|←|<)(\s+(page|results?|jobs?))?$",
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


def _whole_number(value, default=None):
    """
    A count the model meant, or None if it did not give one we can use.

    Two things this exists for, both of which were refusing sound proposals.

    A model asked for JSON returns `"150"` and `150.0` about as readily as
    `150`, and an `isinstance(value, int)` test calls all three implausible.
    That is a transport detail being treated as a misunderstanding of the page.

    And an *absent* number is not a wrong one. The prompt says "include only the
    keys for the mode you chose" and then lists seven keys, so a model choosing
    scroll may legitimately answer `{"mode": "scroll"}` — a complete and correct
    statement that this board has no second page. That was rejected with
    "scroll_passes must be 1..300", which reads like the model said something
    absurd when it said nothing at all. The depth is the crawler's decision
    anyway: `browse_plan` already falls back to the board's own setting when a
    scroll recipe does not name one.

    A value that is present and *wrong* still fails, which is the case the
    range check was written for.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d{1,9}", text):
            return int(text)
    return None


# Query parameters that count pages, and ones that count results. The
# difference decides what "one page further" adds to the number: an ordinal
# goes up by one, an offset goes up by however many results a page holds.
#
# Getting this wrong is silent and expensive. `?page=1, 26, 51` on a board that
# numbers its pages fetches pages 1, 26 and 51 — three real pages, wildly
# scattered, with everything between them never visited and the crawl looking
# like it worked.
_ORDINAL_PARAMS = frozenset({
    "page", "p", "pg", "pagenum", "page_num", "pagenumber", "page_number",
    "pageindex", "page_index", "pageno", "page_no",
})
_OFFSET_PARAMS = frozenset({
    "offset", "start", "from", "skip", "startindex", "start_index",
    "startrow", "first",
})

# What the board itself calls its page length, when the URL says so.
_SIZE_PARAMS = ("per_page", "perpage", "page_size", "pagesize", "limit",
                "count", "num", "size", "rows", "results")


def _default_page_size(param: str, base, query: dict) -> int:
    """
    How much the page parameter advances by, when the model did not say.

    Read off the parameter's own name first, because that is what the board is
    telling you it means. `?page=` counts pages and advances by one; `?offset=`
    counts results and advances by a pageful — and if the URL says how big a
    pageful is, that is a better answer than any default.
    """
    name = (param or "").strip().lower()
    if name in _ORDINAL_PARAMS:
        return 1
    if name in _OFFSET_PARAMS or base == 0:
        for key in _SIZE_PARAMS:
            stated = _whole_number((query or {}).get(key))
            if stated and 1 <= stated <= MAX_PAGE_SIZE:
                return stated
        return 25
    # base == 1 with an unfamiliar name: counting from one is what an ordinal
    # does, so treat it as one.
    return 1


def validate(evidence: dict, recipe: dict) -> dict:
    """
    Whether this recipe is safe and plausible for this page.

    Returns `{ok, reason, recipe}` — the recipe normalised, which is what gets
    stored, so what runs and what the panel says are the same thing.

    The rule about what may be adjusted and what may not turns on one
    distinction, and getting it wrong in both directions is what made this
    reject sound proposals:

    * **A fact about the board is refused when it is out of range.** How much
      the page parameter advances by, whether pages count from zero or one —
      a wrong answer here builds URLs that skip or repeat results, and
      clamping it into range would hide that behind a recipe that goes active
      and quietly under-crawls.

    * **A budget of ours is clamped.** How many pages we are willing to click
      is our appetite, not a claim about the board. Handshake really does have
      400 pages of results, and answering "400" was correct; refusing it nine
      times because we only walk 30 got us nothing at all from a model that
      had read the page properly.
    """
    if not isinstance(recipe, dict):
        return {"ok": False, "reason": "The model did not return an object."}

    mode = str(recipe.get("mode") or "").strip().lower()
    if mode not in MODES:
        return {"ok": False, "reason": f"Unknown mode {mode!r}."}

    out = dict(recipe, mode=mode)

    def ok(reason):
        return {"ok": True, "reason": reason, "recipe": out}

    def no(reason):
        return {"ok": False, "reason": reason, "recipe": out}

    if mode == "scroll":
        # Absent means "this board scrolls, use the usual depth" — a complete
        # answer, and the one the prompt invites.
        if recipe.get("scroll_passes") is None:
            out.pop("scroll_passes", None)
            return ok("Scrolls; no second page to reach.")
        passes = _whole_number(recipe.get("scroll_passes"))
        if passes is None or passes < 1:
            return no(f"scroll_passes must be 1..{MAX_SCROLL_PASSES}.")
        # A budget. Asking for more than we will ever do is not a mistake about
        # the board.
        out["scroll_passes"] = min(passes, MAX_SCROLL_PASSES)
        return ok(f"Scrolls {out['scroll_passes']} times.")

    if mode == "url":
        param = str(recipe.get("page_param") or "")
        if not _PARAM_OK.match(param):
            return no(f"Implausible page_param {param!r}.")
        seen = evidence.get("query") if isinstance(evidence, dict) else {}
        seen = seen if isinstance(seen, dict) else {}

        base = _whole_number(recipe.get("page_base"), 0)
        if base not in (0, 1):
            return no("page_base must be 0 or 1.")
        out["page_base"] = base

        # A fact about the board, so it is refused when stated wrongly — but
        # inferred rather than demanded when it is simply absent. A model that
        # named the parameter has identified the mechanism, and the parameter's
        # own name says how it advances better than any constant would.
        if recipe.get("page_size") is None:
            out["page_size"] = _default_page_size(param, base, seen)
        else:
            size = _whole_number(recipe.get("page_size"))
            if size is None or not 1 <= size <= MAX_PAGE_SIZE:
                return no(f"page_size must be 1..{MAX_PAGE_SIZE}.")
            out["page_size"] = size

        # The parameter has to be one the page actually uses, or one that is
        # absent — inventing `?page=2` on a board that pages by `start` gives a
        # URL that returns page one, five times, and looks like depth.
        if seen and param not in seen:
            known = ", ".join(sorted(seen)) or "none"
            return no(f"{param!r} is not in this URL (has: {known}).")
        return ok(f"Pages by ?{param}= (+{out['page_size']} each)")

    # mode == "click"
    selector = str(recipe.get("selector") or "").strip()
    if not selector or len(selector) > 200:
        return no("A click recipe needs a selector.")
    if not _SELECTOR_OK.match(selector):
        return no("That selector has syntax we won't run.")
    out["selector"] = selector

    pages = _whole_number(recipe.get("max_pages"), 10)
    if pages is None or pages < 1:
        return no(f"max_pages must be 1..{MAX_CLICK_PAGES}.")
    # Our appetite, not a claim about the board. See the docstring.
    out["max_pages"] = min(pages, MAX_CLICK_PAGES)

    controls = (evidence or {}).get("controls") or []
    hits = [c for c in controls if _matches_selector(c, selector)]
    if not hits:
        return no("That selector matches nothing the page offered.")

    # Every control it matches has to be a pagination control. One match that
    # reads like an action is enough to refuse the whole recipe: a selector
    # loose enough to catch both will eventually catch the wrong one first.
    for control in hits:
        label = _label_of(control)
        if _DANGEROUS_LABEL.search(label):
            return no(f"That selector matches {label!r}, which is an action "
                      f"rather than a page control.")
        if _BACKWARD_LABEL.match(label):
            return no(f"That selector matches {label!r}, which goes back a "
                      f"page rather than forward.")

    # A selector that catches the whole numbered row is not a "next" control,
    # and the browser resolves it to the *first* match — which on any board
    # showing "1 2 3 … 400" is the button for page one. The crawl then clicks
    # its way back to the start and reports having visited pages.
    #
    # Undecidable rather than merely risky: nothing in a snapshot says which of
    # several numbers is forward from here, because that depends on the page
    # you are on. Handshake's proposal was exactly this — one class shared by
    # every page button — and it would have walked backwards on every visit.
    numbered = sorted({_label_of(c) for c in hits if _label_of(c).isdigit()})
    if len(numbered) > 1:
        listed = ", ".join(numbered[:4])
        hint = ""
        query = (evidence or {}).get("query") or {}
        for name in query:
            if name.lower() in _ORDINAL_PARAMS or name.lower() in _OFFSET_PARAMS:
                hint = (f" The page number is already in this URL as "
                        f"?{name}=, so this board wants a url recipe.")
                break
        return no(f"That selector matches {len(numbered)} numbered buttons "
                  f"({listed}), and the crawler clicks the first one — which "
                  f"is not necessarily forward.{hint}")

    readable = [c for c in hits if _PAGINATION_LABEL.match(_label_of(c))]
    if not readable:
        labels = ", ".join(repr(_label_of(c)) for c in hits[:3])
        return no(f"Matches {labels}, which does not read like a page control.")

    return ok(f"Clicks {_label_of(readable[0])!r}, up to "
              f"{out['max_pages']} pages.")


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
- PREFER "url" over "click" when the parameter is present, even if the page
  also shows numbered buttons. Changing an address is exact; clicking is not.
- "page_base" is what the parameter reads on the FIRST page: 0 for an offset,
  1 for an ordinal page number.
- "page_size" is how much the parameter ADVANCES between one page and the next
  — not how many results a page holds, unless those happen to be the same
  number. For "?page=" counting pages it is 1, so the pages are 1, 2, 3. For
  "?offset=" counting results, 25 results a page makes it 25, so the offsets
  are 0, 25, 50. Getting this wrong fetches three scattered real pages and
  never visits anything between them.
- For "click", the selector must match ONE control, and it must be the one that
  goes FORWARD — a "next" control. Never a control that submits, applies,
  deletes, withdraws, or otherwise acts on the account, and never one that goes
  back.
- A selector matching every numbered button will be refused. The crawler clicks
  the first thing the selector finds, and on a row reading "1 2 3 ... 400" that
  is the button for page one. If a board paginates only by numbers, its page
  number is nearly always in the URL — answer "url".
- Prefer a selector built from a stable attribute (aria-label, rel, data-testid)
  over a generated class name.
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


# How far a visit got, in the unit its mode is measured in, and the value at
# or below which it got nowhere.
#
# One number could not do this job, and using one is why every recipe that was
# not a click retired itself.
#
#   click  — `pages_done` counts the controls clicked within a single visit,
#            so page one alone means the click did nothing.
#   scroll — `pages_done` is 1 by construction: one URL, one page, however far
#            down it you got. Depth is `batches`, the number of times new
#            content actually arrived, and zero means the scroll found nothing.
#   url    — every page is a separate URL and a separate visit, so each one is
#            legitimately page one of its own address. No visit can grade this,
#            and grading it on `pages_done` retired every url recipe on its
#            third outing however well it was working.
_PROGRESS_FLOOR = {"click": 1, "scroll": 0}


def _progress(recipe: dict, pages_reached: int, batches: int):
    """How far this visit got, and the floor for its mode. `(value, floor)`."""
    mode = str((recipe or {}).get("mode") or "").lower()
    if mode == "click":
        return int(pages_reached or 0), _PROGRESS_FLOOR["click"]
    if mode == "scroll":
        return int(batches or 0), _PROGRESS_FLOOR["scroll"]
    return None, None


def note_outcome(db, host: str, pages_reached: int, batches: int = 0) -> None:
    """
    Record how a visit under the active recipe went, and retire a useless one.

    The half that validation cannot do. A recipe is checked against a snapshot
    of the page, and a snapshot cannot say whether clicking that control
    actually advances anything — only the visit can. So a recipe that keeps
    getting nowhere after a fair number of tries is withdrawn, which puts the
    board back on its hand-written setting and puts the host back on the
    panel's list of things to teach.

    "Nowhere" has to be measured in each mode's own unit — see
    `_PROGRESS_FLOOR`. Measuring everything in pages meant a scroll recipe and
    a url recipe were retired on their third visit no matter how well they
    worked, because neither can ever report more than one page. Every non-click
    recipe this system has ever written was withdrawn that way: it would learn
    a board, work correctly three times, and delete what it learned.
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
    got, floor = _progress(row.recipe or {}, pages_reached, batches)
    if got is None:
        # A url recipe. Counted so the panel can show it has been used, never
        # judged here — the check that matters for this mode is that the
        # parameter was already in the URL, and validation has done it.
        db.commit()
        return

    row.best_pages = max(row.best_pages or 0, got)
    # Three tries before judging: one visit can get nowhere because the board
    # had a single page of results that day, which is not the recipe's fault.
    if row.tries >= 3 and row.best_pages <= floor:
        row.status = "rejected"
        row.note = (
            f"Retired after {row.tries} visits that got nowhere. "
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
    # The normalised form, not the raw proposal: a budget that was clamped or a
    # page size that was inferred has to be what runs, or the recipe does one
    # thing and the panel says another.
    stored = outcome.get("recipe") or proposal["recipe"]
    row = save(db, host, stored, outcome,
               model=provider.model if provider else "")
    return {
        "ok": outcome["ok"],
        "reason": outcome["reason"],
        "recipe": stored,
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
