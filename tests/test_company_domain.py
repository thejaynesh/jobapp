from unittest.mock import patch

from app.services.company_domain import (
    company_key, domain_candidates_from_name, domains_in_text, extract_domain,
    is_company_domain, registrable_domain, resolve_company_domain,
)


class TestExtractDomain:
    def test_extracts_from_url(self):
        assert extract_domain("https://www.acme.com/jobs/123") == "acme.com"

    def test_extracts_without_www(self):
        assert extract_domain("https://lever.co/acme") == "lever.co"

    def test_returns_empty_string_for_invalid(self):
        assert extract_domain("not-a-url") == ""

    def test_strips_port_and_credentials(self):
        assert extract_domain("https://user:pw@acme.com:8443/x") == "acme.com"

    def test_lowercases(self):
        assert extract_domain("https://ACME.com/Jobs") == "acme.com"


class TestRegistrableDomain:
    def test_strips_subdomain(self):
        assert registrable_domain("careers.acme.com") == "acme.com"

    def test_keeps_two_label_domain(self):
        assert registrable_domain("acme.com") == "acme.com"

    def test_handles_multipart_suffix(self):
        assert registrable_domain("jobs.acme.co.uk") == "acme.co.uk"

    def test_handles_deep_subdomain(self):
        assert registrable_domain("a.b.c.acme.io") == "acme.io"


class TestIsCompanyDomain:
    def test_rejects_ats_host(self):
        assert is_company_domain("boards.greenhouse.io") is False

    def test_rejects_ats_subdomain(self):
        assert is_company_domain("acme.applytojob.com") is False

    def test_rejects_aggregator(self):
        assert is_company_domain("linkedin.com") is False

    def test_rejects_free_mail(self):
        assert is_company_domain("gmail.com") is False

    def test_accepts_real_company(self):
        assert is_company_domain("acme.com") is True

    def test_rejects_junk(self):
        assert is_company_domain("") is False
        assert is_company_domain("localhost") is False


class TestCompanyKey:
    def test_ignores_legal_suffix(self):
        assert company_key("Acme, Inc.") == company_key("Acme Inc")

    def test_ignores_punctuation_and_case(self):
        assert company_key("ACME  Corp.") == "acme"

    def test_keeps_something_when_all_words_are_noise(self):
        # "The Group" is entirely noise words; dropping everything would make
        # every such company collide under the empty string.
        assert company_key("The Group") != ""

    def test_distinguishes_different_companies(self):
        assert company_key("Acme") != company_key("Globex")


class TestDomainCandidatesFromName:
    def test_puts_dot_com_first(self):
        assert domain_candidates_from_name("Acme Inc")[0] == "acme.com"

    def test_returns_nothing_for_empty_name(self):
        assert domain_candidates_from_name("") == []


class TestDomainsInText:
    def test_finds_link(self):
        assert "acme.com" in domains_in_text("See https://www.acme.com/about for more")

    def test_finds_email_domain(self):
        assert "acme.io" in domains_in_text("write to jane@acme.io")

    def test_skips_aggregators_and_free_mail(self):
        found = domains_in_text("https://linkedin.com/x and someone@gmail.com")
        assert found == []


class TestResolveCompanyDomain:
    def test_prefers_apply_url(self):
        domain, source = resolve_company_domain(
            "Acme", url="https://boards.greenhouse.io/acme",
            apply_url="https://careers.acme.com/jobs/1", verify=False,
        )
        assert (domain, source) == ("acme.com", "apply_url")

    def test_falls_back_to_job_url(self):
        domain, source = resolve_company_domain(
            "Acme", url="https://www.acme.com/careers/1", verify=False
        )
        assert (domain, source) == ("acme.com", "url")

    def test_skips_ats_url(self):
        domain, source = resolve_company_domain(
            "Globex", url="https://jobs.lever.co/globex", verify=False
        )
        assert source != "url"

    def test_uses_description_domain_matching_the_company(self):
        domain, source = resolve_company_domain(
            "Globex",
            url="https://boards.greenhouse.io/globex",
            description="Apply at https://globex.io/careers or email jobs@globex.io",
            verify=False,
        )
        assert (domain, source) == ("globex.io", "description")

    def test_guesses_from_name_when_nothing_else(self):
        domain, source = resolve_company_domain(
            "Initech", url="https://jobs.lever.co/initech", verify=False
        )
        assert (domain, source) == ("initech.com", "name")

    def test_verification_skips_a_dead_guess(self):
        with patch("app.services.company_domain.domain_responds", return_value=False):
            domain, source = resolve_company_domain(
                "Initech", url="https://jobs.lever.co/initech", verify=True
            )
        assert domain == ""

    def test_returns_empty_for_an_unusable_company(self):
        domain, source = resolve_company_domain("", url="", verify=False)
        assert (domain, source) == ("", "")
