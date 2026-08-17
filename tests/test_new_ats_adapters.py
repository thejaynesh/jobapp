"""
The five ATSes added in Phase 2.1.

Each ATS we speak makes every company hosted on it reachable, and starts the
discovery flywheel finding slugs for it in job links and career pages. Two
shapes here, deliberately:

* **BambooHR and Personio** have real public endpoints — a JSON list and an XML
  feed — so they read those.
* **iCIMS, Teamtailor and Jobvite** do not: their APIs are per-customer,
  undocumented, and move. What they all publish is `JobPosting` structured
  data, because their customers' openings appearing in Google's job results is
  the product. That is the more durable read: the endpoint can move, the
  structured data cannot without costing the customer their ranking.

Listing pages usually omit descriptions. That is fine now — enrichment fetches
them from the posting URL — and the tests say so rather than treating it as a
failure.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest


def _resp(payload=None, text="", status=200, content=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    resp.content = content if content is not None else text.encode()
    resp.headers = {"content-type": "application/json" if payload else "text/html"}
    resp.raise_for_status = MagicMock()
    return resp


def _ld_page(postings: str) -> str:
    return (
        "<html><head><script type=\"application/ld+json\">["
        + postings
        + "]</script></head><body>" + ("x" * 3000) + "</body></html>"
    )


def _posting(title, url, company="Acme", location="New York", **extra) -> str:
    import json as _json

    node = {
        "@type": "JobPosting",
        "title": title,
        "url": url,
        "hiringOrganization": {"name": company},
        "jobLocation": {"address": {"addressLocality": location, "addressRegion": "NY"}},
    }
    node.update(extra)
    return _json.dumps(node)


# ---------------------------------------------------------------------------
# BambooHR
# ---------------------------------------------------------------------------

class TestBambooHRAdapter:
    _LIST = {"result": [{
        "id": 1234,
        "jobOpeningName": "Senior Backend Engineer",
        "location": {"city": "Austin", "state": "TX"},
        "employmentStatusLabel": "Full-Time",
    }]}
    _DETAIL = {"jobOpeningShare": {
        "description": "<p>Build APIs.</p><ul><li>Python</li><li>Go</li></ul>",
    }}

    def _router(self, list_payload=None, detail_payload=None):
        def _get(url, **kwargs):
            if "/detail" in url:
                return _resp(detail_payload if detail_payload is not None else self._DETAIL)
            return _resp(list_payload if list_payload is not None else self._LIST)
        return _get

    def test_returns_standard_dicts(self):
        from app.services.sources.bamboohr import fetch
        with patch("httpx.get", side_effect=self._router()):
            results = fetch(company_slugs=["acme"])

        assert len(results) == 1
        job = results[0]
        assert job["source"] == "bamboohr"
        assert job["source_job_id"] == "1234"
        assert job["title"] == "Senior Backend Engineer"
        assert job["location"] == "Austin, TX"
        assert job["url"] == "https://acme.bamboohr.com/careers/1234"
        assert job["experience_level"] == "senior"

    def test_the_description_comes_from_the_detail_call_and_is_cleaned(self):
        from app.services.sources.bamboohr import fetch
        with patch("httpx.get", side_effect=self._router()):
            results = fetch(company_slugs=["acme"])
        assert results[0]["description"] == "Build APIs.\n\n- Python\n- Go"

    def test_a_moved_detail_wrapper_is_still_read(self):
        # The payload nests the posting under a wrapper whose name has moved
        # before; several are tried rather than one path.
        from app.services.sources.bamboohr import fetch
        with patch("httpx.get", side_effect=self._router(
            detail_payload={"result": {"description": "Flat wrapper."}}
        )):
            results = fetch(company_slugs=["acme"])
        assert results[0]["description"] == "Flat wrapper."

    def test_a_failed_detail_leaves_the_job_without_a_description(self):
        """Enrichment fetches it later; losing the whole job would be worse."""
        import httpx
        from app.services.sources.bamboohr import fetch

        def _get(url, **kwargs):
            if "/detail" in url:
                raise httpx.HTTPError("500")
            return _resp(self._LIST)

        with patch("httpx.get", side_effect=_get):
            results = fetch(company_slugs=["acme"])
        assert len(results) == 1
        assert results[0]["description"] == ""

    def test_rows_without_a_title_or_id_are_dropped(self):
        from app.services.sources.bamboohr import fetch
        payload = {"result": [
            {"id": 1, "jobOpeningName": ""},
            {"jobOpeningName": "No id here"},
            {"id": 2, "jobOpeningName": "Real Job"},
        ]}
        with patch("httpx.get", side_effect=self._router(list_payload=payload)):
            results = fetch(company_slugs=["acme"])
        assert [j["title"] for j in results] == ["Real Job"]

    def test_a_dead_slug_costs_only_itself(self):
        import httpx
        from app.services.sources.bamboohr import fetch

        def _get(url, **kwargs):
            if "dead." in url:
                raise httpx.HTTPError("404")
            if "/detail" in url:
                return _resp(self._DETAIL)
            return _resp(self._LIST)

        with patch("httpx.get", side_effect=_get):
            results = fetch(company_slugs=["dead", "acme"])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Personio
# ---------------------------------------------------------------------------

_PERSONIO_XML = """<?xml version="1.0" encoding="utf-8"?>
<workzag-jobs>
  <company>Acme GmbH</company>
  <position>
    <id>9911</id>
    <name>Backend Engineer</name>
    <office>Berlin</office>
    <department>Engineering</department>
    <createdAt>2026-08-01T09:00:00+02:00</createdAt>
    <jobDescriptions>
      <jobDescription>
        <name>Your mission</name>
        <value>&lt;p&gt;Build the platform.&lt;/p&gt;</value>
      </jobDescription>
      <jobDescription>
        <name>Your profile</name>
        <value>&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;li&gt;Go&lt;/li&gt;&lt;/ul&gt;</value>
      </jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>
"""


class TestPersonioAdapter:
    def test_returns_standard_dicts(self):
        from app.services.sources.personio import fetch
        with patch("httpx.get", return_value=_resp(text=_PERSONIO_XML)):
            results = fetch(company_slugs=["acme"])

        assert len(results) == 1
        job = results[0]
        assert job["source"] == "personio"
        assert job["source_job_id"] == "9911"
        assert job["title"] == "Backend Engineer"
        assert job["company"] == "Acme GmbH"
        assert job["location"] == "Berlin"
        assert job["url"] == "https://acme.jobs.personio.de/job/9911"
        assert job["posted_at"] == "2026-08-01T09:00:00+02:00"

    def test_every_description_section_is_kept(self):
        """
        Reading only the first section drops the requirements — which is the
        half the skill filter and the matcher actually care about.
        """
        from app.services.sources.personio import fetch
        with patch("httpx.get", return_value=_resp(text=_PERSONIO_XML)):
            results = fetch(company_slugs=["acme"])

        description = results[0]["description"]
        assert "Your mission" in description
        assert "Build the platform." in description
        assert "Your profile" in description
        assert "- Python" in description

    def test_html_in_the_feed_is_cleaned(self):
        from app.services.sources.personio import fetch
        with patch("httpx.get", return_value=_resp(text=_PERSONIO_XML)):
            results = fetch(company_slugs=["acme"])
        assert "<p>" not in results[0]["description"]

    def test_a_remote_office_is_flagged(self):
        from app.services.sources.personio import fetch
        remote = _PERSONIO_XML.replace("<office>Berlin</office>",
                                       "<office>Remote (Europe)</office>")
        with patch("httpx.get", return_value=_resp(text=remote)):
            results = fetch(company_slugs=["acme"])
        assert results[0]["is_remote"] is True

    def test_an_html_error_page_is_not_read_as_a_feed(self, caplog):
        from app.services.sources.personio import fetch
        with caplog.at_level(logging.ERROR):
            with patch("httpx.get", return_value=_resp(text="<html>Not found</html>")):
                results = fetch(company_slugs=["acme"])
        assert results == []
        assert "not XML" in caplog.text


# ---------------------------------------------------------------------------
# The structured-data boards: iCIMS, Teamtailor, Jobvite
# ---------------------------------------------------------------------------

class TestStructuredDataBoards:
    @pytest.mark.parametrize("module_name,source,slug,expected_host", [
        ("icims", "icims", "acme", "acme.icims.com"),
        ("teamtailor", "teamtailor", "acme", "acme.teamtailor.com"),
        ("jobvite", "jobvite", "acme", "jobs.jobvite.com/acme"),
    ])
    def test_postings_are_read_from_structured_data(
        self, module_name, source, slug, expected_host
    ):
        import importlib

        fetch = importlib.import_module(
            f"app.services.sources.{module_name}"
        ).fetch
        page = _ld_page(
            _posting("Backend Engineer", f"https://{expected_host}/jobs/778899/backend")
            + ","
            + _posting("Staff SRE", f"https://{expected_host}/jobs/778900/sre")
        )
        requested = []

        def _get(url, **kwargs):
            requested.append(url)
            return _resp(text=page)

        with patch("httpx.get", side_effect=_get):
            results = fetch(company_slugs=[slug])

        assert expected_host in requested[0]
        assert {j["title"] for j in results} == {"Backend Engineer", "Staff SRE"}
        assert all(j["source"] == source for j in results)
        assert results[0]["company"] == "Acme"
        assert results[0]["location"] == "New York, NY"

    def test_the_job_id_is_read_out_of_the_posting_url(self):
        from app.services.sources.teamtailor import fetch
        page = _ld_page(
            _posting("Backend Engineer", "https://acme.teamtailor.com/jobs/778899-backend")
        )
        with patch("httpx.get", return_value=_resp(text=page)):
            results = fetch(company_slugs=["acme"])
        assert results[0]["source_job_id"] == "778899"

    def test_a_listing_without_descriptions_is_not_a_failure(self):
        """
        Listing pages routinely omit them. Enrichment fetches the full posting
        from the URL each block carries, which is the case it was built for.
        """
        from app.services.sources.teamtailor import fetch
        page = _ld_page(_posting("Backend Engineer",
                                 "https://acme.teamtailor.com/jobs/1"))
        with patch("httpx.get", return_value=_resp(text=page)):
            results = fetch(company_slugs=["acme"])
        assert len(results) == 1
        assert results[0]["description"] == ""
        assert results[0]["url"] == "https://acme.teamtailor.com/jobs/1"

    def test_a_description_in_the_block_is_cleaned(self):
        from app.services.sources.teamtailor import fetch
        page = _ld_page(_posting(
            "Backend Engineer", "https://acme.teamtailor.com/jobs/1",
            description="<p>Build things.</p><ul><li>Python</li></ul>",
        ))
        with patch("httpx.get", return_value=_resp(text=page)):
            results = fetch(company_slugs=["acme"])
        assert results[0]["description"] == "Build things.\n\n- Python"

    def test_a_page_with_no_structured_data_says_so_rather_than_going_quiet(self, caplog):
        """
        Markup drift is the failure that looks exactly like "this board has no
        openings" — and stays unnoticed for months unless it is said out loud.
        """
        from app.services.sources.teamtailor import fetch
        redesigned = "<html><body>" + ("x" * 5000) + "</body></html>"

        with caplog.at_level(logging.WARNING):
            with patch("httpx.get", return_value=_resp(text=redesigned)):
                results = fetch(company_slugs=["acme"])

        assert results == []
        assert "no JobPosting structured data" in caplog.text

    def test_a_genuinely_empty_board_is_quiet(self):
        from app.services.sources.teamtailor import fetch
        with caplog_free_of_warnings() as caplog:
            with patch("httpx.get", return_value=_resp(text="<html></html>")):
                results = fetch(company_slugs=["acme"])
        assert results == []
        assert "structured data" not in caplog.text

    def test_icims_tries_both_host_shapes(self):
        from app.services.sources.icims import fetch
        page = _ld_page(_posting("Backend Engineer",
                                 "https://careers-acme.icims.com/jobs/5/backend/job"))
        requested = []

        def _get(url, **kwargs):
            requested.append(url)
            if url.startswith("https://acme.icims.com"):
                return _resp(text="<html></html>")  # the plain host has nothing
            return _resp(text=page)

        with patch("httpx.get", side_effect=_get):
            results = fetch(company_slugs=["acme"])

        assert len(requested) == 2
        assert "careers-acme.icims.com" in requested[1]
        assert len(results) == 1

    def test_jobvite_falls_back_from_the_search_route(self):
        from app.services.sources.jobvite import fetch
        page = _ld_page(_posting("Backend Engineer",
                                 "https://jobs.jobvite.com/acme/job/oAbC1"))
        requested = []

        def _get(url, **kwargs):
            requested.append(url)
            return _resp(text=page if not url.endswith("/search") else "<html></html>")

        with patch("httpx.get", side_effect=_get):
            results = fetch(company_slugs=["acme"])

        assert requested[0].endswith("/search")
        assert len(results) == 1


class caplog_free_of_warnings:
    """A tiny context manager so the quiet-board test can assert on log text."""

    def __enter__(self):
        import io

        self._stream = io.StringIO()
        self._handler = logging.StreamHandler(self._stream)
        self._handler.setLevel(logging.WARNING)
        logging.getLogger("app.services.sources").addHandler(self._handler)
        self.text = ""
        return self

    def __exit__(self, *exc):
        logging.getLogger("app.services.sources").removeHandler(self._handler)
        self.text = self._stream.getvalue()
        return False


# ---------------------------------------------------------------------------
# Registration — an adapter nobody calls is an adapter that does nothing
# ---------------------------------------------------------------------------

class TestTheyAreActuallyWiredUp:
    NEW = ("icims", "bamboohr", "teamtailor", "jobvite", "personio")

    def test_each_has_a_config_field_and_a_discovery_pattern(self):
        from app.services.ats_discovery import ALL_ATS, ATS_CONFIG_FIELDS, ATS_PATTERNS

        for ats in self.NEW:
            assert ats in ATS_CONFIG_FIELDS, ats
            assert ats in ATS_PATTERNS, ats
            assert ats in ALL_ATS, ats

    def test_each_config_field_exists_on_settings(self):
        from app.config import settings
        from app.services.ats_discovery import ATS_CONFIG_FIELDS

        for ats in self.NEW:
            assert hasattr(settings, ATS_CONFIG_FIELDS[ats]), ats

    def test_every_ats_has_a_validation_probe(self):
        # A board with no probe is never checked before being polled forever.
        from app.services.ats_discovery import ALL_ATS
        from app.services.ats_validation import PROBES

        assert set(PROBES) == set(ALL_ATS)

    def test_each_can_be_triggered_from_the_runs_page(self):
        from app.routers.runs import TRIGGERABLE_SOURCES

        for ats in self.NEW:
            assert ats in TRIGGERABLE_SOURCES, ats

    def test_the_fetch_cycle_calls_them(self):
        from app.services.job_fetcher import _run_all_adapters

        slugs = {ats: ["acme"] for ats in self.NEW}
        called = {}

        def _stub(name):
            def _fetch(company_slugs=None, **kwargs):
                called[name] = list(company_slugs or [])
                return []
            return _fetch

        patches = [
            patch(f"app.services.sources.{name}.fetch", _stub(name))
            for name in self.NEW
        ]
        for p in patches:
            p.start()
        try:
            from app.config import settings
            _, stats = _run_all_adapters(
                ["Backend Engineer"], ["NYC"], settings,
                ats_slugs=slugs, only=set(self.NEW),
            )
        finally:
            for p in patches:
                p.stop()

        assert called == {name: ["acme"] for name in self.NEW}
        for name in self.NEW:
            assert stats[name]["enabled"] is True

    def test_discovery_finds_slugs_for_each_new_ats(self):
        from app.services.ats_discovery import extract_slugs

        text = (
            "https://careers-globex.icims.com/jobs/1/x/job "
            "https://initech.bamboohr.com/careers/22 "
            "https://umbrella.teamtailor.com/jobs/33 "
            "https://jobs.jobvite.com/soylent/job/oX1 "
            "https://tyrell.jobs.personio.de/job/44"
        )
        found = extract_slugs(text)
        assert found["icims"] == {"globex"}
        assert found["bamboohr"] == {"initech"}
        assert found["teamtailor"] == {"umbrella"}
        assert found["jobvite"] == {"soylent"}
        assert found["personio"] == {"tyrell"}

    def test_the_jobvite_click_tracker_is_never_read_as_a_board(self):
        """
        click.jobvite.com is a redirect middleman (see link_resolver). Reading
        a slug out of one would register the tracker itself as a company.
        """
        from app.services.ats_discovery import extract_slugs

        found = extract_slugs("https://click.jobvite.com/e/abc?u=1")
        assert not found.get("jobvite")
