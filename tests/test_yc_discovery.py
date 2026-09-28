"""
Boards behind the websites of hiring YC companies, from yc-oss's directory,
a batch per hourly tick. No network.
"""

import httpx

from app.models.company_board import CompanyBoard
from app.models.profile import Profile
from app.services import ats_sniffer, tunables, yc_discovery

HIRING = [
    {"name": "Legalist", "website": "https://www.legalist.com", "isHiring": True},
    {"name": "Nanonets", "website": "https://nanonets.com", "isHiring": True},
    {"name": "OneChronos", "website": "https://www.onechronos.com", "isHiring": True},
    {"name": "No Site", "website": "", "isHiring": True},
    {"name": "Social Only", "website": "https://twitter.com/x", "isHiring": True},
]
BOARDS = {"www.legalist.com": {"greenhouse": ["legalist"]},
          "nanonets.com": {"ashby": ["nanonets"]}}


def serve(monkeypatch, calls):
    def get(url, **kw):
        calls.append(url)
        return httpx.Response(200, json=HIRING, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    sniffed = []
    monkeypatch.setattr(ats_sniffer, "sniff_host", lambda host, html="", posting_url=None,
                        company=None: sniffed.append((host, company)) or BOARDS.get(host, {}))
    return sniffed


def test_the_directory_gives_each_hiring_companys_site(monkeypatch):
    serve(monkeypatch, [])
    assert yc_discovery.hiring_sites() == [
        ["nanonets.com", "https://nanonets.com", "Nanonets"],
        ["www.legalist.com", "https://www.legalist.com", "Legalist"],
        ["www.onechronos.com", "https://www.onechronos.com", "OneChronos"],
    ]


def _tick(db, overrides):
    from app.tasks import discovery

    class _Borrowed:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    discovery.SessionLocal = _Borrowed
    profile = db.query(Profile).first()
    profile.data = {**(profile.data or {}), tunables.STORE_KEY: {
        "commoncrawl_enabled": False, "workday_site_discovery": False,
        "ats_board_validate_hourly": 0, "yc_discovery_enabled": True, **overrides}}
    db.commit()
    return discovery.discover_boards()


def test_sites_are_looked_behind_a_batch_at_a_time(db, monkeypatch):
    calls = []
    sniffed = serve(monkeypatch, calls)
    db.add(Profile(data={}))
    db.commit()

    report = _tick(db, {"yc_discovery_per_hour": 2})["yc"]
    assert report["queued"] == 3 and report["looked_at"] == 2 and report["remaining"] == 1
    assert sniffed == [("nanonets.com", "Nanonets"), ("www.legalist.com", "Legalist")]
    board = db.query(CompanyBoard).filter_by(ats="ashby", slug="nanonets").one()
    assert board.company == "Nanonets" and board.origin == "sniffed"

    report = _tick(db, {"yc_discovery_per_hour": 2})["yc"]
    assert report["looked_at"] == 1 and report["remaining"] == 0
    # The directory is read once a week, not every tick.
    assert calls == [yc_discovery.HIRING_URL]


def test_the_settings_page_can_switch_it_off(db, monkeypatch):
    calls = []
    serve(monkeypatch, calls)
    db.add(Profile(data={}))
    db.commit()
    assert "yc" not in _tick(db, {"yc_discovery_enabled": False})
    assert calls == []
    assert _tick(db, {"yc_discovery_per_hour": 0})["yc"] == {"skipped": True}
