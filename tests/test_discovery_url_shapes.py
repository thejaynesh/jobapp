"""
Posting-link shapes discovery used to drop, each taken from a live
SimplifyJobs row (2026-09-28). Measured on that data, these were 207 of the
1,983 active new-grad and internship postings from the last 60 days, and 197
boards the lists name. No network.
"""

import httpx

from app.services import ats_validation, enrichment
from app.services.ats_discovery import extract_slugs
from app.services.sources import lever


def _workday(url):
    return extract_slugs(url).get("workday", set())


class TestWorkdaySites:
    def test_a_site_may_share_a_name_with_a_blocked_slug(self):
        """`careers` and `search` are refused as vendor slugs, not as sites."""
        assert _workday("https://theocc.wd5.myworkdayjobs.com/careers/job/Chicago/"
                        "Associate_REQ-4555-2") == {"theocc:wd5:careers"}
        assert _workday("https://expedia.wd108.myworkdayjobs.com/search/job/USA/"
                        "Graduate_R-98587-2") == {"expedia:wd108:search"}
        assert _workday("https://collegeboard.wd1.myworkdayjobs.com/en-US/Careers/job/"
                        "Remote/Software-Engineer-1_REQ002405-2") == {"collegeboard:wd1:Careers"}

    def test_a_one_character_site_is_a_site(self):
        assert _workday("https://citi.wd5.myworkdayjobs.com/2/job/Getzville/"
                        "Analyst_26935260") == {"citi:wd5:2"}

    def test_the_shared_host_gives_the_same_spec(self):
        for url in (
            "https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/AZ/Engineer_R248-26",
            "https://wd5.myworkdaysite.com/en-US/recruiting/microchiphr/External/job/AZ/X_R1",
        ):
            assert _workday(url) == {"microchiphr:wd5:External"}

    def test_workdays_own_paths_are_not_sites(self):
        assert _workday("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Ext/jobs") == set()
        assert _workday("https://acme.wd1.myworkdayjobs.com/robots.txt") == set()

    def test_the_tenant_is_still_judged_against_the_blocklist(self):
        assert _workday("https://linkedin.wd1.myworkdayjobs.com/Ext/job/X_R1") == set()


class TestEuBoards:
    def test_an_eu_greenhouse_board_is_a_greenhouse_board(self):
        found = extract_slugs("https://job-boards.eu.greenhouse.io/stubhubinc/jobs/4773787101")
        assert found["greenhouse"] == {"stubhubinc"}

    def test_an_eu_greenhouse_posting_is_read_from_the_api_that_serves_it(self):
        assert enrichment.looks_like_ats("https://job-boards.eu.greenhouse.io/mangroup/jobs/1")

    def test_an_eu_lever_board_is_a_lever_board(self):
        found = extract_slugs("https://jobs.eu.lever.co/cirrus/ec1787f8-e154-4162-bf36-7becd291674d")
        assert found["lever"] == {"cirrus"}


def _lever_api(monkeypatch, boards, calls):
    """US and EU Lever APIs, each knowing only its own boards."""
    def get(url, **kw):
        calls.append(url)
        region = "eu" if url.startswith("https://api.eu.lever.co/") else "us"
        slug = url.split("/v0/postings/", 1)[1].split("?", 1)[0]
        if boards.get(slug) != region:
            return httpx.Response(404, json={"ok": False}, request=httpx.Request("GET", url))
        return httpx.Response(200, json=[{
            "id": "p1", "text": "Software Engineer", "createdAt": None,
            "categories": {"location": "Austin, TX"},
            "hostedUrl": f"https://jobs.{'eu.' if region == 'eu' else ''}lever.co/{slug}/p1",
            "descriptionPlain": "Build things.",
        }], request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)


class TestLeverRegions:
    def test_an_eu_board_is_read_from_the_eu_api(self, monkeypatch):
        calls = []
        _lever_api(monkeypatch, {"cirrus": "eu"}, calls)
        jobs = lever.fetch(["cirrus"], max_age_days=0)
        assert [j["url"] for j in jobs] == ["https://jobs.eu.lever.co/cirrus/p1"]
        assert [c.split("/v0")[0] for c in calls] == ["https://api.lever.co",
                                                     "https://api.eu.lever.co"]

    def test_a_us_board_costs_one_request(self, monkeypatch):
        calls = []
        _lever_api(monkeypatch, {"netflix": "us"}, calls)
        assert len(lever.fetch(["netflix"], max_age_days=0)) == 1
        assert len(calls) == 1

    def test_the_probe_accepts_an_eu_board_and_refuses_a_missing_one(self, monkeypatch):
        _lever_api(monkeypatch, {"cirrus": "eu"}, [])
        assert ats_validation._probe_lever("cirrus") is True
        assert ats_validation._probe_lever("nobody") is False

    def test_an_eu_posting_is_enriched_from_the_eu_api(self):
        from unittest.mock import MagicMock

        posting = "ec1787f8-e154-4162-bf36-7becd291674d"
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"descriptionPlain": "Mixed-signal design.", "lists": []}
        client = MagicMock()
        client.get.return_value = resp
        found = enrichment.enrich_one(client, f"https://jobs.eu.lever.co/cirrus/{posting}")
        assert client.get.call_args[0][0] == f"https://api.eu.lever.co/v0/postings/cirrus/{posting}"
        assert "Mixed-signal design." in found.description


def test_icims_own_careers_host_needs_no_marker():
    assert extract_slugs("https://dish.jibeapply.com/jobs/98189")["jibe"] == {"dish.jibeapply.com"}


def test_common_crawl_walks_the_new_shapes():
    from app.services import commoncrawl

    names = {name for name, _ in commoncrawl.TARGETS}
    assert {"greenhouse-eu", "workday-shared", "jibe", "rippling", "pinpoint"} <= names
    found = commoncrawl.boards_in([
        "https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/AZ/X_R1",
        "https://job-boards.eu.greenhouse.io/mangroup/jobs/1",
    ])
    assert found == {"workday": {"microchiphr:wd5:External"}, "greenhouse": {"mangroup"}}
