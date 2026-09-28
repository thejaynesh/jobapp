"""
Every career site of a Workday company, and the large-employer seed boards.

The robots.txt below is Salesforce's as read on 2026-09-28 (trimmed): its
new-grad and internship roles are on sites of their own, which no posting link
names. No network.
"""

from datetime import datetime, timedelta, timezone

import httpx

from app.models.company_board import CompanyBoard
from app.models.profile import Profile
from app.services import company_boards, tunables
from app.services.sources import workday

ROBOTS = """Sitemap: https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/siteMap.xml
Sitemap: https://salesforce.wd12.myworkdayjobs.com/Futureforce_NewGradRoles/siteMap.xml
Sitemap: https://salesforce.wd12.myworkdayjobs.com/Futureforce_Internships/siteMap.xml

User-agent: *
Allow: /External_Career_Site/
Allow: /Futureforce_NewGradRoles/
Disallow: /refreshFacet/"""


def robots(monkeypatch, calls=None):
    def get(url, **kw):
        if calls is not None:
            calls.append(url)
        if url == "https://salesforce.wd12.myworkdayjobs.com/robots.txt":
            return httpx.Response(200, text=ROBOTS, request=httpx.Request("GET", url))
        return httpx.Response(404, text="", request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)


def _board(db, slug, company="Salesforce"):
    now = datetime.now(timezone.utc)
    db.add(CompanyBoard(ats="workday", slug=slug, company=company, origin="discovered",
                        active=True, validated_at=now, first_seen_at=now, last_seen_at=now))
    db.commit()


def test_sites_come_from_the_tenants_robots_file(monkeypatch):
    robots(monkeypatch)
    assert workday.sites_for("salesforce", "wd12") == [
        "External_Career_Site", "Futureforce_NewGradRoles", "Futureforce_Internships"]


class TestExpansion:
    def test_a_known_tenant_gains_its_other_sites_under_its_name(self, db, monkeypatch):
        robots(monkeypatch)
        db.add(Profile(data={}))
        _board(db, "salesforce:wd12:External_Career_Site")
        report = company_boards.expand_workday_sites(db)
        assert report == {"scanned": 1, "new_boards": 2}
        new = db.query(CompanyBoard).filter_by(slug="salesforce:wd12:Futureforce_NewGradRoles").one()
        assert new.company == "Salesforce" and new.origin == "workday-sites"

    def test_a_tenant_is_read_once_a_month(self, db, monkeypatch):
        calls = []
        robots(monkeypatch, calls)
        db.add(Profile(data={}))
        _board(db, "salesforce:wd12:External_Career_Site")
        company_boards.expand_workday_sites(db)
        company_boards.expand_workday_sites(db)
        assert len(calls) == 1
        profile = db.query(Profile).first()
        stamp = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        profile.data = {**profile.data,
                        company_boards.WORKDAY_SITES_KEY: {"salesforce:wd12": stamp}}
        db.commit()
        company_boards.expand_workday_sites(db)
        assert len(calls) == 2

    def test_a_tenant_without_a_robots_file_costs_nothing_more(self, db, monkeypatch):
        robots(monkeypatch)
        db.add(Profile(data={}))
        _board(db, "acme:wd1:Ext", company="Acme")
        assert company_boards.expand_workday_sites(db) == {"scanned": 1, "new_boards": 0}

    def test_the_settings_page_can_switch_it_off(self, db, monkeypatch):
        from app.tasks import discovery

        class _Borrowed:
            def __getattr__(self, name):
                return getattr(db, name)

            def close(self):
                pass

        monkeypatch.setattr(discovery, "SessionLocal", _Borrowed)
        calls = []
        robots(monkeypatch, calls)
        db.add(Profile(data={tunables.STORE_KEY: {"workday_site_discovery": False,
                                                  "commoncrawl_enabled": False}}))
        _board(db, "salesforce:wd12:External_Career_Site")
        report = discovery.discover_boards()
        assert "workday_sites" not in report and calls == []
        profile = db.query(Profile).first()
        profile.data = {tunables.STORE_KEY: {"workday_site_discovery": True,
                                             "commoncrawl_enabled": False}}
        db.commit()
        assert discovery.discover_boards()["workday_sites"]["new_boards"] == 2


class TestLargeEmployerSeeds:
    def test_they_join_the_seed_list_with_their_names(self):
        from app.services.ats_seeds import SEED_ATS_SLUGS, SEED_BOARD_NAMES

        assert "salesforce:wd12:Futureforce_NewGradRoles" in SEED_ATS_SLUGS["workday"]
        assert "apply.careers.microsoft.com" in SEED_ATS_SLUGS["eightfold"]
        assert SEED_BOARD_NAMES[("eightfold", "apply.careers.microsoft.com")] == "Microsoft"
        # The hand-verified list is still there, and nothing is listed twice.
        assert "nvidia:wd5:NVIDIAExternalCareerSite" in SEED_ATS_SLUGS["workday"]
        assert len(SEED_ATS_SLUGS["workday"]) == len(set(SEED_ATS_SLUGS["workday"]))

    def test_every_seed_is_a_spec_its_adapter_reads(self):
        from app.services.ats_seeds_large import LARGE_EMPLOYER_BOARDS
        from app.services.sources.workday import parse_tenant_spec

        assert all(parse_tenant_spec(spec) for spec, _ in LARGE_EMPLOYER_BOARDS["workday"])
        assert all("." in host and "/" not in host
                   for host, _ in LARGE_EMPLOYER_BOARDS["eightfold"])

    def test_the_registry_files_seeds_under_their_company(self, db):
        from app.services.ats_seeds import SEED_ATS_SLUGS, SEED_BOARD_NAMES

        company_boards.backfill_from_slugs(db, SEED_ATS_SLUGS, origin="seed",
                                           names=SEED_BOARD_NAMES)
        board = db.query(CompanyBoard).filter_by(
            ats="workday", slug="salesforce:wd12:Futureforce_NewGradRoles").one()
        assert board.company == "Salesforce" and board.active
