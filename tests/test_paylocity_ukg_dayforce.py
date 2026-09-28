"""
Paylocity boards, and what can be read of UKG and Dayforce: their posting
pages, for enrichment and liveness. Shapes follow the live sites as read on
2026-09-28. No network.
"""

import json
from unittest.mock import MagicMock

import httpx
import pytest

from app.services import ats_validation, enrichment, liveness
from app.services.ats_discovery import extract_slugs
from app.services.ats_sniffer import company_host
from app.services.sources import paylocity

BOARD = "4caf1c72-b512-497b-b501-42f737c1dab7"


def pl_job(n, title="Software Developer IV", **over):
    return {"JobId": 4536000 + n, "JobTitle": title, "LocationName": "Twin Cities",
            "PublishedDate": "2026-09-25T09:18:23-05:00",
            "Description": "Who are we?Western National Insurance Group is a private mutual",
            "IsInternal": False, "HiringDepartment": "IT",
            "JobLocation": {"City": "Edina", "State": "MN", "Country": "USA"},
            "IsRemote": False, **over}


def board_page(jobs, title="Western National Group & Umialik Insurance"):
    data = {"ModuleId": 25030, "ModuleTitle": title, "Jobs": jobs, "Departments": []}
    return f"<html><script>window.pageData = {json.dumps(data)};</script></html>"


def serve_board(monkeypatch, pages, calls=None):
    """pages: company id → board page HTML; anything else is "job not found"."""
    def get(url, **kw):
        if calls is not None:
            calls.append(url)
        company = url.rsplit("/", 1)[1]
        if company in pages:
            return httpx.Response(200, text=pages[company], request=httpx.Request("GET", url))
        final = "https://recruiting.paylocity.com/Recruiting/Jobs/JobNotFound"
        return httpx.Response(200, text="<h1>Job Not Found</h1>", request=httpx.Request("GET", final))
    monkeypatch.setattr(httpx, "get", get)


class TestPaylocity:
    def test_a_board_is_read_whole_from_its_page(self, monkeypatch):
        serve_board(monkeypatch, {BOARD: board_page([pl_job(1)])})
        [job] = paylocity.fetch([BOARD])
        assert job["company"] == "Western National Group & Umialik Insurance"
        assert job["url"] == "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4536001"
        assert job["source_job_id"] == "4536001"
        assert job["location"] == "Edina, MN, United States"
        assert job["posted_at"] == "2026-09-25T09:18:23-05:00"
        assert job["description"].startswith("Who are we?")

    def test_interns_remote_and_internal_postings(self, monkeypatch):
        serve_board(monkeypatch, {BOARD: board_page([
            pl_job(1, "IT Data Engineering Intern"),
            pl_job(2, "Remote Developer", IsRemote=True),
            pl_job(3, "Internal Transfer Only", IsInternal=True),
        ])})
        jobs = {j["title"]: j for j in paylocity.fetch([BOARD])}
        assert jobs["IT Data Engineering Intern"]["employment_type"] == "internship"
        assert jobs["Remote Developer"]["is_remote"] is True
        assert "Internal Transfer Only" not in jobs

    def test_an_unknown_board_is_nothing(self, monkeypatch):
        serve_board(monkeypatch, {})
        assert paylocity.board("00000000-1111-2222-3333-444444444444") is None
        assert paylocity.fetch(["00000000-1111-2222-3333-444444444444"]) == []

    def test_something_that_is_not_a_company_id_is_never_requested(self, monkeypatch):
        calls = []
        serve_board(monkeypatch, {}, calls)
        assert paylocity.board("acme") is None and calls == []

    def test_the_probe_and_the_name(self, monkeypatch):
        serve_board(monkeypatch, {BOARD: board_page([])})
        assert ats_validation._probe_paylocity(BOARD) is True
        assert ats_validation._probe_paylocity("00000000-1111-2222-3333-444444444444") is False
        assert ats_validation._paylocity_name(BOARD) == "Western National Group & Umialik Insurance"

    def test_a_board_link_names_the_board_and_a_posting_link_does_not(self):
        assert extract_slugs(
            f"https://recruiting.paylocity.com/recruiting/jobs/All/{BOARD.upper()}/Western-National"
        )["paylocity"] == {BOARD}
        assert "paylocity" not in extract_slugs(
            "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4537849")

    def test_boards_are_polled_in_a_board_cycle(self, monkeypatch, db):
        from app.models.profile import Profile
        from app.services import job_fetcher, commoncrawl, tunables

        serve_board(monkeypatch, {BOARD: board_page([pl_job(1), pl_job(2)])})
        db.add(Profile(data={}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        _, stats = job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {"paylocity": [BOARD]}, {},
            only={"paylocity"})
        assert stats["paylocity"]["count"] == 2
        assert "paylocity" in job_fetcher.SOURCE_GROUPS["boards"]
        assert "paylocity" in {name for name, _ in commoncrawl.TARGETS}


@pytest.mark.parametrize("url", [
    "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4537849",
    "https://jobs.dayforcehcm.com/en-US/taiho/CANDIDATEPORTAL/jobs/4707",
    "https://recruiting.ultipro.com/ACA1001/JobBoard/f24b6286-a80b-4d02-e4e1-ad04762a00de/",
])
def test_a_host_every_customer_shares_is_not_one_employers_site(url):
    """The sniffer caches by host, so one customer's board would stand for all."""
    assert company_host(url) is None


def _client(text):
    resp = MagicMock(status_code=200, text=text)
    client = MagicMock()
    client.get.return_value = resp
    return client


UKG_URL = ("https://recruiting.ultipro.com/ACA1001/JobBoard/f24b6286-a80b-4d02-e4e1-ad04762a00de/"
           "OpportunityDetail?opportunityId=00c125be-14d2-471f-a2ab-280cce3dfb3e")


def ukg_page(closed=False, **over):
    data = {"Id": "00c125be-14d2-471f-a2ab-280cce3dfb3e", "Title": "Software Engineer I",
            "FullTime": True, "PostedDate": "2026-09-20T21:37:05.519Z",
            "Description": "<p>Build <b>claims</b> software.</p>",
            "Locations": [{"Address": {"City": "Detroit", "State": {"Code": "MI"},
                                       "Country": {"Name": "United States"}}}],
            "CompensationAnnualMinimum": 85000, "CompensationAnnualMaximum": 105000,
            "OpportunityIsClosed": closed, **over}
    return ("<script>var vm = new US.Opportunity.CandidateOpportunityDetail("
            + json.dumps(data, separators=(",", ":")) + ");</script>")


class TestUkg:
    def test_a_posting_page_is_read(self):
        client = _client(ukg_page())
        assert enrichment.looks_like_ats(UKG_URL)
        found = enrichment.enrich_one(client, UKG_URL)
        assert found.description == "Build claims software."
        assert found.posted_at == "2026-09-20T21:37:05.519Z"
        assert found.details == {"location": "Detroit, MI, United States",
                                 "employment_type": "full_time", "salary_min": 85000,
                                 "salary_max": 105000, "salary_currency": "USD",
                                 "salary_period": "YEAR"}
        # Only the posting page, which robots.txt allows; never JobBoardView.
        assert "JobBoardView" not in client.get.call_args[0][0]

    def test_a_closed_opportunity_is_closed(self):
        assert liveness.closed_marker(ukg_page(closed=True))
        assert not liveness.closed_marker(ukg_page(closed=False))


DAYFORCE_URL = "https://jobs.dayforcehcm.com/en-US/texasfarm/CANDIDATEPORTAL/jobs/634"


def dayforce_page(**extra):
    posting = {"jobPostingId": 634, "jobTitle": "Software Developer Intern",
               "postingStartTimestampUTC": "2026-03-09T05:00:00+00:00", **extra,
               "jobPostingContent": {"jobDescriptionHeader": "<p>The Voice of Texas Agriculture.</p>",
                                     "jobDescription": "<p>Write C# services.</p>",
                                     "jobDescriptionFooter": "<p>EOE.</p>"},
               "postingLocations": [{"cityName": "Waco", "stateCode": "TX", "isoCountryCode": "US"}],
               "jobPostingAttributes": [{"name": "EmploymentIndicator", "value": "Internship"}]}
    data = {"props": {"pageProps": {"dehydratedState": {"queries": [
        {"queryKey": ["site-info", {}], "state": {"data": {"jobBoardId": 1}}},
        {"queryKey": ["jobs", {}, {"id": "634"}], "state": {"data": posting}},
    ]}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def test_a_dayforce_posting_page_is_read():
    client = _client(dayforce_page())
    assert enrichment.looks_like_ats(DAYFORCE_URL)
    found = enrichment.enrich_one(client, DAYFORCE_URL)
    assert "The Voice of Texas Agriculture." in found.description
    assert "Write C# services." in found.description and "EOE." in found.description
    assert found.posted_at == "2026-03-09T05:00:00+00:00"
    assert found.details == {"location": "Waco, TX, US", "employment_type": "Internship"}
    assert client.get.call_args[0][0] == DAYFORCE_URL


class TestADayforcePostingPastItsExpiry:
    """Dayforce serves an expired posting whole, with a 200; its data says so."""

    def check(self, url=DAYFORCE_URL, **extra):
        response = MagicMock(status_code=200, text=dayforce_page(**extra), url=url,
                             headers={"content-type": "text/html; charset=utf-8"})
        client = MagicMock()
        client.get.return_value = response
        return liveness.check_url(url, client)

    def test_is_closed(self):
        result = self.check(postingExpiryTimestampUTC="2026-04-27T04:59:00+00:00",
                            isEvergreen=False)
        assert result.state == "closed" and "expired on Apr 27, 2026" in result.note

    def test_one_that_expires_later_is_open(self):
        assert self.check(postingExpiryTimestampUTC="2999-01-01T00:00:00+00:00").state == "open"

    def test_an_evergreen_posting_never_expires(self):
        assert self.check(postingExpiryTimestampUTC="2026-04-27T04:59:00+00:00",
                          isEvergreen=True).state == "open"

    def test_no_expiry_stated_is_open(self):
        assert self.check().state == "open"

    def test_only_dayforce_is_taken_at_its_word(self):
        result = self.check(url="https://careers.example.com/jobs/634",
                            postingExpiryTimestampUTC="2026-04-27T04:59:00+00:00")
        assert result.state == "open"

    def test_the_description_is_still_read(self):
        client = _client(dayforce_page(postingExpiryTimestampUTC="2026-04-27T04:59:00+00:00"))
        assert "Write C# services." in enrichment.enrich_one(client, DAYFORCE_URL).description


def test_a_closed_paylocity_posting_is_closed():
    assert liveness.closed_marker(
        "<h1>Job Not Found</h1><p>We're sorry, that job does not exist or is not currently "
        "active.</p>")
