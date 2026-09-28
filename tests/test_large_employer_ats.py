"""
Oracle Recruiting Cloud, SuccessFactors, Phenom and Eightfold: the careers
platforms of large US employers, read by careers host.

Fixtures are trimmed from live responses read on 2026-09-28 (American Express's
Oracle site, Qorvo's SuccessFactors feed, Mastercard's Phenom site, Qualcomm's
Eightfold site). No network: `httpx` is answered here.
"""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from app.services import enrichment, job_fetcher
from app.services.ats_discovery import discover_from_jobs, extract_slugs
from app.services.ats_validation import probe_board
from app.services.sources import eightfold, oracle, phenom, successfactors

Q = ["Software Engineer"]


def _resp(url, *, json_body=None, text=None, status=200, method="GET"):
    req = httpx.Request(method, url)
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text or "", request=req)


# --- Oracle ----------------------------------------------------------------

ORACLE = "egug.fa.us2.oraclecloud.com"


def _oracle_row(n, title):
    return {"Id": str(26000000 + n), "Title": title, "PostedDate": "2026-09-2%d" % (n % 9),
            "PrimaryLocation": "Phoenix, AZ, United States", "WorkplaceTypeCode": "ORA_HYBRID",
            "secondaryLocations": [{"Name": "New York, NY, United States"}]}


ORACLE_ROWS = [_oracle_row(i, t) for i, t in enumerate([
    "Software Engineer I", "Financial Analyst", "Sr Software Engineer", "Engineer III, Backend",
    "Risk Manager"])]


def _oracle_get(calls):
    def get(url, **kw):
        calls.append(unquote(url))
        if "recruitingCEJobRequisitions?" in url:
            finder = unquote(url).split("finder=")[1]
            offset = int(finder.split("offset=")[1].split(",")[0])
            limit = int(finder.split("limit=")[1].split(",")[0])
            page = ORACLE_ROWS[offset:offset + limit]
            return _resp(url, json_body={"items": [{"TotalJobsCount": len(ORACLE_ROWS),
                                                    "requisitionList": page}]})
        if "recruitingCEJobRequisitionDetails" in url:
            job_id = unquote(url).split('Id="')[1].split('"')[0]
            return _resp(url, json_body={"items": [{
                "Id": job_id, "ExternalDescriptionStr": "<p>Build payments systems.</p>",
                "ExternalQualificationsStr": "<ul><li>Python</li></ul>",
                "ExternalPostedStartDate": "2026-09-25T13:08:48+00:00",
                "PrimaryLocation": "Phoenix, AZ, United States"}]})
        if "/hcmUI/CandidateExperience/" in url:
            return _resp(url, text="<html><head><title>American Express</title></head></html>")
        return _resp(url, status=404)
    return get


class TestOracle:
    def test_a_site_is_read_whole_and_gated_by_title(self, monkeypatch):
        calls = []
        monkeypatch.setattr(httpx, "get", _oracle_get(calls))
        monkeypatch.setattr(oracle, "_PAGE_SIZE", 2)
        monkeypatch.setattr(oracle, "_MAX_DETAILS", 2)
        jobs = oracle.fetch([f"{ORACLE}:CX_1"], Q)
        titles = {j["title"] for j in jobs}
        assert "Financial Analyst" not in titles and "Risk Manager" not in titles
        assert {"Software Engineer I", "Sr Software Engineer"} <= titles
        # Paged at the (patched) page size until the total was reached.
        assert sum("recruitingCEJobRequisitions?" in c for c in calls) == 3
        job = next(j for j in jobs if j["title"] == "Software Engineer I")
        assert job["company"] == "American Express"
        assert job["url"] == f"https://{ORACLE}/hcmUI/CandidateExperience/en/sites/CX_1/job/26000000"
        assert job["source_job_id"] == f"{ORACLE}:26000000"
        assert job["location"] == "Phoenix, AZ, United States; New York, NY, United States"
        assert "Build payments systems." in job["description"] and "Python" in job["description"]
        assert job["posted_at"] == "2026-09-25T13:08:48+00:00"
        # Two descriptions, spent on the best titles.
        assert sum(bool(j["description"]) for j in jobs) == 2

    def test_a_bad_spec_is_refused(self):
        assert oracle.parse_spec("example.com:CX_1") is None
        assert oracle.parse_spec(f"{ORACLE}:CX_1") == (ORACLE, "CX_1")

    def test_enrichment_reads_an_oracle_posting(self, monkeypatch):
        with httpx.Client(transport=httpx.MockTransport(
                lambda req: _oracle_get([])(str(req.url)))) as client:
            found = enrichment._ats_extraction(
                client, f"https://{ORACLE}/hcmUI/CandidateExperience/en/sites/CX_1/job/26000003")
        assert found.method == "ats_api" and "Build payments systems." in found.description
        assert enrichment.looks_like_ats(
            f"https://{ORACLE}/hcmUI/CandidateExperience/en/sites/CX_1/job/1")

    def test_validation_names_the_employer_and_refuses_a_missing_site(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", _oracle_get([]))
        probe = probe_board("oracle", f"{ORACLE}:CX_1")
        assert probe.exists and probe.company == "American Express"
        monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, status=404))
        assert not probe_board("oracle", "nope.fa.us2.oraclecloud.com:CX_1").exists


# --- SuccessFactors ----------------------------------------------------------

FEED = """<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel><title>Jobs</title>
<item><title>Software Engineer (Chelmsford, MA, US, 1824)</title>
<description><![CDATA[&lt;p&gt;Design &amp;amp; build RF software.&lt;/p&gt;]]></description>
<link>https://careers.qorvo.com/job/Chelmsford-Software-Engineer-MA-1824/1386275600/</link>
<guid>1386275600</guid><g:id>1386275600</g:id><g:expiration_date>2026-10-28</g:expiration_date>
<g:employer>Qorvo US Inc.</g:employer><g:location>Chelmsford, MA, US, 1824</g:location></item>
<item><title>Senior Specialist, Program Finance (Norfolk, VA, US, 23502)</title>
<description><![CDATA[&lt;p&gt;Finance.&lt;/p&gt;]]></description>
<link>https://careers.qorvo.com/job/Norfolk-Finance-VA-23502/1418964300/</link>
<guid>1418964300</guid><g:id>1418964300</g:id><g:employer>Qorvo US Inc.</g:employer>
<g:location>Norfolk, VA, US, 23502</g:location></item>
</channel></rss>"""


class _Stream:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        yield self._body


class TestSuccessFactors:
    def test_the_feed_is_one_request_with_descriptions(self, monkeypatch):
        calls = []
        monkeypatch.setattr(httpx, "get", lambda url, **kw: calls.append(url) or _resp(url, text=FEED))
        (job,) = successfactors.fetch(["careers.qorvo.com"], Q)
        assert calls == ["https://careers.qorvo.com/sitemal.xml"]
        assert job["title"] == "Software Engineer"          # location dropped from the title
        assert job["location"] == "Chelmsford, MA, US"      # and the site code from the place
        assert job["company"] == "Qorvo US Inc."
        assert job["source_job_id"] == "careers.qorvo.com:1386275600"
        assert job["description"] == "Design & build RF software."
        assert job["posted_at"] is None

    def test_validation_reads_only_the_head_of_the_feed(self, monkeypatch):
        monkeypatch.setattr(httpx, "stream", lambda *a, **kw: _Stream(200, FEED.encode()))
        assert probe_board("successfactors", "careers.qorvo.com").exists
        monkeypatch.setattr(httpx, "stream", lambda *a, **kw: _Stream(200, b"<html>hi</html>"))
        assert not probe_board("successfactors", "example.com").exists
        monkeypatch.setattr(httpx, "stream", lambda *a, **kw: _Stream(404, b""))
        assert not probe_board("successfactors", "example.com").exists


# --- Phenom ------------------------------------------------------------------

def _phenom_job(n, title):
    return {"title": title, "jobId": f"R-27{n:04d}", "jobSeqNo": f"MASRUSR27{n:04d}EXTERNALENUS",
            "cityStateCountry": "O Fallon, Missouri, United States of America",
            "postedDate": "2026-08-18T00:00:00.000+0000",
            "applyUrl": f"https://mastercard.wd1.myworkdayjobs.com/CorporateCareers/job/OFallon/X_R-27{n:04d}/apply"}


def _phenom_post(bodies, total=3):
    jobs = [_phenom_job(i, t) for i, t in enumerate(
        ["Senior Software Engineer", "Software Engineer II", "Product Designer"])]

    def post(url, json=None, **kw):
        bodies.append(json)
        if json["ddoKey"] == "refineSearch":
            page = jobs[json["from"]:json["from"] + json["size"]]
            return _resp(url, method="POST", json_body={"refineSearch": {
                "status": 200, "totalHits": total, "data": {"jobs": page}}})
        return _resp(url, method="POST", json_body={"jobDetail": {"status": 200, "data": {"job": {
            "description": "<p>Our Purpose</p>", "companyName": "Mastercard"}}}})
    return post


class TestPhenom:
    def test_search_pages_and_the_board_behind_the_site(self, monkeypatch):
        bodies = []
        monkeypatch.setattr(httpx, "post", _phenom_post(bodies))
        monkeypatch.setattr(phenom, "_PAGE_SIZE", 2)
        monkeypatch.setattr(phenom, "_MAX_DETAILS", 1)
        jobs = phenom.fetch(["careers.mastercard.com/us/en"], Q)
        assert len(jobs) == 3
        searches = [b for b in bodies if b["ddoKey"] == "refineSearch"]
        assert [b["from"] for b in searches] == [0, 2]
        assert searches[0]["keywords"] == "Software Engineer" and searches[0]["lang"] == "en_us"
        job = next(j for j in jobs if j["description"])
        assert job["company"] == "Mastercard" and job["description"] == "Our Purpose"
        assert job["url"].startswith("https://careers.mastercard.com/us/en/job/R-27")
        assert job["apply_url"].startswith("https://mastercard.wd1.myworkdayjobs.com/")

    def test_the_board_behind_a_phenom_site_is_discovered(self):
        found = discover_from_jobs([{
            "source": "phenom", "url": "https://careers.mastercard.com/us/en/job/R-1",
            "apply_url": "https://mastercard.wd1.myworkdayjobs.com/CorporateCareers/job/X/Y_R-1/apply",
        }])
        assert found == {"workday": {"mastercard:wd1:CorporateCareers"}}

    def test_validation_wants_phenoms_own_search_block(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", _phenom_post([]))
        assert probe_board("phenom", "careers.mastercard.com/us/en").exists
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(url, method="POST",
                                                                   json_body={"ok": True}))
        assert not probe_board("phenom", "example.com/us/en").exists
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(url, method="POST",
                                                                   text="<html>"))
        assert not probe_board("phenom", "example.com/us/en").exists


# --- Eightfold ---------------------------------------------------------------

ROBOTS = ("User-agent: *\nAllow: /api/pcsx\n"
          "Sitemap: https://qualcomm.eightfold.ai/careers/sitemap_index.xml?domain=qualcomm.com\n")


def _positions(n):
    return [{"id": 4467000000 + i, "name": t, "locations": ["San Diego, California, United States of America"],
             "standardizedLocations": ["San Diego, CA, US"], "postedTs": 1789603200,
             "workLocationOption": "onsite", "positionUrl": f"/careers/job/{4467000000 + i}"}
            for i, t in enumerate(["Senior Software Engineer", "Software Engineer, Modem",
                                   "Finance Manager"][:n])]


def _eightfold_get(calls):
    def get(url, **kw):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return _resp(url, text=ROBOTS)
        if "/api/pcsx/search" in url:
            qs = parse_qs(urlsplit(url).query)
            assert qs["domain"] == ["qualcomm.com"]
            start = int(qs["start"][0])
            rows = _positions(3)[start:start + 2]
            return _resp(url, json_body={"status": 200, "data": {"positions": rows, "count": 3}})
        if "/api/pcsx/position_details" in url:
            return _resp(url, json_body={"status": 200, "data": {"jobDescription": "<p>Modem</p>"}})
        return _resp(url, status=404)
    return get


class TestEightfold:
    def test_domain_from_robots_then_search_and_details(self, monkeypatch):
        calls = []
        monkeypatch.setattr(httpx, "get", _eightfold_get(calls))
        monkeypatch.setattr(eightfold, "_PAGE_SIZE", 2)
        monkeypatch.setattr(eightfold, "_MAX_DETAILS", 1)
        jobs = eightfold.fetch(["qualcomm.eightfold.ai"], Q)
        assert len(jobs) == 3
        assert sum("/api/pcsx/search" in c for c in calls) == 2
        job = jobs[0]
        assert job["company"] == "Qualcomm"
        assert job["url"] == "https://qualcomm.eightfold.ai/careers/job/4467000000"
        assert job["location"] == "San Diego, California, United States of America"
        assert job["posted_at"].startswith("2026-09-17")
        assert sum(bool(j["description"]) for j in jobs) == 1

    def test_a_custom_host_is_its_own_domain_when_robots_is_silent(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda url, **kw: _resp(url, text="User-agent: *\n"))
        assert eightfold.tenant_domain("apply.careers.microsoft.com") == "microsoft.com"
        assert eightfold.tenant_domain("eaton.eightfold.ai") == "eaton.com"


# --- Discovery and wiring ----------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (f"https://{ORACLE}/hcmUI/CandidateExperience/en/sites/CX_1/job/26007181",
     {"oracle": {f"{ORACLE}:CX_1"}}),
    ("https://careers.qorvo.com/job/Chelmsford-RFIC-Intern-MA-1824/1424704500/?ats=successfactors",
     {"successfactors": {"careers.qorvo.com"}}),
    ("https://careers.mastercard.com/us/en/job/R-275650/Senior-Software-Engineer",
     {"phenom": {"careers.mastercard.com/us/en"}}),
    ("https://qualcomm.eightfold.ai/careers/job/446717859953", {"eightfold": {"qualcomm.eightfold.ai"}}),
    ("https://apply.careers.microsoft.com/careers/job/1970393556982911",
     {"eightfold": {"apply.careers.microsoft.com"}}),
    # A job board with the same URL shape is not an employer.
    ("https://builtin.com/job/software-engineer-i/11387823/", {}),
    ("https://www.linkedin.com/us/en/job/12345", {}),
])
def test_boards_are_found_in_posting_links(url, expected):
    assert extract_slugs(url) == expected


def test_the_fetcher_hands_them_the_roles(monkeypatch):
    from app.config import settings

    seen = {}
    for name in ("oracle", "successfactors", "phenom", "eightfold"):
        monkeypatch.setattr(f"app.services.sources.{name}.fetch",
                            lambda company_slugs, queries=None, _n=name: seen.setdefault(
                                _n, (company_slugs, queries)) and [])
    slugs = {"oracle": [f"{ORACLE}:CX_1"], "successfactors": ["careers.qorvo.com"],
             "phenom": ["careers.mastercard.com/us/en"], "eightfold": ["qualcomm.eightfold.ai"]}
    _, stats = job_fetcher._run_all_adapters(
        ["Software Engineer"], ["Remote"], settings, slugs, {},
        only={"oracle", "successfactors", "phenom", "eightfold"})
    assert seen == {k: (v, ["Software Engineer"]) for k, v in slugs.items()}
    assert all(stats[k]["enabled"] for k in slugs)
