"""
How many company boards a cycle reaches, and how it spends Workday's budget.

The settings page decides both now; these tests store an override on the
profile and check the behaviour moves, per CLAUDE.md.
"""

from unittest.mock import patch

import httpx

from app.models.company_board import CompanyBoard
from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.ats_discovery import slug_caps
from app.services.sources import workday
from app.services.sources.base import board_workers, cycle_settings


def _profile(db, overrides):
    db.query(Profile).delete()
    db.add(Profile(data={"target_roles": ["Software Engineer"],
                         tunables.STORE_KEY: overrides}))
    db.commit()
    return db.query(Profile).first()


class TestConcurrency:
    def test_board_adapters_use_the_settings_page_worker_count(self, db, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "app.services.sources.greenhouse.fetch_boards_concurrently",
            lambda slugs, fetch_one, label, workers: seen.append(workers) or [])
        profile = _profile(db, {"ats_board_fetch_workers": 24})
        job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"],
            tunables.effective_settings(profile.data),
            {"greenhouse": ["acme"]}, {}, only={"greenhouse"})
        assert seen == [24]

    def test_outside_a_cycle_the_environment_decides(self):
        from app.config import settings

        assert board_workers() == settings.ATS_BOARD_FETCH_WORKERS

    def test_the_override_ends_with_the_cycle(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "ATS_BOARD_FETCH_WORKERS", 5)

        class Cfg:
            ATS_BOARD_FETCH_WORKERS = 3
        with cycle_settings(Cfg()):
            assert board_workers() == 3
        assert board_workers() == 5


class TestHowManyBoardsACycleReaches:
    def test_workday_tenants_per_cycle(self, db):
        profile = _profile(db, {"workday_max_tenants": 90})
        assert slug_caps(tunables.effective_settings(profile.data))["workday"] == 90
        profile = _profile(db, {"workday_max_tenants": 400})
        assert slug_caps(tunables.effective_settings(profile.data))["workday"] == 400

    def test_boards_per_ats(self, db):
        profile = _profile(db, {"ats_max_slugs_per_ats": 1200})
        caps = slug_caps(tunables.effective_settings(profile.data))
        assert caps["greenhouse"] == 1200 and caps["lever"] == 1200
        # The per-company-expensive ones keep their tighter ceilings.
        assert caps["smartrecruiters"] == 80

    def test_a_cycle_polls_that_many_registered_tenants(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs

        _profile(db, {"workday_max_tenants": 45})
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for i in range(80):
            db.add(CompanyBoard(ats="workday", slug=f"t{i}:wd1:Site", origin="list",
                                active=True, first_seen_at=now, last_seen_at=now))
        db.commit()
        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)), \
             patch("app.services.job_fetcher._run_all_adapters",
                   return_value=([], {})) as run:
            fetch_and_save_jobs(db)
        assert len(run.call_args[0][3]["workday"]) == 45


class TestWorkdayDetailBudget:
    def _postings(self):
        # "Sales Associate" shares nothing; "Civil Engineer" only the generic
        # "engineer"; the last two share "software".
        return [{"title": "Sales Associate", "externalPath": "/job/a"},
                {"title": "Civil Engineer", "externalPath": "/job/b"},
                {"title": "Software Engineer I", "externalPath": "/job/c"},
                {"title": "Backend Software Engineer", "externalPath": "/job/d"}]

    def test_titles_the_matcher_keeps_are_described_first(self):
        paths = workday._detail_paths(self._postings(), ["Software Engineer"], 2)
        assert paths == {"/job/c", "/job/d"}

    def test_leftover_budget_goes_to_what_the_filter_would_pass_next(self):
        paths = workday._detail_paths(self._postings(), ["Software Engineer"], 3)
        assert paths == {"/job/c", "/job/d", "/job/b"}

    def test_every_posting_is_still_returned(self, monkeypatch):
        rows = self._postings()

        def post(url, json=None, **kw):
            return httpx.Response(200, json={"total": len(rows), "jobPostings": rows},
                                  request=httpx.Request("POST", url))

        described = []

        def detail(tenant, host, site, path):
            described.append(path)
            return {"jobDescription": "<p>Build</p>"}

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(workday, "_fetch_detail", detail)
        from app.config import settings
        monkeypatch.setattr(settings, "WORKDAY_MAX_DETAILS_PER_BOARD", 2)
        jobs = workday.fetch(["acme:wd1:Ext"], ["Software Engineer"])
        assert len(jobs) == 4
        assert sorted(described) == ["/job/c", "/job/d"]
        assert {j["title"] for j in jobs if j["description"]} == {
            "Software Engineer I", "Backend Software Engineer"}
