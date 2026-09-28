"""
Taleo career sections: the portal number from the search page, the REST
search it calls, discovery, the probe, and enrichment's reading of a posting
page. Shapes follow textron.taleo.net as read on 2026-09-28. No network.
"""

from unittest.mock import MagicMock
from urllib.parse import quote

import httpx
import pytest

from app.services import ats_validation, enrichment
from app.services.ats_discovery import extract_slugs
from app.services.sources import taleo

SEARCH_PAGE = "<script>queryString: 'lang=en&amp;portal=8140753014',</script>"


def row(n, title, location='["US-Massachusetts-Wilmington"]', date="09/02/2026"):
    return {"jobId": str(1530000 + n), "contestNo": str(339000 + n),
            "column": [title, location, date], "linkedColumn": 0, "locationsColumns": [1]}


def serve(monkeypatch, rows, calls, page=SEARCH_PAGE, per_page=25):
    def get(url, params=None, **kw):
        calls.append(("GET", url, None))
        request = httpx.Request("GET", url)
        if url.endswith("/jobsearch.ftl"):
            return httpx.Response(200, text=page, request=request)
        return httpx.Response(404, text="", request=request)

    def post(url, params=None, json=None, **kw):
        calls.append(("POST", url, {**(params or {}), "body": json}))
        start = (json["pageNo"] - 1) * per_page
        return httpx.Response(200, json={
            "requisitionList": rows[start:start + per_page],
            "pagingData": {"currentPageNo": json["pageNo"], "pageSize": per_page,
                           "totalCount": len(rows)},
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)


def test_a_board_is_tenant_and_section():
    assert taleo.parse_spec("textron/textron") == ("textron", "textron")
    assert taleo.parse_spec("weyerhaeuser/100002") == ("weyerhaeuser", "100002")
    assert taleo.parse_spec("textron") is None


def test_a_posting_is_read_from_its_row(monkeypatch):
    calls = []
    serve(monkeypatch, [row(1, "Software Engineer II/III")], calls)
    [job] = taleo.fetch(["textron/textron"], ["Software Engineer"])
    assert job["title"] == "Software Engineer II/III"
    assert job["url"] == ("https://textron.taleo.net/careersection/textron/"
                          "jobdetail.ftl?job=339001&lang=en")
    assert job["source_job_id"] == "textron:339001"
    assert job["location"] == "US-Massachusetts-Wilmington"
    assert job["posted_at"] == "2026-09-02T00:00:00+00:00"
    assert job["company"] == "textron/textron"   # the registry supplies the name
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == "https://textron.taleo.net/careersection/rest/jobboard/searchjobs"
    assert post[2]["portal"] == "8140753014"
    assert post[2]["body"]["fieldData"]["fields"]["KEYWORD"] == "Software Engineer"


def test_columns_are_read_where_the_row_says_they_are(monkeypatch):
    """cinfin/ex puts the requisition number first and the title second."""
    moved = {"jobId": "166788", "contestNo": "2600649",
             "column": ["2600649", "Software Engineer", '["OH-Cincinnati", "Remote"]'],
             "linkedColumn": 1, "locationsColumns": [2]}
    serve(monkeypatch, [moved], [])
    [job] = taleo.fetch(["cinfin/ex"], ["Software Engineer"])
    assert job["title"] == "Software Engineer"
    assert job["location"] == "OH-Cincinnati; Remote" and job["is_remote"]
    assert job["posted_at"] is None


def test_titles_are_held_to_the_roles(monkeypatch):
    """The search is loose, and each job kept costs enrichment a large page."""
    rows = [row(1, "Software Engineer III"), row(2, "Quality Engineer II"),
            row(3, "2027 Development Program: Operations Pathway")]
    serve(monkeypatch, rows, [])
    assert [j["title"] for j in taleo.fetch(["textron/textron"], ["Software Engineer"])] == [
        "Software Engineer III"]


def test_pages_until_the_total_runs_out(monkeypatch):
    calls = []
    serve(monkeypatch, [row(n, f"Software Engineer {n}") for n in range(60)], calls)
    assert len(taleo.fetch(["textron/textron"], ["Software Engineer"])) == 60
    assert [c[2]["body"]["pageNo"] for c in calls if c[0] == "POST"] == [1, 2, 3]


def test_a_section_without_a_portal_is_not_searched(monkeypatch):
    calls = []
    serve(monkeypatch, [row(1, "Software Engineer")], calls, page="<html>portal=null</html>")
    assert taleo.fetch(["kp/external"], ["Software Engineer"]) == []
    assert not any(c[0] == "POST" for c in calls)


class TestProbe:
    def test_a_section_with_a_portal_passes(self, monkeypatch):
        serve(monkeypatch, [], [])
        assert ats_validation._probe_taleo("textron/textron") is True

    def test_one_without_fails(self, monkeypatch):
        serve(monkeypatch, [], [], page="<html>no search here</html>")
        assert ats_validation._probe_taleo("kp/external") is False

    def test_a_missing_section_fails(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
            404, text="Page not Found", request=httpx.Request("GET", url)))
        assert ats_validation._probe_taleo("nobody/ex") is False


class TestDiscovery:
    @pytest.mark.parametrize("url,spec", [
        ("https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=337778&lang=en",
         "textron/textron"),
        ("https://wvu.taleo.net/careersection/faculty/jobdetail.ftl?job=28771", "wvu/faculty"),
        ("https://baesystems.taleo.net/careersection/2/jobsearch.ftl?lang=en", "baesystems/2"),
    ])
    def test_a_posting_link_names_its_board(self, url, spec):
        assert extract_slugs(url)["taleo"] == {spec}

    def test_taleos_own_endpoints_are_not_boards(self):
        assert "taleo" not in extract_slugs(
            "https://textron.taleo.net/careersection/rest/jobboard/searchjobs?portal=1")


def test_enrichment_reads_the_description_out_of_the_page():
    description = '<p>Who We Are\\: Textron Systems.</p>'
    qualifications = "<ul><li>BS in Computer Science</li></ul>"
    history = "!|!".join(["339064", "Software Engineer",
                          quote("!*!" + description), quote("!*!" + description),
                          quote("!*!" + qualifications), "Textron is committed"])
    page = f'<input type="hidden" name="initialHistory" id="initialHistory" value="{history}">'
    resp = MagicMock(status_code=200, text=page)
    client = MagicMock()
    client.get.return_value = resp
    url = "https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=339064&lang=en"
    assert enrichment.looks_like_ats(url)
    found = enrichment.enrich_one(client, url)
    assert found.description.count("Who We Are: Textron Systems.") == 1
    assert "BS in Computer Science" in found.description
    assert "Textron is committed" not in found.description


def test_boards_are_searched_by_the_roles_in_a_board_cycle(monkeypatch, db):
    from app.services import job_fetcher, tunables
    from app.models.profile import Profile

    calls = []
    serve(monkeypatch, [row(1, "Software Engineer")], calls)
    db.add(Profile(data={}))
    db.commit()
    cfg = tunables.effective_settings(db.query(Profile).first().data)
    jobs, stats = job_fetcher._run_all_adapters(
        ["Software Engineer"], ["Remote"], cfg, {"taleo": ["textron/textron"]}, {},
        only={"taleo"})
    assert stats["taleo"]["count"] == 1 and "taleo" in job_fetcher.SOURCE_GROUPS["boards"]
