from unittest.mock import MagicMock, patch

from app.services.ats_sniffer import (
    company_host,
    domain_slug,
    sniff_host,
    sniff_hosts,
)


def _resp(text: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    return resp


def _fake_client(responses: dict):
    client = MagicMock()

    def _get(url, **kwargs):
        result = responses.get(url)
        if result is None:
            return _resp(status=404)
        if isinstance(result, Exception):
            raise result
        return result

    client.get.side_effect = _get
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx


class TestCompanyHost:
    def test_accepts_a_company_careers_site(self):
        assert company_host("https://careers.acme.com/job/1") == "careers.acme.com"

    def test_rejects_known_ats_and_social_hosts(self):
        assert company_host("https://boards.greenhouse.io/stripe/jobs/1") is None
        assert company_host("https://acme.wd5.myworkdayjobs.com/x") is None
        assert company_host("https://github.com/acme/jobs") is None
        assert company_host("https://lnkd.in/abc") is None
        assert company_host("not-a-url") is None


class TestDomainSlug:
    def test_strips_careers_subdomain_and_tld(self):
        assert domain_slug("careers.acme.com") == "acme"
        assert domain_slug("jobs.acme.io") == "acme"
        assert domain_slug("www.acme.com") == "acme"

    def test_handles_compound_tlds(self):
        assert domain_slug("careers.acme.co.uk") == "acme"

    def test_returns_none_when_there_is_nothing_to_guess(self):
        assert domain_slug("localhost") is None
        assert domain_slug("careers.com") is None


class TestSniffHost:
    def test_mines_the_seed_html_without_any_request(self):
        html = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">'
        with patch("app.services.ats_sniffer.httpx.Client") as client:
            found = sniff_host("careers.acme.com", seed_html=html)
        client.assert_not_called()
        assert found == {"greenhouse": ["acme"]}

    def test_falls_back_to_fetching_the_careers_page(self):
        responses = {
            "https://careers.acme.com/careers": _resp(
                '<a href="https://jobs.lever.co/acme">Open roles</a>'
            )
        }
        with patch("app.services.ats_sniffer.httpx.Client",
                   return_value=_fake_client(responses)):
            found = sniff_host("careers.acme.com")
        assert found == {"lever": ["acme"]}

    def test_guesses_the_slug_from_the_domain_when_markup_is_silent(self):
        with patch("app.services.ats_sniffer.httpx.Client",
                   return_value=_fake_client({})), \
             patch("app.services.ats_validation.is_valid_slug",
                   side_effect=lambda ats, slug: ats == "ashby" and slug == "acme"):
            found = sniff_host("careers.acme.com")
        assert found == {"ashby": ["acme"]}

    def test_returns_nothing_when_every_avenue_fails(self):
        with patch("app.services.ats_sniffer.httpx.Client",
                   return_value=_fake_client({})), \
             patch("app.services.ats_validation.is_valid_slug", return_value=False):
            assert sniff_host("careers.acme.com") == {}


class TestSniffHosts:
    _EMBED = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme">'

    def test_collects_boards_and_caches_them_by_host(self):
        merged, cache, per_host = sniff_hosts({"careers.acme.com": self._EMBED})
        assert merged == {"greenhouse": ["acme"]}
        assert per_host["careers.acme.com"] == {"greenhouse": ["acme"]}
        assert cache["careers.acme.com"]["found"] == {"greenhouse": ["acme"]}

    def test_a_cached_host_is_never_sniffed_again(self):
        _, cache, _ = sniff_hosts({"careers.acme.com": self._EMBED})
        with patch("app.services.ats_sniffer._safe_sniff") as sniff:
            merged, _, _ = sniff_hosts({"careers.acme.com": ""}, cache)
        sniff.assert_not_called()
        assert merged == {"greenhouse": ["acme"]}

    def test_a_cached_miss_is_also_respected(self):
        with patch("app.services.ats_sniffer._safe_sniff", return_value={}):
            _, cache, _ = sniff_hosts({"careers.acme.com": "<html>nothing</html>"})
        assert cache["careers.acme.com"]["found"] == {}
        with patch("app.services.ats_sniffer._safe_sniff") as sniff:
            merged, _, _ = sniff_hosts({"careers.acme.com": ""}, cache)
        sniff.assert_not_called()
        assert merged == {}

    def test_respects_the_per_cycle_host_budget(self):
        hosts = {f"careers.acme{i}.com": self._EMBED for i in range(5)}
        with patch("app.services.ats_sniffer._safe_sniff", return_value={}) as sniff:
            sniff_hosts(hosts, max_hosts=2)
        assert sniff.call_count == 2

    def test_a_failing_host_does_not_break_the_batch(self):
        with patch("app.services.ats_sniffer.sniff_host",
                   side_effect=RuntimeError("boom")):
            merged, cache, _ = sniff_hosts({"careers.acme.com": ""})
        assert merged == {}
        assert cache["careers.acme.com"]["found"] == {}
