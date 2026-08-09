from unittest.mock import MagicMock, patch

from app.services.team_pages import _name_from_slug, people_from_html, team_page_contacts

TEAM_HTML = """
<html><body>
  <div class="member">
    <h3>Jane Doe</h3><p>VP Engineering</p>
    <a href="https://www.linkedin.com/in/jane-doe-8b12a4">LinkedIn</a>
    <a href="mailto:jane@acme.com">Email</a>
  </div>
  <div class="member">
    <h3>Sam Recruiter</h3>
    <a href="https://linkedin.com/in/sam-recruiter/">Profile</a>
  </div>
  <a href="https://www.linkedin.com/company/acme">Follow us</a>
  <a href="mailto:someone@gmail.com">Personal</a>
  <a href="mailto:press@othercorp.com">Press</a>
</body></html>
"""

JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Person", "name": "Dana Lead", "jobTitle": "Head of Engineering",
 "email": "dana@acme.com"}
</script>
</head><body></body></html>
"""


def _response(text, content_type="text/html", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


class TestNameFromSlug:
    def test_reads_a_name(self):
        assert _name_from_slug("jane-doe") == "Jane Doe"

    def test_drops_the_disambiguating_hash(self):
        assert _name_from_slug("jane-doe-8b12a4") == "Jane Doe"

    def test_empty_for_a_single_token(self):
        assert _name_from_slug("janedoe") == ""

    def test_empty_for_junk(self):
        assert _name_from_slug("") == ""


class TestPeopleFromHtml:
    def test_finds_linkedin_profiles(self):
        found = people_from_html(TEAM_HTML, "acme.com")
        urls = {c.get("linkedin_url") for c in found}
        assert "https://www.linkedin.com/in/jane-doe-8b12a4" in urls
        assert "https://www.linkedin.com/in/sam-recruiter" in urls

    def test_names_people_from_their_profile_slug(self):
        names = {c.get("name") for c in people_from_html(TEAM_HTML, "acme.com")}
        assert "Sam Recruiter" in names

    def test_ignores_the_company_page_link(self):
        found = people_from_html(TEAM_HTML, "acme.com")
        assert not any("/company/" in (c.get("linkedin_url") or "") for c in found)

    def test_finds_a_published_address(self):
        emails = {c.get("email") for c in people_from_html(TEAM_HTML, "acme.com")}
        assert "jane@acme.com" in emails

    def test_skips_free_mail(self):
        emails = {c.get("email") for c in people_from_html(TEAM_HTML, "acme.com")}
        assert "someone@gmail.com" not in emails

    def test_skips_a_different_company(self):
        emails = {c.get("email") for c in people_from_html(TEAM_HTML, "acme.com")}
        assert "press@othercorp.com" not in emails

    def test_reads_schema_org_person_markup(self):
        found = people_from_html(JSONLD_HTML, "acme.com")
        assert found[0]["name"] == "Dana Lead"
        assert found[0]["title"] == "Head of Engineering"
        assert found[0]["role"] == "hiring_manager"

    def test_merges_a_profile_and_an_address_for_one_person(self):
        html = """
        <a href="https://linkedin.com/in/jane-doe">x</a>
        <script type="application/ld+json">
        {"@type":"Person","name":"Jane Doe","jobTitle":"CTO","email":"jane@acme.com"}
        </script>
        """
        found = people_from_html(html, "acme.com")
        named = [c for c in found if c.get("name") == "Jane Doe"]
        assert len(named) == 1
        assert named[0]["linkedin_url"] and named[0]["email"]

    def test_survives_malformed_json_ld(self):
        html = '<script type="application/ld+json">{not json</script>'
        assert people_from_html(html, "acme.com") == []

    def test_empty_page(self):
        assert people_from_html("", "acme.com") == []


class TestTeamPageContacts:
    def test_fetches_and_parses(self):
        with patch("app.services.team_pages.httpx.get", return_value=_response(TEAM_HTML)):
            found = team_page_contacts("acme.com")
        assert any(c.get("linkedin_url") for c in found)

    def test_needs_a_domain(self):
        assert team_page_contacts("") == []

    def test_ignores_a_non_html_response(self):
        with patch("app.services.team_pages.httpx.get",
                   return_value=_response("{}", content_type="application/json")):
            assert team_page_contacts("acme.com") == []

    def test_ignores_a_404(self):
        with patch("app.services.team_pages.httpx.get", return_value=_response("", status=404)):
            assert team_page_contacts("acme.com") == []

    def test_a_network_failure_is_not_an_exception(self):
        with patch("app.services.team_pages.httpx.get", side_effect=Exception("dns")):
            assert team_page_contacts("acme.com") == []

    def test_respects_the_limit(self):
        with patch("app.services.team_pages.httpx.get", return_value=_response(TEAM_HTML)):
            assert len(team_page_contacts("acme.com", limit=1)) == 1
