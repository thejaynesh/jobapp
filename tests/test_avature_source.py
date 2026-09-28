"""
Avature portals, read from the sitemap each one names in robots.txt and the
pages of the new postings matching the roles. No network: `httpx.get` is
routed to fixtures shaped like the live portals (measured 2026-09-28).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models.job import Job
from app.models.profile import Profile
from app.services import enrichment, job_fetcher, liveness, tunables
from app.services.ats_discovery import extract_slugs
from app.services.ats_validation import is_valid_slug
from app.services.sources import avature, base

TODAY = datetime.now(timezone.utc).date()
HOST = "acme.avature.net"
SPEC = f"{HOST}/careers"
BODY = "<p>Build distributed systems in Python and Go. " + "Ship them. " * 30 + "</p>"


def field(label, value, outer="div", label_tag="div", value_tag="div"):
    label_html = (f'<{label_tag} class="article__content__view__field__label"> {label} '
                  f'</{label_tag}>') if label is not None else ""
    return (f'<{outer} class="article__content__view__field ">{label_html}'
            f'<{value_tag} class="article__content__view__field__value"> {value} '
            f'</{value_tag}></{outer}>')


def page(title, fields=None, site="Acme", extra=""):
    fields = fields if fields is not None else [
        field(None, f"<h3>{title}</h3>"),
        field("Location", "New York"),
        field("Ref #", "10054138"),
        field(None, BODY),
    ]
    return (f'<html><head><title> {title} - 1 - {site} </title>'
            f'<meta property="og:title" content="{title}" />'
            f'<meta property="og:site_name" content="{site}" />{extra}</head><body>'
            f'<div class="article__content__view">{"".join(fields)}</div></body></html>')


def entry(n, slug, age_days=1, locale=""):
    lastmod = (TODAY - timedelta(days=age_days)).isoformat()
    return (f"<url><loc>https://{HOST}/{locale}careers/JobDetail/{slug}/{n}</loc>"
            f"<lastmod>{lastmod}</lastmod></url>")


class Portal:
    """robots.txt → sitemap index → one sitemap per language → posting pages."""

    def __init__(self, monkeypatch, entries, pages=None, robots=None):
        self.entries = entries
        self.pages = pages or {}
        self.robots = robots if robots is not None else (
            f"User-agent: *\nAllow: /careers\nDisallow: /careers/*qtvc=\n"
            f"Sitemap: https://{HOST}/careers/sitemap_index.xml\n"
            f"Sitemap: https://{HOST}/events/sitemap_index.xml\n")
        self.calls: list[str] = []
        monkeypatch.setattr(httpx, "get", self.get)

    def get(self, url, **kw):
        self.calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=self.robots, request=request)
        if url.endswith("/careers/sitemap_index.xml"):
            children = "".join(f"<sitemap><loc>https://{HOST}/{lang}/careers/sitemap.xml</loc></sitemap>"
                               for lang in ("es_ES", "en_US", "de_DE"))
            return httpx.Response(200, text=f'<?xml version="1.0"?><sitemapindex>{children}</sitemapindex>',
                                  request=request)
        if url.endswith("/en_US/careers/sitemap.xml"):
            static = f"<url><loc>https://{HOST}/careers/SearchJobs</loc></url>"
            return httpx.Response(200, text=f"<urlset>{static}{''.join(self.entries)}</urlset>",
                                  request=request)
        if "/JobDetail/" in url:
            job_id = url.rstrip("/").rsplit("/", 1)[1]
            html = self.pages.get(job_id)
            if html is None:
                # A closed posting: the portal redirects to its Error page.
                return httpx.Response(200, text="<html>Error</html>",
                                      request=httpx.Request("GET", f"https://{HOST}/careers/Error"))
            return httpx.Response(200, text=html, request=request)
        return httpx.Response(404, request=request)

    def pages_read(self):
        return sorted(u.rsplit("/", 1)[1] for u in self.calls if "/JobDetail/" in u)


@pytest.fixture(autouse=True)
def _fresh_memo():
    avature._SEEN.clear()
    yield
    avature._SEEN.clear()


def run(specs=(SPEC,), queries=("Software Engineer",), known=(), max_age_days=30):
    with base.known_descriptions({"avature": set(known)}):
        return avature.fetch(list(specs), list(queries), max_age_days=max_age_days)


# --- The page -----------------------------------------------------------------

class TestPage:
    def test_labelled_fields_and_the_text_around_them(self):
        found = avature.parse_detail(page("Senior Software Engineer - VAULT"))
        assert found["title"] == "Senior Software Engineer - VAULT"
        assert found["location"] == "New York" and found["company"] == "Acme"
        assert "distributed systems" in found["description"]
        assert "10054138" not in found["description"]

    def test_definition_lists_as_well_as_divs(self):
        """TotalEnergies' portal writes its fields as dl/dt/dd."""
        fields = [field("Country", "United States", "dl", "dt", "dd"),
                  field("Location", "Houston", "dl", "dt", "dd"),
                  field(None, BODY)]
        found = avature.parse_detail(page("Data Engineer", fields))
        assert found["location"] == "Houston" and "distributed systems" in found["description"]

    def test_city_state_and_country_as_separate_fields(self):
        fields = [field("Country", "USA"), field("State", "Ohio"), field("City:", "Cincinnati"),
                  field("Job Description", BODY)]
        assert avature.parse_detail(page("Claims Specialist", fields))["location"] \
            == "Cincinnati, Ohio, USA"

    @pytest.mark.parametrize("label,value,expected", [
        ("Date Published", "09-25-2026", "2026-09-25"),
        ("Posted date", "28-Aug-2026", "2026-08-28"),
        ("Date", "Wednesday, September 23, 2026", "2026-09-23"),
    ])
    def test_a_posted_date_in_the_portals_own_words(self, label, value, expected):
        fields = [field("Closed date", "30-Sep-2026"), field(label, value), field(None, BODY)]
        assert avature.parse_detail(page("Engineer", fields))["posted_at"].startswith(expected)

    def test_employment_type_from_what_the_portal_wrote(self):
        fields = [field("Work Location Type:", "On-site"),
                  field("Employment Type:", "Full-time (30+ hrs/week)/FULLTIME"), field(None, BODY)]
        found = avature.parse_detail(page("Engineer", fields))
        assert found["employment_type"] == "full_time" and not found["remote"]

    def test_a_title_escaped_twice_and_a_template_site_name(self):
        found = avature.parse_detail(page("Crane &amp;amp; Rigging Supervisor", site="Company"))
        assert found["title"] == "Crane & Rigging Supervisor"
        assert found["company"] == ""   # left for the registry's name

    def test_the_company_field_names_the_subsidiary(self):
        fields = [field("Location(s)", "Lisle, Illinois"), field("Company", "Molex"),
                  field(None, BODY)]
        found = avature.parse_detail(page("Software Engineer", fields, site="Koch"))
        assert found["company"] == "Molex" and "Molex" not in found["description"]

    def test_the_older_template_and_json_ld(self):
        older = page("Client Service Coordinator", fields=[],
                     extra="") .replace("<body>", f'<body><div class="crmDescription">{BODY}</div>')
        assert "distributed systems" in avature.parse_detail(older)["description"]
        ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "AI Architect",'
              '"description": "<p>Design agents.</p>", "datePosted": "2026-09-20",'
              '"jobLocation": {"@type": "Place", "address": {"addressLocality": "Austin",'
              '"addressRegion": "TX", "addressCountry": "US"}}}</script>')
        found = avature.parse_detail(page("AI Architect", fields=[], extra=ld))
        assert "Design agents" in found["description"] and found["posted_at"] == "2026-09-20"
        assert "Austin" in found["location"]

    def test_a_page_with_nothing_on_it_is_no_posting(self):
        assert avature.parse_detail("<html><head><title>Login</title></head></html>") is None


# --- The sitemap -------------------------------------------------------------

class TestSitemap:
    def test_robots_names_the_sitemap_and_english_is_read(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(1, "Software-Engineer"), entry(2, "3915")])
        found = avature.sitemap(HOST, "careers")
        assert [(e["id"], e["title"]) for e in found] == [("1", "Software Engineer"), ("2", "")]
        assert f"https://{HOST}/en_US/careers/sitemap.xml" in portal.calls

    def test_a_portal_robots_names_no_sitemap_for_is_not_read(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(1, "Software-Engineer")])
        assert avature.sitemap(HOST, "internalcareers") is None
        assert portal.calls == [f"https://{HOST}/robots.txt"]

    def test_a_bot_challenge_is_a_refusal(self, monkeypatch):
        def get(url, **kw):
            return httpx.Response(202, headers={"x-amzn-waf-action": "challenge"},
                                  request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", get)
        with pytest.raises(avature.Blocked):
            avature.sitemap("ibmglobal.avature.net", "careers")


# --- A cycle ----------------------------------------------------------------

class TestFetch:
    def test_matching_titles_are_read_and_the_rest_are_not(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(1, "Senior-Software-Engineer"),
                                      entry(2, "Tax-Accountant")],
                        pages={"1": page("Senior Software Engineer"), "2": page("Tax Accountant")})
        [job] = run()
        assert portal.pages_read() == ["1"]
        assert job["source"] == "avature" and job["source_job_id"] == f"{HOST}:1"
        assert job["url"] == f"https://{HOST}/careers/JobDetail/Senior-Software-Engineer/1"
        assert job["posted_at"].startswith(str(TODAY - timedelta(days=1)))
        assert job["ats_slug"] == SPEC

    def test_a_number_where_the_title_goes_is_read_to_find_out(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(7, "7190"), entry(8, "7191")],
                        pages={"7": page("Software Engineer II"), "8": page("Estimator")})
        assert [j["title"] for j in run()] == ["Software Engineer II"]
        assert portal.pages_read() == ["7", "8"]
        # …once: neither is read again next cycle.
        portal.calls.clear()
        assert run() == [] and portal.pages_read() == []

    def test_postings_already_stored_with_their_text_are_not_read(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(1, "Software-Engineer"), entry(2, "Software-Engineer")],
                        pages={"1": page("Software Engineer"), "2": page("Software Engineer")})
        assert [j["source_job_id"] for j in run(known={f"{HOST}:1"})] == [f"{HOST}:2"]
        assert portal.pages_read() == ["2"]

    def test_too_old_to_keep_is_not_read(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(1, "Software-Engineer", age_days=90)],
                        pages={"1": page("Software Engineer")})
        assert run() == [] and portal.pages_read() == []

    def test_a_closed_posting_the_sitemap_still_lists_is_skipped(self, monkeypatch):
        Portal(monkeypatch, [entry(1, "Software-Engineer")], pages={})
        assert run() == []

    def test_every_listed_posting_is_reported_seen(self, monkeypatch):
        Portal(monkeypatch, [entry(1, "Software-Engineer"), entry(2, "Accountant", age_days=90)],
               pages={"1": page("Software Engineer")})
        with base.collect_board_sightings() as seen:
            run()
        assert seen == {("avature", SPEC): {f"{HOST}:1", f"{HOST}:2"}}

    def test_no_roles_no_requests(self, monkeypatch):
        portal = Portal(monkeypatch, [entry(1, "Software-Engineer")])
        assert run(queries=()) == [] and portal.calls == []


class TestTheSettingsPageControlsIt:
    def _run(self, db, overrides):
        db.query(Profile).delete()
        db.add(Profile(data={tunables.STORE_KEY: overrides}))
        db.commit()
        cfg = tunables.effective_settings(db.query(Profile).first().data)
        return job_fetcher._run_all_adapters(
            ["Software Engineer"], ["Remote"], cfg, {"avature": [SPEC]}, {}, only={"avature"})

    def _portal(self, monkeypatch):
        entries = [entry(n, "Software-Engineer", age_days=n) for n in range(1, 6)]
        return Portal(monkeypatch, entries,
                      pages={str(n): page(f"Software Engineer {n}") for n in range(1, 6)})

    def test_pages_per_portal(self, db, monkeypatch):
        portal = self._portal(monkeypatch)
        jobs, stats = self._run(db, {"avature_max_details": 2})
        # The newest two.
        assert portal.pages_read() == ["1", "2"] and stats["avature"]["count"] == 2

    def test_zero_reads_none(self, db, monkeypatch):
        portal = self._portal(monkeypatch)
        jobs, _ = self._run(db, {"avature_max_details": 0})
        assert jobs == [] and portal.pages_read() == []

    def test_the_maximum_job_age_is_the_settings_pages_too(self, db, monkeypatch):
        portal = self._portal(monkeypatch)
        # Dated by the sitemap: day 3 is inside four days, day 4 is not.
        self._run(db, {"max_job_age_days": 4})
        assert portal.pages_read() == ["1", "2", "3"]

    def test_avature_is_a_searched_full_feed_board(self):
        assert "avature" in job_fetcher.SOURCE_GROUPS["boards"]
        assert "avature" in job_fetcher.FULL_FEED_BOARDS


def test_a_posting_gone_from_the_sitemap_is_closed(db, monkeypatch):
    from tests.test_fetch_task import _make_profile_with_targets

    _make_profile_with_targets(db)
    portal = Portal(monkeypatch, [entry(1, "Software-Engineer"), entry(2, "Software-Engineer-II")],
                    pages={"1": page("Software Engineer"), "2": page("Software Engineer II")})

    def cycle():
        def run_adapters(*args, **kwargs):
            jobs = avature.fetch([SPEC], ["Software Engineer"], max_age_days=30)
            return jobs, {"avature": {"count": len(jobs), "errors": [], "enabled": True}}

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)), \
             patch("app.services.job_fetcher._run_all_adapters", side_effect=run_adapters):
            return job_fetcher.fetch_and_save_jobs(db)

    cycle()
    stored = db.query(Job).filter_by(source="avature").all()
    assert {j.board for j in stored} == {f"avature:{SPEC}"} and len(stored) == 2
    portal.entries = [entry(1, "Software-Engineer")]
    assert cycle()["closed"] == 1
    assert portal.pages_read() == ["1", "2"]   # nothing read twice
    gone = db.query(Job).filter_by(source="avature", source_job_id=f"{HOST}:2").one()
    assert gone.closed_note == job_fetcher.VANISHED_NOTE


# --- Finding portals ----------------------------------------------------------

class TestDiscovery:
    @pytest.mark.parametrize("url,spec", [
        ("https://bloomberg.avature.net/careers/JobDetail/Senior-Software-Engineer/22344",
         "bloomberg.avature.net/careers"),
        ("https://koch.avature.net/en_US/CollegeRecruiting/JobDetail/Intern/178314",
         "koch.avature.net/CollegeRecruiting"),
        ("https://Pomerleau.avature.net/en_US/Jobs/JobDetail/7190/3915",
         "pomerleau.avature.net/Jobs"),
    ])
    def test_a_posting_link_names_its_portal(self, url, spec):
        assert extract_slugs(url).get("avature") == {spec}

    @pytest.mark.parametrize("url", [
        "https://bloomberg.avature.net/internalcareers/JobDetail/x/1",
        "https://sandboxea.avature.net/careers/JobDetail/x/1",
        "https://deloittebe.avature.net/examplePathName/JobDetail/x/1",
        "https://bloomberg.avature.net/careers/Login",
    ])
    def test_internal_sandbox_and_template_portals_are_not(self, url):
        assert "avature" not in extract_slugs(url)


class TestProbe:
    def test_a_portal_with_a_readable_posting_passes(self, monkeypatch):
        Portal(monkeypatch, [entry(1, "Software-Engineer")], pages={"1": page("Software Engineer")})
        assert is_valid_slug("avature", SPEC)

    def test_one_whose_postings_redirect_fails(self, monkeypatch):
        """Internal portals send every posting to a login."""
        Portal(monkeypatch, [entry(1, "Software-Engineer")], pages={})
        assert not is_valid_slug("avature", SPEC)

    def test_one_robots_does_not_name_fails(self, monkeypatch):
        Portal(monkeypatch, [entry(1, "Software-Engineer")], robots="User-agent: *\n")
        assert not is_valid_slug("avature", SPEC)

    def test_a_blocked_tenant_fails_rather_than_passing_as_network_trouble(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
            202, request=httpx.Request("GET", url)))
        assert not is_valid_slug("avature", "ibmglobal.avature.net/careers")


# --- After the fetch ------------------------------------------------------------

def test_enrichment_reads_a_posting_linked_from_elsewhere():
    url = "https://bloomberg.avature.net/careers/JobDetail/Senior-Software-Engineer/22344"
    client = MagicMock()
    client.get.return_value = httpx.Response(200, text=page("Senior Software Engineer"),
                                             request=httpx.Request("GET", url))
    assert enrichment.looks_like_ats(url)
    found = enrichment.enrich_one(client, url)
    assert "distributed systems" in found.description
    assert found.details.get("location") == "New York"


def test_liveness_reads_the_error_redirect_as_closed():
    url = "https://jobsearch.harman.com/en_US/careers/JobDetail/Engineer/32481"
    response = MagicMock(status_code=200, text="<html>Error</html>",
                         url="https://jobsearch.harman.com/en_US/careers/Error",
                         headers={"content-type": "text/html"})
    client = MagicMock()
    client.get.return_value = response
    assert liveness.check_url(url, client).state == "closed"
