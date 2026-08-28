"""
Working out how to walk a board, instead of being told board by board.

`harvest_recipes` learns where the jobs are in a payload. This learns the step
before it: how to make the page show more of them.

The motivating complaint was exactly right — every board's pagination was
hand-written in `browse_plan.BOARDS`, so each new site meant telling somebody,
and a board that changed its pagination silently yielded page one forever while
every number on the panel looked healthy.

What this file mostly defends is the *validation*, because the failure modes
here are worse than for extraction. A wrong extraction recipe stores a bad
company name in a row you can fix. A wrong click happens on a logged-in job
board, under your account, and some of those pages have "Withdraw application"
on them. So a proposal has to match something the page actually offered, that
something has to read like a page control, and anything reading like an action
is refused whatever else is true of it.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.services import crawl_recipes

# What the extension sends back from a board with numbered buttons.
EVIDENCE = {
    "query": {"q": "software engineer"},
    "scroll": {"passes": 8, "batches": 1, "doc_height": 2400,
               "client_height": 900},
    "controls": [
        {"tag": "button", "text": "1", "aria": "", "cls": "page-btn current",
         "rel": "", "id": "", "testid": "", "href": "", "disabled": False},
        {"tag": "button", "text": "2", "aria": "", "cls": "page-btn",
         "rel": "", "id": "", "testid": "", "href": "", "disabled": False},
        {"tag": "button", "text": "Next", "aria": "Next page",
         "cls": "page-next", "rel": "", "id": "", "testid": "pagination-next",
         "href": "", "disabled": False},
    ],
}


def _sample(db, host="hiring.cafe", evidence=None, url=None):
    return crawl_recipes.record(
        db, host, url or f"https://{host}/", evidence or EVIDENCE,
        pages_reached=1, batches=1,
    )


class TestKeepingTheEvidence:
    def test_a_sample_is_stored_against_its_host(self, db):
        row = _sample(db)
        assert row.host == "hiring.cafe"
        assert crawl_recipes.latest_sample(db, "hiring.cafe").id == row.id

    def test_only_the_last_few_are_kept(self, db):
        """
        Evidence for a decision, not an archive. The newest one describes the
        page as it is now, which is the only version a recipe can be written
        against — an older one describes a layout that may no longer exist.
        """
        from app.models.crawl_recipe import CrawlSample

        for _ in range(6):
            _sample(db)
        assert db.query(CrawlSample).filter(
            CrawlSample.host == "hiring.cafe").count() <= 3

    def test_junk_is_not_stored(self, db):
        assert crawl_recipes.record(db, "", "https://x/", EVIDENCE) is None
        assert crawl_recipes.record(db, "x.com", "https://x/", None) is None

    def test_a_host_with_evidence_and_no_recipe_is_offered(self, db):
        _sample(db)
        assert "hiring.cafe" in crawl_recipes.hosts_needing_a_recipe(db)

    def test_a_host_with_a_recipe_is_not_offered_again(self, db):
        _sample(db)
        crawl_recipes.save(
            db, "hiring.cafe",
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": 10},
            {"ok": True, "reason": "fine"},
        )
        assert "hiring.cafe" not in crawl_recipes.hosts_needing_a_recipe(db)


class TestRefusingAProposalThatWouldClickTheWrongThing:
    """
    The reason this validates at all rather than trusting the answer. A click
    recipe presses a real button on a real board under a real login, and that
    is not undone by re-running the crawl.
    """

    def test_a_selector_matching_a_destructive_control_is_refused(self):
        evidence = {"controls": [
            {"tag": "button", "text": "Withdraw application",
             "cls": "danger-action", "aria": ""},
        ]}
        outcome = crawl_recipes.validate(
            evidence, {"mode": "click", "selector": ".danger-action",
                       "max_pages": 5},
        )
        assert not outcome["ok"]
        assert "action" in outcome["reason"].lower()

    def test_one_dangerous_match_condemns_the_whole_selector(self):
        """
        Even alongside a legitimate one. A selector loose enough to catch both
        a page button and a destructive button will eventually catch the wrong
        one first, and "usually right" is not a standard that applies here.
        """
        evidence = {"controls": [
            {"tag": "button", "text": "Next", "cls": "btn", "aria": ""},
            {"tag": "button", "text": "Delete", "cls": "btn", "aria": ""},
        ]}
        outcome = crawl_recipes.validate(
            evidence, {"mode": "click", "selector": ".btn", "max_pages": 5},
        )
        assert not outcome["ok"]

    def test_a_selector_matching_nothing_on_the_page_is_refused(self):
        outcome = crawl_recipes.validate(
            EVIDENCE, {"mode": "click", "selector": ".invented-classname",
                       "max_pages": 5},
        )
        assert not outcome["ok"]
        assert "matches nothing" in outcome["reason"]

    def test_a_selector_matching_something_unpaginated_is_refused(self):
        evidence = {"controls": [
            {"tag": "a", "text": "Help", "cls": "footer-link", "aria": ""},
        ]}
        outcome = crawl_recipes.validate(
            evidence, {"mode": "click", "selector": ".footer-link",
                       "max_pages": 5},
        )
        assert not outcome["ok"]

    def test_selector_syntax_we_will_not_run_is_refused(self):
        outcome = crawl_recipes.validate(
            EVIDENCE, {"mode": "click", "selector": "button:has(> .x)",
                       "max_pages": 5},
        )
        assert not outcome["ok"]
        assert "syntax" in outcome["reason"]

    def test_a_good_next_control_is_accepted(self):
        outcome = crawl_recipes.validate(
            EVIDENCE,
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": 10},
        )
        assert outcome["ok"]

    def test_an_absurd_page_count_is_refused_rather_than_trimmed(self):
        """
        Refused, not clamped. A proposal outside these bounds did not
        understand the page, and quietly trimming it into range hides that —
        the recipe goes active, does nothing useful, and reads as the board
        having changed.
        """
        outcome = crawl_recipes.validate(
            EVIDENCE,
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": 5000},
        )
        assert not outcome["ok"]


class TestRefusingAnImplausibleUrlRecipe:
    def test_a_parameter_the_url_does_not_have_is_refused(self):
        """
        The failure this prevents is the quiet one. Inventing `?page=2` on a
        board that pages by `start` produces a URL that returns page one — so
        the crawl queues five visits, harvests the same rows five times, and
        reports five pages of depth.
        """
        outcome = crawl_recipes.validate(
            EVIDENCE,
            {"mode": "url", "page_param": "page", "page_size": 25,
             "page_base": 1},
        )
        assert not outcome["ok"]
        assert "not in this URL" in outcome["reason"]

    def test_a_parameter_the_url_does_have_is_accepted(self):
        evidence = dict(EVIDENCE, query={"q": "engineer", "start": "0"})
        outcome = crawl_recipes.validate(
            evidence,
            {"mode": "url", "page_param": "start", "page_size": 25,
             "page_base": 0},
        )
        assert outcome["ok"]

    def test_a_nonsense_parameter_name_is_refused(self):
        evidence = dict(EVIDENCE, query={"?? bad": "1"})
        outcome = crawl_recipes.validate(
            evidence,
            {"mode": "url", "page_param": "?? bad", "page_size": 25},
        )
        assert not outcome["ok"]

    def test_an_impossible_page_base_is_refused(self):
        evidence = dict(EVIDENCE, query={"page": "1"})
        outcome = crawl_recipes.validate(
            evidence,
            {"mode": "url", "page_param": "page", "page_size": 25,
             "page_base": 7},
        )
        assert not outcome["ok"]


class TestScrollRecipes:
    def test_a_sane_depth_is_accepted(self):
        outcome = crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": 150})
        assert outcome["ok"]

    def test_an_absurd_depth_is_refused(self):
        outcome = crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": 100000})
        assert not outcome["ok"]

    def test_a_mode_we_do_not_know_is_refused(self):
        # A closed set, so a model answering "infinite-scroll" is caught here
        # rather than silently doing nothing at crawl time.
        outcome = crawl_recipes.validate(EVIDENCE, {"mode": "infinite-scroll"})
        assert not outcome["ok"]

    def test_a_non_object_is_refused(self):
        assert not crawl_recipes.validate(EVIDENCE, None)["ok"]
        assert not crawl_recipes.validate(EVIDENCE, ["scroll"])["ok"]


class TestAnAnswerWeAskedForIsNotAnAnswerWeRefuse:
    """
    The rejections that were our fault rather than the model's.

    The prompt says "include only the keys for the mode you chose" and then
    lists seven keys. A model choosing scroll answers `{"mode": "scroll"}` —
    complete, correct, and the literal thing we asked for — and got back
    "scroll_passes must be 1..300", which reads like it said something absurd
    when it said nothing at all.

    The same shape of mistake sat under every number in here: JSON from a model
    arrives as `"150"` and `150.0` about as readily as `150`, and treating the
    transport as a misunderstanding of the page refused sound proposals.
    """

    def test_scroll_with_no_depth_is_a_complete_answer(self):
        # The crawler decides the depth anyway: browse_plan falls back to the
        # board's own setting when a scroll recipe does not name one. So this
        # says the only thing that matters — there is no second page.
        assert crawl_recipes.validate(EVIDENCE, {"mode": "scroll"})["ok"]

    def test_a_depth_that_is_present_and_absurd_is_still_refused(self):
        # The distinction the fix rests on. Absent is not wrong; wrong is wrong.
        assert not crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": 100000})["ok"]

    def test_a_number_sent_as_a_string_is_read_as_a_number(self):
        assert crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": "150"})["ok"]

    def test_a_whole_number_sent_as_a_float_is_read_as_a_number(self):
        assert crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": 150.0})["ok"]

    def test_a_fractional_depth_is_refused(self):
        # Not a transport artefact — nothing sane produces 2.5 scroll passes.
        assert not crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": 2.5})["ok"]

    def test_a_boolean_is_not_a_number(self):
        # Python would otherwise take True as 1 and accept a nonsense recipe.
        assert not crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": True})["ok"]

    def test_prose_where_a_number_belongs_is_refused(self):
        assert not crawl_recipes.validate(
            EVIDENCE, {"mode": "scroll", "scroll_passes": "as many as needed"},
        )["ok"]

    def test_a_url_recipe_may_leave_the_arithmetic_out(self):
        # It named the mechanism, which is the hard part. browse_plan uses the
        # same defaults this stands in, so the two cannot disagree.
        evidence = dict(EVIDENCE, query={"q": "engineer", "start": "0"})
        assert crawl_recipes.validate(
            evidence, {"mode": "url", "page_param": "start"})["ok"]

    def test_a_click_recipe_may_leave_the_page_count_out(self):
        assert crawl_recipes.validate(
            EVIDENCE,
            {"mode": "click", "selector": "[data-testid='pagination-next']"},
        )["ok"]

    def test_a_stringly_typed_page_count_is_read(self):
        assert crawl_recipes.validate(
            EVIDENCE,
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": "10"},
        )["ok"]


class TestControlsThatGoTheWrongWay:
    """
    The extension collects "Previous" and "First" now, because their presence
    is what tells a model it is looking at a pagination row rather than a
    footer. They are evidence, never a target.

    That distinction has to be enforced here or the widening is a regression:
    the extension clicks the *first* match, so a selector loose enough to catch
    the whole row would walk backwards through results already harvested and
    report depth for it.
    """

    ROW = {"controls": [
        {"tag": "button", "text": "Previous", "aria": "Previous page",
         "cls": "pg", "rel": "", "id": "", "testid": "", "href": "",
         "disabled": True},
        {"tag": "button", "text": "2", "aria": "", "cls": "pg", "rel": "",
         "id": "", "testid": "", "href": "", "disabled": False},
        {"tag": "button", "text": "Next", "aria": "Next page", "cls": "pg-next",
         "rel": "", "id": "", "testid": "", "href": "", "disabled": False},
    ], "query": {}, "scroll": {"passes": 4, "batches": 0}}

    def test_a_selector_catching_the_whole_row_is_refused(self):
        outcome = crawl_recipes.validate(
            self.ROW, {"mode": "click", "selector": ".pg", "max_pages": 5})
        assert not outcome["ok"]
        assert "back a page" in outcome["reason"]

    def test_the_forward_control_on_its_own_is_still_accepted(self):
        assert crawl_recipes.validate(
            self.ROW,
            {"mode": "click", "selector": ".pg-next", "max_pages": 5},
        )["ok"]


class TestLabelsRealBoardsActuallyUse:
    """
    "Next" on its own is the rare case. The accept list knew only that, so a
    board labelling its control "Next results" or "Load more jobs" had a
    perfectly good proposal refused for a control the page plainly offered.
    """

    def _row(self, label):
        return {"controls": [
            {"tag": "button", "text": label, "aria": "", "cls": "go",
             "rel": "", "id": "", "testid": "", "href": "", "disabled": False},
        ], "query": {}, "scroll": {"passes": 4, "batches": 0}}

    @pytest.mark.parametrize("label", [
        "Next", "Next page", "Next results", "Next jobs", "2", "Page 2",
        "Load more", "Load more jobs", "Show more", "See more results",
        "More results", "›", "»", "→", ">", "...",
    ])
    def test_it_reads_like_a_page_control(self, label):
        outcome = crawl_recipes.validate(
            self._row(label), {"mode": "click", "selector": ".go",
                               "max_pages": 5})
        assert outcome["ok"], f"{label!r} was refused: {outcome['reason']}"

    @pytest.mark.parametrize("label", [
        "Help", "Save this search", "Sign in", "About us", "Filters",
    ])
    def test_it_does_not(self, label):
        assert not crawl_recipes.validate(
            self._row(label), {"mode": "click", "selector": ".go",
                               "max_pages": 5})["ok"]

    @pytest.mark.parametrize("label", [
        "Withdraw application", "Delete", "Apply now", "Submit",
    ])
    def test_an_action_is_refused_however_it_is_worded(self, label):
        outcome = crawl_recipes.validate(
            self._row(label), {"mode": "click", "selector": ".go",
                               "max_pages": 5})
        assert not outcome["ok"]

    def test_the_extension_collects_everything_this_accepts(self):
        """
        The two lists are in different languages in different files, and a
        label the extension drops is a label no model ever sees. That is the
        failure this pairs with: the evidence went missing one layer before the
        rejection, so the panel blamed the model for an empty list.
        """
        import re

        source = open("extension/background.js").read()
        # The collection filter, as written in the content script.
        pattern = re.search(r"if \(!/\^\((.+?)\)\$/i\n?\s*\.test\(label\)\)",
                            source, re.S)
        assert pattern, "could not find the control filter in background.js"
        collected = pattern.group(1)
        for label in ("next", "page", "load", "show", "more", "older"):
            assert label in collected.lower(), (
                f"the extension drops {label!r}, which validate() accepts"
            )


class TestStoringWhatWasLearned:
    def test_a_valid_recipe_goes_active(self, db):
        row = crawl_recipes.save(
            db, "hiring.cafe",
            {"mode": "click", "selector": "[data-testid='pagination-next']"},
            {"ok": True, "reason": "fine"},
        )
        assert row.status == "active"
        assert crawl_recipes.active_for(db, "hiring.cafe")["mode"] == "click"

    def test_a_refused_recipe_is_kept_but_not_used(self, db):
        # Kept because the note says what it got wrong, which is the only
        # useful thing about a bad proposal.
        row = crawl_recipes.save(
            db, "hiring.cafe", {"mode": "click", "selector": ".nope"},
            {"ok": False, "reason": "matches nothing"},
        )
        assert row.status == "rejected"
        assert crawl_recipes.active_for(db, "hiring.cafe") is None

    def test_a_new_recipe_retires_the_old_one(self, db):
        # One active per host, enforced by a partial unique index — without
        # retiring the incumbent first the insert fails outright.
        crawl_recipes.save(db, "hiring.cafe", {"mode": "scroll",
                                               "scroll_passes": 50},
                           {"ok": True, "reason": "a"})
        crawl_recipes.save(db, "hiring.cafe", {"mode": "scroll",
                                               "scroll_passes": 150},
                           {"ok": True, "reason": "b"})
        assert crawl_recipes.active_for(db, "hiring.cafe")["scroll_passes"] == 150

    def test_an_unknown_host_has_no_recipe(self, db):
        assert crawl_recipes.active_for(db, "nowhere.example") is None
        assert crawl_recipes.active_for(db, "") is None


class TestARecipeThatDoesNotActuallyWork:
    """
    The half validation cannot do. A recipe is checked against a snapshot of
    the page, and a snapshot cannot say whether clicking that control advances
    anything — only a visit can.
    """

    def _active(self, db):
        crawl_recipes.save(
            db, "hiring.cafe",
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": 10},
            {"ok": True, "reason": "fine"},
        )

    def test_visits_are_counted_against_it(self, db):
        self._active(db)
        crawl_recipes.note_outcome(db, "hiring.cafe", 4)
        assert crawl_recipes.listing(db)[0].tries == 1
        assert crawl_recipes.listing(db)[0].best_pages == 4

    def test_one_bad_visit_does_not_condemn_it(self, db):
        # A board can genuinely have one page of results on a given day, and
        # that is not the recipe's fault.
        self._active(db)
        crawl_recipes.note_outcome(db, "hiring.cafe", 1)
        assert crawl_recipes.active_for(db, "hiring.cafe") is not None

    def test_it_retires_itself_after_several(self, db):
        self._active(db)
        for _ in range(3):
            crawl_recipes.note_outcome(db, "hiring.cafe", 1)
        assert crawl_recipes.active_for(db, "hiring.cafe") is None

    def test_a_recipe_that_works_is_left_alone(self, db):
        self._active(db)
        for _ in range(5):
            crawl_recipes.note_outcome(db, "hiring.cafe", 6)
        assert crawl_recipes.active_for(db, "hiring.cafe") is not None

    def test_retiring_puts_the_host_back_on_the_list(self, db):
        # So it can be taught again, rather than being stuck with a recipe
        # that does nothing and never appearing again.
        _sample(db)
        self._active(db)
        for _ in range(3):
            crawl_recipes.note_outcome(db, "hiring.cafe", 1)
        assert "hiring.cafe" in crawl_recipes.hosts_needing_a_recipe(db)

    def test_grading_a_host_with_no_recipe_is_harmless(self, db):
        crawl_recipes.note_outcome(db, "nowhere.example", 1)


class TestTheCrawlUsesWhatWasLearned:
    def test_a_click_recipe_sets_the_page_count(self, db):
        from app.services import browse_plan

        crawl_recipes.save(
            db, "hiring.cafe",
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": 7},
            {"ok": True, "reason": "fine"},
        )
        assert browse_plan._max_pages("https://hiring.cafe/", db) == 7

    def test_the_learned_selector_reaches_the_task(self, db, monkeypatch):
        from app.config import settings
        from app.models.browser_task import BrowserTask
        from app.services import browse_plan

        monkeypatch.setattr(settings, "BROWSE_PAUSED_HOSTS", "")
        crawl_recipes.save(
            db, "hiring.cafe",
            {"mode": "click", "selector": "[data-testid='pagination-next']",
             "max_pages": 7},
            {"ok": True, "reason": "fine"},
        )
        browse_plan.enqueue(db, ["https://hiring.cafe/"],
                            priority=browse_plan.PRIORITY_REQUESTED)
        task = db.query(BrowserTask).filter(
            BrowserTask.kind == "browse_page").one()
        assert task.payload["click_selector"] == "[data-testid='pagination-next']"
        assert task.payload["max_pages"] == 7

    def test_a_scroll_recipe_sets_the_depth(self, db):
        from app.services import browse_plan

        crawl_recipes.save(
            db, "hiring.cafe", {"mode": "scroll", "scroll_passes": 180},
            {"ok": True, "reason": "fine"},
        )
        assert browse_plan._scroll_passes("https://hiring.cafe/", db) == 180

    def test_a_recipe_overrides_the_hand_written_board(self, db):
        """
        The board entry is a guess made once by whoever added the site. The
        recipe was written against the page as it is now and withdraws itself
        when it stops working, so when they disagree the newer one wins.
        """
        from app.services import browse_plan

        assert browse_plan._max_pages("https://hiring.cafe/", db) > 1
        crawl_recipes.save(
            db, "hiring.cafe", {"mode": "scroll", "scroll_passes": 120},
            {"ok": True, "reason": "fine"},
        )
        assert browse_plan._max_pages("https://hiring.cafe/", db) == 1

    def test_a_url_recipe_gives_depth_to_a_board_that_had_none(self, db):
        # Entry-page boards were added with no page parameter at all, so they
        # were one page each however much the board held.
        from app.services import browse_plan

        crawl_recipes.save(
            db, "hiring.cafe",
            {"mode": "url", "page_param": "page", "page_size": 1,
             "page_base": 1},
            {"ok": True, "reason": "fine"},
        )
        board = browse_plan.BOARDS_BY_KEY["hiringcafe"]
        pages = browse_plan._pages_for(db, board, "https://hiring.cafe/", 3)
        assert pages == [
            "https://hiring.cafe/",
            "https://hiring.cafe/?page=2",
            "https://hiring.cafe/?page=3",
        ]

    def test_a_board_with_no_recipe_is_untouched(self, db):
        from app.services import browse_plan

        assert browse_plan._click_selector(
            "https://my.greenhouse.io/jobs/search", db) == ""
        assert browse_plan._max_pages(
            "https://my.greenhouse.io/jobs/search", db) == 1

    def test_the_lookup_survives_a_missing_table(self, db, monkeypatch):
        """
        On a deploy where the migration has not run, a failed statement aborts
        the whole enclosing transaction — so catching the error is not enough.
        Everything the caller does afterwards would fail too, with an error
        naming a query that had nothing to do with it.
        """
        from app.services import browse_plan
        from app.models.browser_task import BrowserTask

        monkeypatch.setattr(
            crawl_recipes, "active_for",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no such table")),
        )
        assert browse_plan._max_pages("https://hiring.cafe/", db) > 1
        # The session still works, which is the actual claim.
        assert db.query(BrowserTask).count() == 0
