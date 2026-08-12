"""
Reading interview writeups out of the three free sources.

These run against canned responses, not the live sites — the build environment
cannot reach any of them. That bounds what the tests prove: they show the
parsing handles the documented response shapes and degrades sensibly, and they
say nothing about whether those shapes are still current. The GeeksforGeeks
selectors in particular are inference rather than observation.

Which is why the loudest thing tested here is the diagnostics. A source that has
quietly started returning nothing is the failure that matters, because it is
indistinguishable from a company nobody has written about.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services import interview_sources as sources

NOW = datetime.now(timezone.utc)


def client_returning(handler) -> httpx.Client:
    """An httpx client wired to a function instead of the network."""
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})


def reddit_payload(**overrides):
    post = {
        "title": "Acme interview experience — SDE-1",
        "selftext": "Three rounds: OA, phone screen, onsite. " * 5,
        "created_utc": (NOW - timedelta(days=20)).timestamp(),
        "permalink": "/r/leetcode/comments/abc/acme/",
    }
    post.update(overrides)
    return {"data": {"children": [{"data": post}]}}


class TestReddit:
    def test_reads_a_writeup(self):
        client = client_returning(lambda r: httpx.Response(200, json=reddit_payload()))
        result = sources.fetch_reddit("Acme", client=client)
        assert result.count >= 1
        first = result.reports[0]
        assert first["source"] == "reddit"
        assert first["url"].startswith("https://www.reddit.com/r/leetcode/")

    def test_dates_come_from_created_utc(self):
        client = client_returning(lambda r: httpx.Response(200, json=reddit_payload()))
        report = sources.fetch_reddit("Acme", client=client).reports[0]
        assert isinstance(report["posted_at"], datetime)
        assert report["posted_at"].tzinfo is not None

    def test_picks_up_the_level_from_the_title(self):
        client = client_returning(lambda r: httpx.Response(200, json=reddit_payload()))
        assert sources.fetch_reddit("Acme", client=client).reports[0]["role_hint"]

    def test_skips_posts_that_are_not_experience_reports(self):
        # "How do I prepare for Acme?" is a question, not a report.
        payload = reddit_payload(title="How should I prepare?", selftext="Any tips?")
        client = client_returning(lambda r: httpx.Response(200, json=payload))
        assert sources.fetch_reddit("Acme", client=client).count == 0

    def test_skips_undated_posts(self):
        payload = reddit_payload(created_utc=None)
        client = client_returning(lambda r: httpx.Response(200, json=payload))
        assert sources.fetch_reddit("Acme", client=client).count == 0

    def test_an_http_error_is_reported_not_raised(self):
        client = client_returning(lambda r: httpx.Response(503))
        result = sources.fetch_reddit("Acme", client=client)
        assert result.count == 0
        assert result.error

    def test_malformed_json_is_reported_not_raised(self):
        client = client_returning(lambda r: httpx.Response(200, text="not json"))
        result = sources.fetch_reddit("Acme", client=client)
        assert result.error

    def test_an_empty_company_fetches_nothing(self):
        assert sources.fetch_reddit("  ").count == 0


class TestGitHub:
    def _payload(self, **overrides):
        repo = {
            "full_name": "someone/acme-interview-questions",
            "name": "acme-interview-questions",
            "description": "Acme interview questions collection",
            "html_url": "https://github.com/someone/acme-interview-questions",
            "pushed_at": (NOW - timedelta(days=40)).isoformat().replace("+00:00", "Z"),
        }
        repo.update(overrides)
        return {"items": [repo]}

    def test_reads_a_repository(self):
        client = client_returning(lambda r: httpx.Response(200, json=self._payload()))
        result = sources.fetch_github("Acme", client=client)
        assert result.count == 1
        assert result.reports[0]["source"] == "github"

    def test_dates_from_the_last_push(self):
        # An unmaintained list describes an old loop, so its staleness is signal.
        client = client_returning(lambda r: httpx.Response(200, json=self._payload()))
        assert isinstance(sources.fetch_github("Acme", client=client).reports[0]["posted_at"], datetime)

    def test_repositories_that_do_not_name_the_company_are_dropped(self):
        # The search is fuzzy enough to return generic interview repos.
        payload = self._payload(
            name="coding-interview-university",
            description="A complete computer science study plan",
            full_name="jwasham/coding-interview-university",
        )
        client = client_returning(lambda r: httpx.Response(200, json=payload))
        assert sources.fetch_github("Acme", client=client).count == 0

    def test_an_undated_repository_is_dropped(self):
        payload = self._payload(pushed_at=None, updated_at=None)
        client = client_returning(lambda r: httpx.Response(200, json=payload))
        assert sources.fetch_github("Acme", client=client).count == 0

    def test_a_rate_limit_is_reported_not_raised(self):
        client = client_returning(lambda r: httpx.Response(403, json={"message": "rate limited"}))
        result = sources.fetch_github("Acme", client=client)
        assert result.count == 0
        assert result.error


GFG_ARTICLE = """
<html><head>
<title>Acme Interview Experience for SDE-1 (On-Campus)</title>
<meta property="article:published_time" content="2026-06-01T09:00:00+00:00">
</head><body>
<div class="content"><p>Round 1: online assessment with two problems.</p>
<p>Round 2: technical phone screen on graphs and dynamic programming.</p>
<p>Round 3: onsite, three interviews plus a hiring manager chat.</p>
<p>%s</p></div></body></html>
""" % ("Detailed notes about each round. " * 20)


class TestGeeksForGeeksParsing:
    def test_reads_an_article(self):
        parsed = sources.parse_gfg_article(GFG_ARTICLE, "https://gfg/x", "Acme")
        assert parsed is not None
        assert parsed["source"] == "geeksforgeeks"
        assert "online assessment" in parsed["body"]

    def test_takes_the_date_from_structured_metadata(self):
        # Metadata rather than the rendered page: it is what the site emits for
        # search engines and changes far less often than the layout.
        parsed = sources.parse_gfg_article(GFG_ARTICLE, "https://gfg/x", "Acme")
        assert parsed["posted_at"].year == 2026
        assert parsed["posted_at"].month == 6

    def test_reads_json_ld_dates_too(self):
        html = GFG_ARTICLE.replace(
            '<meta property="article:published_time" content="2026-06-01T09:00:00+00:00">',
            '<script type="application/ld+json">{"datePublished":"2026-05-02T00:00:00Z"}</script>',
        )
        parsed = sources.parse_gfg_article(html, "https://gfg/x", "Acme")
        assert parsed["posted_at"].month == 5

    def test_an_article_with_no_date_is_refused(self):
        # The corpus cannot rank it, so storing it would only dilute retrieval.
        html = GFG_ARTICLE.replace(
            '<meta property="article:published_time" content="2026-06-01T09:00:00+00:00">', ""
        )
        assert sources.parse_gfg_article(html, "https://gfg/x", "Acme") is None

    def test_a_stub_page_is_refused(self):
        html = '<html><head><meta name="datePublished" content="2026-06-01T00:00:00Z"></head><body>404</body></html>'
        assert sources.parse_gfg_article(html, "https://gfg/x", "Acme") is None

    def test_markup_is_stripped_from_the_body(self):
        parsed = sources.parse_gfg_article(GFG_ARTICLE, "https://gfg/x", "Acme")
        assert "<p>" not in parsed["body"]

    def test_the_level_is_read_from_the_title(self):
        parsed = sources.parse_gfg_article(GFG_ARTICLE, "https://gfg/x", "Acme")
        assert parsed["role_hint"] and "sde" in parsed["role_hint"].lower()


class TestGeeksForGeeksFetching:
    def test_follows_index_links_to_articles(self):
        def handler(request):
            if "tag/" in str(request.url):
                return httpx.Response(
                    200,
                    text='<a href="https://www.geeksforgeeks.org/acme-interview-experience-sde-1/">Acme</a>',
                )
            return httpx.Response(200, text=GFG_ARTICLE)

        result = sources.fetch_geeksforgeeks("Acme", client=client_returning(handler))
        assert result.count == 1

    def test_an_index_with_no_matching_links_says_so(self):
        # The failure this exists to prevent: a redesign that breaks link
        # discovery looks exactly like a company nobody wrote about.
        client = client_returning(lambda r: httpx.Response(200, text="<html>nothing here</html>"))
        result = sources.fetch_geeksforgeeks("Acme", client=client)
        assert result.count == 0
        assert result.error and "no interview-experience links" in result.error

    def test_a_dead_index_is_reported(self):
        client = client_returning(lambda r: httpx.Response(404))
        result = sources.fetch_geeksforgeeks("Acme", client=client)
        assert result.count == 0
        assert result.error

    def test_an_empty_company_fetches_nothing(self):
        assert sources.fetch_geeksforgeeks("").count == 0


class TestFetchAll:
    """
    Patching goes through FETCHERS, not the module attributes.

    `FETCHERS` binds the function objects at import, so setattr on the module
    would leave `fetch_all` calling the originals — and against a blocked
    network those raise, get caught, and produce exactly the error entries the
    test was asserting on. Passing for the wrong reason.
    """

    @staticmethod
    def _stub(name, reports=None, raises=None):
        def fetcher(company, **kwargs):
            if raises:
                raise raises
            return sources.SourceResult(name, list(reports or []))

        return fetcher

    def test_one_source_failing_does_not_stop_the_others(self, monkeypatch):
        monkeypatch.setitem(
            sources.FETCHERS, "reddit", self._stub("reddit", [{"source": "reddit"}])
        )
        monkeypatch.setitem(
            sources.FETCHERS, "github", self._stub("github", raises=RuntimeError("boom"))
        )
        monkeypatch.setitem(sources.FETCHERS, "geeksforgeeks", self._stub("geeksforgeeks"))

        outcome = sources.fetch_all("Acme")
        assert "boom" in outcome["sources"]["github"]["error"]
        assert outcome["sources"]["reddit"]["count"] == 1
        # A corpus built from two of three sources beats an exception.
        assert len(outcome["reports"]) == 1

    def test_every_source_reports_its_own_count(self, monkeypatch):
        monkeypatch.setitem(
            sources.FETCHERS, "reddit", self._stub("reddit", [{"source": "reddit"}])
        )
        monkeypatch.setitem(sources.FETCHERS, "github", self._stub("github"))
        monkeypatch.setitem(sources.FETCHERS, "geeksforgeeks", self._stub("geeksforgeeks"))

        outcome = sources.fetch_all("Acme")
        assert set(outcome["sources"]) == {"reddit", "github", "geeksforgeeks"}
        assert outcome["sources"]["github"]["count"] == 0

    def test_a_source_reporting_an_error_still_reports_its_count(self, monkeypatch):
        # Zero with an error attached is the signal that something broke; zero
        # on its own only means nobody wrote about this company.
        def failing(company, **kwargs):
            return sources.SourceResult("geeksforgeeks", [], error="index moved")

        monkeypatch.setitem(sources.FETCHERS, "reddit", self._stub("reddit"))
        monkeypatch.setitem(sources.FETCHERS, "github", self._stub("github"))
        monkeypatch.setitem(sources.FETCHERS, "geeksforgeeks", failing)

        outcome = sources.fetch_all("Acme")
        assert outcome["sources"]["geeksforgeeks"] == {"count": 0, "error": "index moved"}
        assert outcome["sources"]["reddit"]["error"] is None

    def test_can_be_limited_to_one_source(self, monkeypatch):
        monkeypatch.setitem(sources.FETCHERS, "reddit", self._stub("reddit"))
        outcome = sources.fetch_all("Acme", only={"reddit"})
        assert set(outcome["sources"]) == {"reddit"}
