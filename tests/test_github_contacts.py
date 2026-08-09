from unittest.mock import MagicMock, patch

from app.services.github_contacts import (
    _contact_from_user, _org_matches_company, find_org, github_contacts,
)


def _response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    resp.json.return_value = payload
    return resp


class TestOrgMatching:
    def test_accepts_an_org_whose_blog_is_the_company_domain(self):
        org = {"login": "stripe", "blog": "https://stripe.com", "name": "Stripe"}
        assert _org_matches_company(org, "Stripe", "stripe.com") is True

    def test_accepts_a_matching_display_name_when_there_is_no_domain(self):
        org = {"login": "acmehq", "name": "Acme, Inc."}
        assert _org_matches_company(org, "Acme Inc", "") is True

    def test_rejects_an_unrelated_org_with_a_similar_login(self):
        # Acting on the wrong org means messaging total strangers, so a bare
        # name collision is not enough.
        org = {"login": "stripe", "blog": "https://someones-side-project.dev", "name": "stripe cli"}
        assert _org_matches_company(org, "Stripe", "stripe.com") is False

    def test_accepts_a_bare_slug_match_when_nothing_contradicts_it(self):
        # Plenty of real company orgs publish no blog or email at all.
        org = {"login": "stripe", "name": ""}
        assert _org_matches_company(org, "Stripe", "stripe.com") is True

    def test_rejects_generic_orgs(self):
        org = {"login": "community", "blog": "https://acme.com"}
        assert _org_matches_company(org, "Acme", "acme.com") is False

    def test_rejects_nothing(self):
        assert _org_matches_company({}, "Acme", "acme.com") is False


class TestFindOrg:
    def test_finds_the_org_by_domain_slug(self):
        org = {"login": "stripe", "blog": "https://stripe.com"}
        with patch("app.services.github_contacts.httpx.get", return_value=_response(org)):
            assert find_org("Stripe", "stripe.com", "tok") == "stripe"

    def test_falls_back_to_search(self):
        def fake_get(url, **kwargs):
            if "/search/users" in url:
                return _response({"items": [{"login": "acme-eng"}]})
            if url.endswith("/orgs/acme-eng"):
                return _response({"login": "acme-eng", "blog": "https://acme.com"})
            return _response(None, status=404)

        with patch("app.services.github_contacts.httpx.get", side_effect=fake_get):
            assert find_org("Acme", "acme.com", "tok") == "acme-eng"

    def test_returns_none_when_nothing_verifies(self):
        with patch("app.services.github_contacts.httpx.get", return_value=_response(None, status=404)):
            assert find_org("Acme", "acme.com", "tok") is None

    def test_survives_a_network_failure(self):
        with patch("app.services.github_contacts.httpx.get", side_effect=Exception("boom")):
            assert find_org("Acme", "acme.com", "tok") is None

    def test_a_rate_limit_is_not_an_exception(self):
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "API rate limit exceeded"
        with patch("app.services.github_contacts.httpx.get", return_value=resp):
            assert find_org("Acme", "acme.com", "tok") is None


class TestContactFromUser:
    def test_builds_a_contact(self):
        user = {"name": "Jane Doe", "email": "jane@acme.com", "bio": "Staff Engineer",
                "html_url": "https://github.com/janedoe", "twitter_username": "janedoe"}
        contact = _contact_from_user(user, "Acme", "acme.com")
        assert contact["name"] == "Jane Doe"
        assert contact["email"] == "jane@acme.com"
        assert contact["profile_url"] == "https://github.com/janedoe"
        assert contact["twitter"] == "janedoe"
        assert contact["source"] == "github"

    def test_skips_a_user_with_no_real_name(self):
        assert _contact_from_user({"name": "octocat", "html_url": "x"}, "Acme", "acme.com") is None

    def test_skips_a_user_with_no_name_at_all(self):
        assert _contact_from_user({"html_url": "x"}, "Acme", "acme.com") is None

    def test_discards_githubs_masked_address(self):
        # noreply addresses bounce by design — storing one guarantees a failure.
        user = {"name": "Jane Doe", "email": "1234+jane@users.noreply.github.com"}
        assert _contact_from_user(user, "Acme", "acme.com")["email"] is None

    def test_defaults_to_engineer(self):
        user = {"name": "Jane Doe"}
        assert _contact_from_user(user, "Acme", "acme.com")["role"] == "engineer"

    def test_reads_a_role_out_of_the_bio(self):
        user = {"name": "Jane Doe", "bio": "Technical Recruiter at Acme"}
        assert _contact_from_user(user, "Acme", "acme.com")["role"] == "recruiter"


class TestGithubContacts:
    def test_off_without_a_token(self):
        assert github_contacts("Acme", "acme.com", "") == []

    def test_returns_named_members(self):
        def fake_get(url, **kwargs):
            if url.endswith("/orgs/acme"):
                return _response({"login": "acme", "blog": "https://acme.com"})
            if url.endswith("/orgs/acme/public_members"):
                return _response([{"login": "janedoe"}, {"login": "ghost"}])
            if url.endswith("/users/janedoe"):
                return _response({"name": "Jane Doe", "html_url": "https://github.com/janedoe"})
            if url.endswith("/users/ghost"):
                return _response({"name": None, "html_url": "https://github.com/ghost"})
            return _response(None, status=404)

        with patch("app.services.github_contacts.httpx.get", side_effect=fake_get):
            contacts = github_contacts("Acme", "acme.com", "tok")
        assert [c["name"] for c in contacts] == ["Jane Doe"]

    def test_empty_when_the_org_hides_its_members(self):
        def fake_get(url, **kwargs):
            if url.endswith("/orgs/acme"):
                return _response({"login": "acme", "blog": "https://acme.com"})
            if url.endswith("/orgs/acme/public_members"):
                return _response([])
            return _response(None, status=404)

        with patch("app.services.github_contacts.httpx.get", side_effect=fake_get):
            assert github_contacts("Acme", "acme.com", "tok") == []

    def test_respects_the_limit(self):
        def fake_get(url, **kwargs):
            if url.endswith("/orgs/acme"):
                return _response({"login": "acme", "blog": "https://acme.com"})
            if url.endswith("/orgs/acme/public_members"):
                return _response([{"login": f"u{i}"} for i in range(10)])
            if "/users/" in url:
                return _response({"name": "Jane Doe", "html_url": url})
            return _response(None, status=404)

        with patch("app.services.github_contacts.httpx.get", side_effect=fake_get):
            assert len(github_contacts("Acme", "acme.com", "tok", limit=2)) == 2

    def test_no_company_means_nothing(self):
        assert github_contacts("", "acme.com", "tok") == []
