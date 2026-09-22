"""
Pagination read from addresses, not guessed.

ZipRecruiter's fourth page is `/jobs-search/4?search=...`. Nothing could
represent a page number in the path, so the board was walked with a `?page=`
it ignores and every "page" was page one. These tests hold three things:

* the counter is found wherever it sits — path or query, ordinal or offset —
  from the board's own links or from an address you paste;
* the URLs built from it replace the counter rather than appending a second;
* teaching needs no model when an address says it all, and a model that is
  still asked hears what you told it.
"""

from unittest.mock import patch

from app.models.crawl_recipe import CrawlRecipe
from app.services import browse_plan, crawl_recipes

ZIP_PAGE_4 = (
    "https://www.ziprecruiter.com/jobs-search/4?search=software+engineer"
    "&location=San+Jose%2C+CA&radius=25&days=&lk=n2JvzZZ8hlhnC8GcNFcbbg"
)
ZIP_PAGE_1 = "https://www.ziprecruiter.com/jobs-search?search=se&location=SJ"


class TestFindingTheCounter:
    def test_a_page_number_in_the_path_is_found(self):
        out = crawl_recipes.infer([(None, ZIP_PAGE_4)])
        assert out["ok"]
        assert out["recipe"]["mode"] == "path"
        assert out["recipe"]["path_prefix"] == "/jobs-search"
        # radius=25 is a number too, and is not taken for the page.
        assert "radius" not in str(out["recipe"])

    def test_saying_which_page_it_is_removes_the_guess(self):
        guessed = crawl_recipes.infer([(None, ZIP_PAGE_4)])
        told = crawl_recipes.infer([(4, ZIP_PAGE_4)])
        assert "assumed" in guessed["reason"]
        assert "assumed" not in told["reason"]
        assert (told["recipe"]["page_base"], told["recipe"]["page_size"]) == (1, 1)

    def test_an_offset_is_told_apart_from_a_page_number(self):
        out = crawl_recipes.infer([(3, "https://x.com/jobs?q=a&start=20")])
        assert out["recipe"] == {**out["recipe"], "mode": "url",
                                 "page_param": "start", "page_base": 0,
                                 "page_size": 10}

    def test_the_page_links_say_where_page_two_is(self):
        controls = [
            {"tag": "a", "text": "2", "href": "/jobs-search/2?search=se"},
            {"tag": "a", "text": "3", "href": "/jobs-search/3?search=se"},
            {"tag": "a", "text": "Next", "href": "/jobs-search/2?search=se"},
        ]
        out = crawl_recipes.infer_from_links(ZIP_PAGE_1, controls)
        assert out["ok"] and "assumed" not in out["reason"]
        assert out["recipe"]["path_prefix"] == "/jobs-search"

    def test_links_that_disagree_with_their_labels_are_not_believed(self):
        controls = [
            {"tag": "a", "text": "2", "href": "/jobs/9913"},
            {"tag": "a", "text": "3", "href": "/jobs/4410"},
        ]
        assert not crawl_recipes.infer_from_links("https://x.com/jobs", controls)["ok"]


class TestBuildingPages:
    RECIPE = {"mode": "path", "path_prefix": "/jobs-search",
              "page_base": 1, "page_size": 1}

    def test_page_one_gains_the_counter(self):
        assert crawl_recipes.page_urls(ZIP_PAGE_1, self.RECIPE, 3) == [
            ZIP_PAGE_1,
            "https://www.ziprecruiter.com/jobs-search/2?search=se&location=SJ",
            "https://www.ziprecruiter.com/jobs-search/3?search=se&location=SJ",
        ]

    def test_a_later_page_carries_on_from_where_it_is(self):
        pages = crawl_recipes.page_urls(ZIP_PAGE_4, self.RECIPE, 3)
        assert "/jobs-search/5?" in pages[1] and "/jobs-search/6?" in pages[2]

    def test_a_query_counter_is_replaced_not_appended(self):
        recipe = {"mode": "url", "page_param": "page", "page_base": 1, "page_size": 1}
        pages = crawl_recipes.page_urls("https://x.com/s?q=a&page=2", recipe, 2)
        assert pages[1].count("page=") == 1 and "page=3" in pages[1]

    def test_ziprecruiter_pages_by_path_out_of_the_box(self):
        board = browse_plan.BOARDS_BY_KEY["ziprecruiter"]
        pages = board.pages(ZIP_PAGE_1, 2)
        assert "/jobs-search/2?" in pages[1]
        assert "page=" not in pages[1]

    def test_a_learned_path_recipe_drives_the_plan(self, db):
        crawl_recipes.save(db, "www.example.com",
                           {"mode": "path", "path_prefix": "/find",
                            "page_base": 1, "page_size": 1}, {"ok": True})
        pages = browse_plan._pages_for(db, None, "https://www.example.com/find?q=a", 3)
        assert pages[2] == "https://www.example.com/find/3?q=a"


class TestValidation:
    def test_a_path_that_is_not_the_pages_path_is_refused(self):
        out = crawl_recipes.validate({}, {"mode": "path", "path_prefix": "/elsewhere"},
                                     source_url=ZIP_PAGE_1)
        assert not out["ok"]

    def test_a_parameter_only_in_the_links_is_accepted(self):
        evidence = {"query": {"q": "a"},
                    "controls": [{"tag": "a", "text": "2", "href": "?q=a&pg=2"}]}
        out = crawl_recipes.validate(evidence, {"mode": "url", "page_param": "pg",
                                                "page_base": 1, "page_size": 1})
        assert out["ok"]


class TestTeaching:
    def test_a_pasted_address_is_learned_without_a_model(self, db):
        with patch("app.services.crawl_recipes.propose") as model:
            out = crawl_recipes.learn(db, "", examples=[(4, ZIP_PAGE_4)])
        model.assert_not_called()
        assert out["ok"]
        row = db.query(CrawlRecipe).filter_by(host="www.ziprecruiter.com").one()
        assert row.status == "active" and row.recipe["mode"] == "path"

    def test_the_note_reaches_the_model_when_one_is_needed(self, db):
        crawl_recipes.record(db, "board.test", "https://board.test/s",
                             {"query": {}, "controls": [], "scroll": {}})
        captured = {}

        def fake_call(profile_data, role, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return '{"mode": "scroll"}'

        with patch("app.services.model_roles.call", side_effect=fake_call):
            crawl_recipes.learn(db, "board.test", hint="click the little arrow")
        assert "click the little arrow" in captured["prompt"]

    def test_pasted_lines_can_say_their_page(self):
        parsed = crawl_recipes.parse_examples(
            f"{ZIP_PAGE_4} page 4\nhttps://x.com/a?p=2\nnot a url", page=None)
        assert parsed == [(4, ZIP_PAGE_4), (None, "https://x.com/a?p=2")]

    def test_the_page_box_applies_to_the_first_address(self):
        assert crawl_recipes.parse_examples(ZIP_PAGE_4, page=4)[0][0] == 4

    def test_a_visit_whose_links_say_how_it_pages_is_learned_on_its_own(self, db):
        evidence = {"query": {"search": "se"}, "scroll": {},
                    "controls": [
                        {"tag": "a", "text": "2", "href": "/jobs-search/2?search=se"},
                        {"tag": "a", "text": "3", "href": "/jobs-search/3?search=se"},
                    ]}
        assert crawl_recipes.learn_automatically(
            db, "www.ziprecruiter.com", ZIP_PAGE_1, evidence)
        assert crawl_recipes.active_for(db, "www.ziprecruiter.com")["mode"] == "path"

    def test_the_teach_form_says_what_it_learned(self, client, db):
        body = client.post("/runs/agent/learn-crawl",
                           data={"examples": ZIP_PAGE_4, "page": "4"}).text
        assert "www.ziprecruiter.com" in body
        assert "/jobs-search/N" in body
