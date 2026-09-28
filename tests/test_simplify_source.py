"""
SimplifyJobs' listings file: as a job source, and as a list of company boards.

Rows are shaped like `.github/scripts/listings.json` in SimplifyJobs'
New-Grad-Positions repo as read on 2026-09-28. No network — `httpx.get` is
answered here.
"""

import time

import httpx
import pytest

from app.models.company_board import CompanyBoard
from app.models.profile import Profile
from app.services import company_boards, job_fetcher, tunables
from app.services.ats_discovery import harvest_boards_from_lists
from app.services.sources import simplify

NOW = time.time()
DAY = 86400


def row(n, **over):
    base = {
        "source": "Simplify", "category": "Software", "id": f"id-{n}",
        "company_name": "Rockwell Automation", "title": "Software Engineer I",
        "active": True, "is_visible": True, "sponsorship": "Other",
        "date_posted": int(NOW - 2 * DAY), "date_updated": int(NOW - DAY),
        "url": ("https://rockwellautomation.wd1.myworkdayjobs.com/External-Early-Careers"
                f"/job/Mayfield-Heights-Ohio/Software-Engineer_R26-{n}"),
        "locations": ["Mayfield Heights, OH"], "degrees": ["Bachelor's"],
    }
    return {**base, **over}


def serve(monkeypatch, files: dict, calls: list | None = None):
    def get(url, **kw):
        if calls is not None:
            calls.append(url)
        if url not in files:
            return httpx.Response(404, text="nope", request=httpx.Request("GET", url))
        body = files[url]
        if isinstance(body, str):
            return httpx.Response(200, text=body, request=httpx.Request("GET", url))
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)


GRAD = "https://lists.test/New-Grad/listings.json"
INTERN = "https://lists.test/Summer2026-Internships/listings.json"


class TestAsAJobSource:
    def test_active_rows_become_dated_jobs(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1), row(2, active=False), row(3, is_visible=False)]})
        (job,) = simplify.fetch([GRAD])
        assert job["source"] == "simplify" and job["source_job_id"] == "id-1"
        assert job["company"] == "Rockwell Automation"
        assert job["location"] == "Mayfield Heights, OH"
        assert job["url"].startswith("https://rockwellautomation.wd1.myworkdayjobs.com/")
        assert job["experience_level"] == "entry"
        assert job["posted_at"].startswith(time.strftime("%Y-%m-%d", time.gmtime(NOW - 2 * DAY)))

    def test_simplifys_markers_come_off_the_link_and_the_address_stays(self):
        assert simplify.clean_url(
            "https://careers.amd.com/jobs/88877?icims=1") == "https://careers.amd.com/jobs/88877"
        assert simplify.clean_url(
            "https://careers.withwaymo.com/jobs?gh_jid=7488508&utm_source=Simplify&ref=Simplify"
        ) == "https://careers.withwaymo.com/jobs?gh_jid=7488508"
        assert simplify.clean_url(
            "https://jobs.ashbyhq.com/flint/39f9/application?embed=true"
        ) == "https://jobs.ashbyhq.com/flint/39f9/application"

    def test_rows_older_than_the_age_window_are_not_handed_on(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1), row(2, date_posted=int(NOW - 60 * DAY))]})
        assert [j["source_job_id"] for j in simplify.fetch([GRAD], max_age_days=30)] == ["id-1"]
        assert len(simplify.fetch([GRAD], max_age_days=0)) == 2

    def test_internship_files_mark_the_employment_type(self, monkeypatch):
        serve(monkeypatch, {INTERN: [row(1, title="Software Engineering Intern")]})
        (job,) = simplify.fetch([INTERN])
        assert job["employment_type"] == "internship"

    def test_a_senior_title_is_not_called_entry_level(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1, title="Senior Software Engineer")]})
        (job,) = simplify.fetch([GRAD])
        assert job["experience_level"] == "senior"

    def test_several_places_are_kept(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1, locations=["New York, NY", "Boston, MA"])]})
        (job,) = simplify.fetch([GRAD])
        assert job["location"] == "New York, NY; Boston, MA"

    def test_a_missing_file_costs_that_file_only(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1)]})
        jobs = simplify.fetch(["https://lists.test/gone.json", GRAD])
        assert len(jobs) == 1

    def test_the_same_row_in_two_files_is_one_job(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1)], INTERN: [row(1)]})
        assert len(simplify.fetch([GRAD, INTERN])) == 1


class TestAsAListOfBoards:
    def test_every_row_active_or_not_names_its_board_and_company(self, monkeypatch):
        serve(monkeypatch, {GRAD: [
            row(1),
            row(2, active=False, company_name="Ionq",
                url="https://job-boards.greenhouse.io/ionq/jobs/123"),
        ]})
        found, names = harvest_boards_from_lists([GRAD])
        assert found["workday"] == {"rockwellautomation:wd1:External-Early-Careers"}
        assert found["greenhouse"] == {"ionq"}
        assert names[("greenhouse", "ionq")] == "Ionq"

    def test_a_readme_is_read_as_text(self, monkeypatch):
        serve(monkeypatch, {"https://lists.test/README.md":
                            "| Acme | [Apply](https://jobs.lever.co/acme/abc) |"})
        found, names = harvest_boards_from_lists(["https://lists.test/README.md"])
        assert found == {"lever": {"acme"}} and names == {}

    def test_nothing_is_capped(self, monkeypatch):
        rows = [row(n, url=f"https://t{n}.wd5.myworkdayjobs.com/Ext/job/x_{n}") for n in range(40)]
        serve(monkeypatch, {GRAD: rows})
        found, _ = harvest_boards_from_lists([GRAD])
        # The old harvest kept fifteen Workday tenants, forever.
        assert len(found["workday"]) == 40

    def test_a_failed_list_is_skipped(self, monkeypatch):
        serve(monkeypatch, {GRAD: [row(1)]})
        found, _ = harvest_boards_from_lists(["https://lists.test/404.md", GRAD])
        assert "workday" in found

    def test_the_registry_files_each_board_under_its_own_company(self, db):
        company_boards.record_boards(
            db, {"greenhouse": {"ionq", "acme"}}, origin="list", revive=False,
            names={("greenhouse", "ionq"): "IonQ"})
        ionq = db.query(CompanyBoard).filter_by(slug="ionq").one()
        acme = db.query(CompanyBoard).filter_by(slug="acme").one()
        assert ionq.company == "IonQ" and acme.company is None


class TestTheSettingsPageControlsIt:
    def _cfg(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        return tunables.effective_settings(db.query(Profile).first().data)

    def _run(self, cfg):
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {}, {}, only={"simplify"})

    def test_which_files_are_read(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, {GRAD: [row(1)], INTERN: [row(2)]}, calls)
        jobs, _ = self._run(self._cfg(db, {"simplify_listings_urls": f"{GRAD}, {INTERN}"}))
        assert calls == [GRAD, INTERN] and len(jobs) == 2
        calls.clear()
        jobs, _ = self._run(self._cfg(db, {"simplify_listings_urls": INTERN}))
        assert calls == [INTERN] and len(jobs) == 1

    def test_switching_it_off(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, {GRAD: [row(1)]}, calls)
        _, stats = self._run(self._cfg(db, {"simplify_enabled": False}))
        assert calls == [] and stats["simplify"]["enabled"] is False

    def test_validation_budget(self, db, monkeypatch):
        from unittest.mock import patch

        from app.config import settings
        from app.services.job_fetcher import fetch_and_save_jobs

        monkeypatch.setattr(settings, "ATS_BOARD_VALIDATION", True)
        db.add(Profile(data={"target_roles": ["Software Engineer"],
                             tunables.STORE_KEY: {"ats_board_validate_per_cycle": 1234}}))
        db.commit()
        with patch("app.services.company_boards.validate_pending", return_value={}) as vp, \
             patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)), \
             patch("app.services.job_fetcher._run_all_adapters", return_value=([], {})):
            fetch_and_save_jobs(db)
        assert vp.call_args.kwargs["limit"] == 1234


@pytest.mark.parametrize("key", ["simplify_enabled", "simplify_listings_urls",
                                 "slug_harvest_urls", "ats_board_validate_per_cycle"])
def test_each_new_setting_is_in_env_example(key):
    from pathlib import Path

    env = tunables.BY_KEY[key].env
    assert f"{env}=" in Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
