"""
Mining company ATS boards out of the jobs a browser harvested.

This is the half of harvesting that compounds, and it was missing entirely —
`discover_from_jobs` existed, did exactly the right thing, and was never called
from the harvest path. Every browse so far saved postings whose URLs and
descriptions named Greenhouse and Lever boards, and threw all of them away.

The asymmetry is the whole point:

  A harvested posting is one job, once.
  A Greenhouse slug is that company's entire board — every role open now and
  every one opened later, with full descriptions, through a free API, on every
  future fetch cycle, with no browser involved.

Which is why `my.greenhouse.io` is worth crawling even though the Greenhouse
adapter already exists. That adapter is slug-driven; the aggregate board lists
postings across every company on the platform, so one pass over it is a slug
mine, and each slug is a permanent new source rather than a single row.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.models.company_board import CompanyBoard
from app.services import browse_plan
from app.services.harvest import save_harvested_jobs


def harvested(**overrides):
    job = {
        "source": "linkedin_harvest",
        "source_job_id": uuid.uuid4().hex[:10],
        "url": f"https://www.linkedin.com/jobs/view/{uuid.uuid4().int % 10**10}/",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "We are hiring.",
    }
    job.update(overrides)
    return job


def slugs(db, ats="greenhouse"):
    return sorted(
        row.slug for row in db.query(CompanyBoard).filter(CompanyBoard.ats == ats).all()
    )


class TestSlugsAreMinedFromHarvestedJobs:
    def test_a_greenhouse_apply_link_becomes_a_board(self, db):
        save_harvested_jobs(db, [harvested(
            url="https://boards.greenhouse.io/stripe/jobs/123",
        )])
        assert "stripe" in slugs(db)

    def test_a_slug_in_the_description_counts_too(self, db):
        # The description is where an aggregate board's card usually carries
        # the employer's real apply link.
        save_harvested_jobs(db, [harvested(
            description="Apply at https://job-boards.greenhouse.io/monzo/jobs/9",
        )])
        assert "monzo" in slugs(db)

    def test_the_embed_widget_form_is_recognised(self, db):
        save_harvested_jobs(db, [harvested(
            description="<iframe src='https://boards.greenhouse.io/embed/"
                        "job_board?for=figma'></iframe>",
        )])
        assert "figma" in slugs(db)

    def test_other_ats_platforms_are_mined_as_well(self, db):
        save_harvested_jobs(db, [harvested(
            description="https://jobs.lever.co/netflix/abc",
        )])
        assert "netflix" in slugs(db, ats="lever")

    def test_the_count_comes_back_with_the_harvest(self, db):
        counts = save_harvested_jobs(db, [harvested(
            url="https://boards.greenhouse.io/ramp/jobs/1",
        )])
        assert counts["boards"] == 1

    def test_several_jobs_naming_one_company_record_it_once(self, db):
        save_harvested_jobs(db, [
            harvested(url="https://boards.greenhouse.io/vercel/jobs/1"),
            harvested(url="https://boards.greenhouse.io/vercel/jobs/2"),
        ])
        assert slugs(db).count("vercel") == 1

    def test_a_job_that_names_no_board_records_none(self, db):
        save_harvested_jobs(db, [harvested()])
        assert slugs(db) == []


class TestItDoesNotBreakTheHarvest:
    def test_the_jobs_are_saved_even_when_mining_fails(self, db, monkeypatch):
        # A posting that was saved is saved. Failing to mine a slug out of it
        # is not a reason to lose the harvest that found it.
        from app.services import harvest as module

        monkeypatch.setattr(
            module, "_mine_ats_boards",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError):
            save_harvested_jobs(db, [harvested()])

    def test_a_broken_registry_costs_the_slug_not_the_batch(self, db, monkeypatch):
        import app.services.company_boards as boards

        monkeypatch.setattr(
            boards, "record_boards",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry down")),
        )
        counts = save_harvested_jobs(db, [harvested(
            url="https://boards.greenhouse.io/stripe/jobs/1",
        )])

        assert counts["inserted"] == 1
        assert counts["boards"] == 0

    def test_slugs_are_recorded_as_pending_not_trusted(self, db):
        # Nothing here validates. `validate_pending` checks a slug against the
        # live API before the fetch cycle uses it, which is the right place: a
        # wrong slug mined here should cost one 404 in a validation pass, not a
        # broken source.
        save_harvested_jobs(db, [harvested(
            url="https://boards.greenhouse.io/notreal/jobs/1",
        )])
        row = db.query(CompanyBoard).filter(CompanyBoard.slug == "notreal").one()
        assert row.origin == "harvest"


class TestTheGreenhouseAggregateBoard:
    def test_it_is_crawled_with_the_target_role_as_the_keyword(self, db):
        urls = browse_plan.search_urls(
            {"target_roles": ["Software Engineer"]}, depth=1,
        )
        greenhouse = [url for url in urls if "my.greenhouse.io" in url]
        assert len(greenhouse) == 1
        assert "query=Software+Engineer" in greenhouse[0]

    def test_the_location_filter_is_carried_through_untouched(self, db):
        # Not composed: the URL carries a place name, a latitude, a longitude
        # and a country code that all have to agree, so substituting a location
        # from the profile would give coordinates in Kansas labelled London.
        urls = browse_plan.search_urls(
            {"target_roles": ["Software Engineer"],
             "target_locations": ["London"]}, depth=1,
        )
        greenhouse = next(url for url in urls if "my.greenhouse.io" in url)
        assert "country_short_name=US" in greenhouse
        assert "London" not in greenhouse

    def test_the_url_is_a_setting_rather_than_a_constant(self, db, monkeypatch):
        monkeypatch.setattr(
            settings, "BROWSE_GREENHOUSE_FEED",
            "https://my.greenhouse.io/jobs/search?query={q}&location=Berlin",
        )
        urls = browse_plan.search_urls({"target_roles": ["Data Engineer"]}, depth=1)
        greenhouse = next(url for url in urls if "my.greenhouse.io" in url)

        assert "location=Berlin" in greenhouse
        assert "query=Data+Engineer" in greenhouse

    def test_a_url_without_a_keyword_is_opened_as_it_is(self, db, monkeypatch):
        # A filter set that needs no keyword — "everything posted today" — is
        # a legitimate crawl target, not a broken template.
        monkeypatch.setattr(
            settings, "BROWSE_GREENHOUSE_FEED",
            "https://my.greenhouse.io/jobs/search?date_posted=past_day",
        )
        urls = browse_plan.search_urls({"target_roles": ["Data Engineer"]}, depth=1)

        assert "https://my.greenhouse.io/jobs/search?date_posted=past_day" in urls

    def test_several_urls_can_be_configured(self, db, monkeypatch):
        monkeypatch.setattr(
            settings, "BROWSE_GREENHOUSE_FEED",
            "https://my.greenhouse.io/a?query={q},https://my.greenhouse.io/b?x=1",
        )
        urls = browse_plan.search_urls({"target_roles": ["SRE"]}, depth=1)

        assert any(url.startswith("https://my.greenhouse.io/a?query=SRE") for url in urls)
        assert "https://my.greenhouse.io/b?x=1" in urls

    def test_an_empty_setting_crawls_nothing_there(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BROWSE_GREENHOUSE_FEED", "")
        urls = browse_plan.search_urls({"target_roles": ["SRE"]}, depth=1)

        assert not any("my.greenhouse.io" in url for url in urls)

    def test_the_extension_is_allowed_to_read_it(self, db):
        # Crawling a page the interceptor is not registered on is real traffic
        # through a logged-in session that harvests nothing.
        source = open("extension/sites.js").read()
        assert "https://my.greenhouse.io/*" in source
