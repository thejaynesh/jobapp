"""
Registries other projects publish, read as board lists: a JSON array of board
names per ATS, in the shape github.com/Feashliaa/job-board-aggregator ships
(MIT). And the registry keeping them without duplicates or a write storm.
No network.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.models.company_board import CompanyBoard
from app.services import company_boards
from app.services.ats_discovery import _slug_list_ats, harvest_boards_from_lists, slugs_from_list

BASE = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data"


class TestReadingAList:
    def test_the_ats_comes_from_the_file_name_or_a_prefix(self):
        assert _slug_list_ats(f"{BASE}/workday_companies.json")[0] == "workday"
        assert _slug_list_ats("lever=https://example.com/boards.json") == (
            "lever", "https://example.com/boards.json")
        # Not an ATS we read, and not a slug list at all.
        assert _slug_list_ats(f"{BASE}/paycom_companies.json")[0] is None
        assert _slug_list_ats("https://x/listings.json")[0] is None

    def test_workday_rows_become_specs(self):
        assert slugs_from_list("workday", [
            "23andme|wd5|23", "Salesforce|WD12|Futureforce_NewGradRoles",
            "acme|wd1|wday", "not a row",
        ]) == {"23andme:wd5:23", "salesforce:wd12:Futureforce_NewGradRoles"}

    def test_other_rows_are_board_names(self):
        assert slugs_from_list("greenhouse", [
            "Stripe", "10xgenomics", {"slug": "airbnb"}, "linkedin", "a", "has space", None,
        ]) == {"stripe", "10xgenomics", "airbnb"}

    def test_a_list_that_is_not_a_list_is_nothing(self):
        assert slugs_from_list("lever", {"boards": ["x"]}) == set()


def test_the_harvest_reads_slug_lists_beside_the_others(monkeypatch):
    lists = {
        f"{BASE}/greenhouse_companies.json": ["stripe", "10xgenomics"],
        f"{BASE}/workday_companies.json": ["23andme|wd5|23"],
        "https://example.com/mine.json": ["acme"],
    }

    def get(url, **kw):
        request = httpx.Request("GET", url)
        if url in lists:
            return httpx.Response(200, json=lists[url], request=request)
        return httpx.Response(404, text="", request=request)

    monkeypatch.setattr(httpx, "get", get)
    found, _ = harvest_boards_from_lists([
        f"{BASE}/greenhouse_companies.json", f"{BASE}/workday_companies.json",
        "ashby=https://example.com/mine.json", f"{BASE}/lever_companies.json",
    ])
    assert found == {"greenhouse": {"stripe", "10xgenomics"},
                     "workday": {"23andme:wd5:23"}, "ashby": {"acme"}}


def _board(db, ats, slug, seen):
    db.add(CompanyBoard(ats=ats, slug=slug, company="Salesforce", origin="seed",
                        active=True, validated_at=seen, first_seen_at=seen, last_seen_at=seen))
    db.commit()


class TestTheRegistry:
    def test_a_board_named_in_another_case_is_the_same_board(self, db):
        """Workday answers `external_career_site` and `External_Career_Site` alike."""
        seen = datetime.now(timezone.utc)
        _board(db, "workday", "salesforce:wd12:External_Career_Site", seen)
        new = company_boards.record_boards(
            db, {"workday": ["salesforce:wd12:external_career_site",
                             "salesforce:wd12:Futureforce_Internships",
                             "salesforce:wd12:futureforce_internships"]},
            origin="list", revive=False)
        assert new == 1
        slugs = sorted(b.slug for b in db.query(CompanyBoard).filter_by(ats="workday"))
        assert slugs == ["salesforce:wd12:External_Career_Site",
                         "salesforce:wd12:Futureforce_Internships"]

    @pytest.mark.parametrize("age_hours,revive,touched", [
        (2, False, False),   # a replay a couple of hours on changes nothing
        (30, False, True),   # but refreshes a board daily
        (2, True, True),     # a fresh sighting always does
    ])
    def test_a_replayed_list_refreshes_a_board_at_most_daily(self, db, age_hours, revive, touched):
        seen = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        _board(db, "greenhouse", "stripe", seen)
        company_boards.record_boards(db, {"greenhouse": ["stripe"]}, origin="list", revive=revive)
        db.commit()
        board = db.query(CompanyBoard).filter_by(slug="stripe").one()
        last = board.last_seen_at if board.last_seen_at.tzinfo else \
            board.last_seen_at.replace(tzinfo=timezone.utc)
        assert (abs((last - seen).total_seconds()) > 60) is touched

    def test_list_boards_wait_for_their_probe(self, db, monkeypatch):
        # conftest switches validation off for the suite; here it is the point.
        monkeypatch.setattr(company_boards, "_validation_enabled", lambda: True)
        company_boards.record_boards(db, {"bamboohr": ["acme"]}, origin="list", revive=False)
        board = db.query(CompanyBoard).filter_by(slug="acme").one()
        assert board.active is False and board.validated_at is None


class TestHourlyProbing:
    """The discovery tick probes waiting boards, as many as the settings say."""

    def _run(self, db, monkeypatch, overrides):
        from app.config import settings
        from app.models.profile import Profile
        from app.services import ats_validation, tunables
        from app.tasks import discovery

        class _Borrowed:
            def __getattr__(self, name):
                return getattr(db, name)

            def close(self):
                pass

        monkeypatch.setattr(discovery, "SessionLocal", _Borrowed)
        monkeypatch.setattr(settings, "ATS_BOARD_VALIDATION", True)
        monkeypatch.setattr(company_boards, "_validation_enabled", lambda: True)
        probed = []
        monkeypatch.setattr(ats_validation, "probe_board", lambda ats, slug: (
            probed.append(slug) or ats_validation.BoardProbe(True)))
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: {"commoncrawl_enabled": False,
                                                  "workday_site_discovery": False,
                                                  **overrides}}))
        db.commit()
        company_boards.record_boards(
            db, {"greenhouse": [f"co{i}" for i in range(5)]}, origin="list", revive=False)
        db.commit()
        return discovery.discover_boards(), probed

    def test_it_probes_up_to_the_hourly_budget(self, db, monkeypatch):
        report, probed = self._run(db, monkeypatch, {"ats_board_validate_hourly": 3})
        assert len(probed) == 3 and report["validated"]["activated"] == 3

    def test_zero_leaves_it_to_board_cycles(self, db, monkeypatch):
        report, probed = self._run(db, monkeypatch, {"ats_board_validate_hourly": 0})
        assert probed == [] and "validated" not in report
