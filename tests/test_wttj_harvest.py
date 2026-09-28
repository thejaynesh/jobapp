"""
Welcome to the Jungle through the browser: its search results, read on the way
past, filed under its own name, and its search in the crawl plan.

The server cannot reach WTTJ at all — every page answers a datacenter IP with an
AWS WAF challenge — so this is the only route its listings have. The payload
below is modelled on the field names open-source readers of the same index use
(JobSpy's WTTJ scraper: `name`, `slug`, `organization.{name,slug}`, `office`,
`remote`, `objectID`), wrapped the way an Algolia multi-query answers. It was
not captured from a live page; if a real payload disagrees, the Harvest by site
panel on /runs shows it as "Forwarding, never finds jobs" with a sample kept.
"""

import pytest

from app.config import settings
from app.models.job import Job
from app.models.profile import Profile
from app.services import browse_plan, harvest

ALGOLIA = ("https://csekhvms53-dsn.algolia.net/1/indexes/*/queries"
           "?x-algolia-agent=Algolia%20for%20JavaScript")


def hit(slug="backend-engineer_paris", name="Backend Engineer", org="Doctolib",
        org_slug="doctolib", remote="partial", **extra):
    return {
        "objectID": f"{org_slug}-{slug}",
        "reference": f"REF_{slug}",
        "name": name,
        "slug": slug,
        "organization": {"name": org, "slug": org_slug,
                         "logo": {"url": "https://cdn.test/logo.png"}},
        "office": {"city": "Paris", "country": "France", "country_code": "FR"},
        "remote": remote,
        "contract_type": "full_time",
        "published_at": "2026-09-26T08:00:00Z",
        # What Algolia adds to every hit: the same fields again, as
        # highlighting objects. Not jobs, and must not read as any.
        "_highlightResult": {
            "name": {"value": f"<em>{name}</em>", "matchLevel": "full"},
            "organization": {"name": {"value": org, "matchLevel": "none"}},
        },
        **extra,
    }


def payload(*hits):
    return {"results": [{"hits": list(hits), "nbHits": len(hits), "page": 0,
                         "index": "wk_cms_jobs_production"}]}


class TestReadingAHit:
    def test_a_hit_becomes_a_job_with_an_address_built_from_its_slugs(self):
        (job,) = harvest.extract_jobs(payload(hit()), source=harvest.WTTJ_SOURCE)
        assert job["title"] == "Backend Engineer"
        assert job["company"] == "Doctolib"
        assert job["url"] == ("https://www.welcometothejungle.com/en/companies/"
                              "doctolib/jobs/backend-engineer_paris")
        assert job["source_job_id"] == "REF_backend-engineer_paris"
        assert job["location"] == "Paris, France"
        # "partial" is hybrid. A search for remote work should not get it.
        assert job["is_remote"] is False

    def test_only_full_remote_counts_as_remote(self):
        (job,) = harvest.extract_jobs(payload(hit(remote="full")),
                                      source=harvest.WTTJ_SOURCE)
        assert job["is_remote"] and job["location"] == "Remote (Paris, France)"

    def test_a_list_of_offices_is_read_when_there_is_no_single_one(self):
        one = hit()
        one.pop("office")
        one["offices"] = [{"city": "New York", "state": "NY", "country_code": "US"}]
        (job,) = harvest.extract_jobs(payload(one), source=harvest.WTTJ_SOURCE)
        assert job["location"] == "New York, NY, US"

    def test_a_page_of_hits_is_every_job_once(self):
        jobs = harvest.extract_jobs(
            payload(hit(), hit(slug="data-engineer", name="Data Engineer"), hit()),
            source=harvest.WTTJ_SOURCE)
        assert sorted(j["title"] for j in jobs) == ["Backend Engineer", "Data Engineer"]

    def test_a_hit_missing_a_slug_is_not_given_an_address(self):
        broken = hit()
        broken["organization"].pop("slug")
        assert harvest.extract_jobs(payload(broken), source=harvest.WTTJ_SOURCE) == []

    def test_another_boards_payload_is_never_given_wttj_addresses(self):
        # The same shape from anywhere else must not be rebuilt on WTTJ's
        # domain — that would be an address that does not exist.
        jobs = harvest.extract_jobs(payload(hit()), source="hiringcafe_harvest")
        assert not any("welcometothejungle.com" in j["url"] for j in jobs)


class TestFilingItUnderTheBoard:
    @pytest.mark.parametrize("url", [
        ALGOLIA,
        "https://csekhvms53-2.algolianet.com/1/indexes/*/queries",
        "https://www.welcometothejungle.com/en/jobs?query=backend",
        "https://app.welcometothejungle.com/jobs",
        "https://otta.com/jobs",
    ])
    def test_its_hosts_are_its_own_source(self, url):
        assert harvest.source_for_url(url) == harvest.WTTJ_SOURCE

    def test_a_harvested_page_of_results_is_stored(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")
        response = client.post(
            "/api/agent/harvest",
            json={"payload": payload(hit(), hit(slug="sre", name="SRE")),
                  "source_url": ALGOLIA},
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["found"] == 2 and body["source"] == harvest.WTTJ_SOURCE
        rows = db.query(Job).filter(Job.source == harvest.WTTJ_SOURCE).all()
        assert {r.title for r in rows} == {"Backend Engineer", "SRE"}
        assert all(r.url.startswith("https://www.welcometothejungle.com/en/companies/")
                   for r in rows)


PROFILE = {"target_roles": ["Backend Engineer"],
           "target_locations": ["London", "Remote", "Berlin"]}


def wttj_urls(urls):
    return [u for u in urls if "welcometothejungle.com" in u]


class TestTheCrawlPlan:
    def _reading(self, db, hosts):
        db.add(Profile(data={"agents": {"laptop": {"harvest_sites": hosts}}}))
        db.commit()

    def test_it_is_only_planned_for_a_browser_that_reads_it(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 1)
        self._reading(db, ["linkedin.com"])
        assert wttj_urls(browse_plan.search_urls(PROFILE, db=db)) == []

    def test_its_search_is_one_per_role_and_pages_from_one(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 3)
        self._reading(db, ["otta.com", "welcometothejungle.com"])
        urls = wttj_urls(browse_plan.search_urls(PROFILE, db=db))
        # Three locations in the profile, and still one search: the template
        # has no place in it, so each location would be the same URL again.
        assert len(urls) == 3
        assert urls[0] == "https://www.welcometothejungle.com/en/jobs?query=Backend+Engineer"
        assert "page=2" in urls[1] and "page=3" in urls[2]

    def test_a_board_that_does_take_a_location_still_gets_one_search_per_place(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_SEARCH_PAGES", 1)
        urls = [u for u in browse_plan.search_urls(PROFILE, db=db) if "linkedin.com" in u]
        assert len(urls) == 3
