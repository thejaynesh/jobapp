"""
JazzHR, read from its sitemaps and per-company exports. Shapes follow
app.jazz.co as read on 2026-09-28. No network.
"""

import httpx
import pytest

from app.models.profile import Profile
from app.services import job_fetcher, tunables
from app.services.sources import jazzhr

ROBOTS = """User-agent: *
Disallow: /cb

Sitemap: http://app.jazz.co/feeds/google/xml/0
Sitemap: http://app.jazz.co/feeds/google/xml/1
"""


def sitemap(*postings):
    urls = "".join(
        f"<url><loc>https://{company}.applytojob.com/apply/{code}/{slug}?source=GS</loc>"
        f"<lastmod>2026-09-28T00:00:00+00:00</lastmod></url>"
        for company, code, slug in postings)
    return f'<?xml version="1.0"?><urlset>{urls}</urlset>'


def job_xml(company, code, title, job_id=None, experience="Entry Level",
            kind="Full Time", status="Open", city="Pittsburgh"):
    job_id = job_id or f"job_20260920133627_{code.upper()}"
    return (f"<job><id><![CDATA[{job_id}]]></id><status><![CDATA[{status}]]></status>"
            f"<title><![CDATA[{title}]]></title>"
            f"<url><![CDATA[https://{company}.applytojob.com/apply/{code}/X]]></url>"
            f"<city><![CDATA[{city}]]></city><state><![CDATA[PA]]></state>"
            f"<country><![CDATA[United States]]></country>"
            f"<description><![CDATA[<p>Build <b>flight software</b>.</p>]]></description>"
            f"<type><![CDATA[{kind}]]></type><experience><![CDATA[{experience}]]></experience></job>")


def export_xml(name, *jobs):
    return (f'<?xml version="1.0" encoding="utf-8"?><jobs><publisher>JazzHR</publisher>'
            f"<company><![CDATA[{name}]]></company>{''.join(jobs)}</jobs>")


@pytest.fixture(autouse=True)
def _fresh_memory():
    jazzhr._SEEN.clear()
    yield
    jazzhr._SEEN.clear()


def serve(monkeypatch, sitemaps, exports, calls):
    def get(url, **kw):
        calls.append(url)
        request = httpx.Request("GET", url)
        if url == jazzhr.ROBOTS_URL:
            return httpx.Response(200, text=ROBOTS, request=request)
        if "/feeds/google/xml/" in url:
            return httpx.Response(200, text=sitemaps[int(url.rsplit("/", 1)[1])],
                                  request=request)
        company = url.rsplit("/", 1)[1]
        if company in exports:
            return httpx.Response(200, text=exports[company], request=request)
        return httpx.Response(404, text="", request=request)
    monkeypatch.setattr(httpx, "get", get)


SITEMAPS = [
    sitemap(("aerotech", "Ab12Cd34Ef", "Software-Engineer"),
            ("aerotech", "Zz99Yy88Xx", "Custodian")),
    sitemap(("mobomo", "1vgh8LVE6S", "Senior-Software-Engineer"),
            ("farinspections", "Fa11Fa11Fa", "Vacancy-Data-Driver"),
            ("farinspections", "Fa22Fa22Fa", "Occupancy-Data-Driver")),
]
EXPORTS = {
    "aerotech": export_xml("Aerotech, Inc.",
                           job_xml("aerotech", "Ab12Cd34Ef", "Software Engineer"),
                           job_xml("aerotech", "Zz99Yy88Xx", "Custodian")),
    "mobomo": export_xml("Mobomo", job_xml("mobomo", "1vgh8LVE6S", "Senior Software Engineer",
                                           experience="Senior Level")),
    "farinspections": export_xml("FAR Inspections",
                                 job_xml("farinspections", "Fa11Fa11Fa", "Vacancy Data Driver"),
                                 job_xml("farinspections", "Fa22Fa22Fa", "Occupancy Data Driver")),
}


def _exports_read(calls):
    return [u.rsplit("/", 1)[1] for u in calls if "/feeds/export/jobs/" in u]


def test_every_sitemap_robots_names_is_read(monkeypatch):
    calls = []
    serve(monkeypatch, SITEMAPS, EXPORTS, calls)
    assert jazzhr.sitemap_urls() == ["https://app.jazz.co/feeds/google/xml/0",
                                     "https://app.jazz.co/feeds/google/xml/1"]
    assert len(jazzhr.sitemap_postings()) == 5


def test_only_companies_with_a_matching_title_are_asked(monkeypatch):
    calls = []
    serve(monkeypatch, SITEMAPS, EXPORTS, calls)
    jobs = jazzhr.fetch(["Software Engineer"])
    assert sorted(_exports_read(calls)) == ["aerotech", "mobomo"]
    assert sorted(j["title"] for j in jobs) == ["Senior Software Engineer", "Software Engineer"]


def test_a_posting_is_read_whole(monkeypatch):
    serve(monkeypatch, SITEMAPS, EXPORTS, [])
    job = next(j for j in jazzhr.fetch(["Software Engineer"]) if j["company"] == "Aerotech, Inc.")
    assert job["url"] == "https://aerotech.applytojob.com/apply/Ab12Cd34Ef/X"
    assert job["source"] == "jazzhr" and job["source_job_id"] == "job_20260920133627_AB12CD34EF"
    assert job["location"] == "Pittsburgh, PA, United States"
    assert job["description"] == "Build flight software."
    assert job["posted_at"] == "2026-09-20T13:36:27+00:00"
    assert job["experience_level"] == "entry" and job["employment_type"] == "full_time"


def test_the_companies_with_whole_role_matches_go_first(monkeypatch):
    """Two "Data Driver"s share a word with "Data Scientist"; one real match wins."""
    sitemaps = [sitemap(("farinspections", "Fa11Fa11Fa", "Vacancy-Data-Driver"),
                        ("farinspections", "Fa22Fa22Fa", "Occupancy-Data-Driver"),
                        ("acme", "Ac11Ac11Ac", "Data-Scientist"))]
    exports = {**EXPORTS, "acme": export_xml("Acme", job_xml("acme", "Ac11Ac11Ac", "Data Scientist"))}
    calls = []
    serve(monkeypatch, sitemaps, exports, calls)
    jazzhr.fetch(["Data Scientist"], max_companies=1)
    assert _exports_read(calls) == ["acme"]


def test_a_posting_already_read_is_not_pursued_again(monkeypatch):
    calls = []
    serve(monkeypatch, SITEMAPS, EXPORTS, calls)
    assert len(jazzhr.fetch(["Software Engineer"])) == 2
    calls.clear()
    assert jazzhr.fetch(["Software Engineer"]) == []
    assert _exports_read(calls) == []


def test_a_code_the_export_spells_differently_is_still_read_once(monkeypatch):
    """The sitemap's long hex code and the export's short one name one posting."""
    long_code = "00073b300b784a575670500d5d685d4506706c336513653f34157906605c1b754e020b"
    sitemaps = [sitemap(("gliacell", long_code, "Python-Software-Engineer"))]
    exports = {"gliacell": export_xml("GliaCell", job_xml("gliacell", "5CooZTgUWP",
                                                          "Python Software Engineer"))}
    calls = []
    serve(monkeypatch, sitemaps, exports, calls)
    assert [j["title"] for j in jazzhr.fetch(["Software Engineer"])] == ["Python Software Engineer"]
    calls.clear()
    assert jazzhr.fetch(["Software Engineer"]) == [] and _exports_read(calls) == []


def test_a_closed_posting_is_skipped(monkeypatch):
    exports = {**EXPORTS, "mobomo": export_xml("Mobomo", job_xml(
        "mobomo", "1vgh8LVE6S", "Senior Software Engineer", status="Closed"))}
    serve(monkeypatch, SITEMAPS, exports, [])
    assert [j["company"] for j in jazzhr.fetch(["Software Engineer"])] == ["Aerotech, Inc."]


@pytest.mark.parametrize("experience,title,level,kind", [
    ("Internship", "Software Engineer", "entry", "internship"),
    ("Senior Level", "Software Engineer", "senior", "full_time"),
    ("", "Software Engineer Intern", None, "internship"),
])
def test_experience_and_type(experience, title, level, kind):
    row = {"id": "job_20260920133627_X", "title": title, "status": "Open",
           "url": "https://a.applytojob.com/apply/X/Y", "experience": experience,
           "type": "Full Time", "description": "Build things."}
    job = jazzhr._as_job(row, "Acme")
    assert job["experience_level"] == level and job["employment_type"] == kind


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {}, {}, only={"jazzhr"})

    def test_companies_per_cycle(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, SITEMAPS, EXPORTS, calls)
        self._run(db, {"jazzhr_max_companies": 1})
        assert len(_exports_read(calls)) == 1
        jazzhr._SEEN.clear()
        calls.clear()
        self._run(db, {"jazzhr_max_companies": 5})
        assert len(_exports_read(calls)) == 2

    def test_switching_it_off(self, db, monkeypatch):
        calls = []
        serve(monkeypatch, SITEMAPS, EXPORTS, calls)
        _, stats = self._run(db, {"jazzhr_enabled": False})
        assert calls == [] and stats["jazzhr"]["enabled"] is False
