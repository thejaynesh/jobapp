from types import SimpleNamespace

from app.services.linkedin_links import (
    company_links, company_people_url, company_slug, contact_link, is_profile_url,
    people_search_url, profile_url_for,
)

PROFILE = {
    "personal": {"name": "Jane Doe"},
    "education": [{"school": "Northeastern University"}, {"school": "IIT Bombay"}],
}


class TestCompanySlug:
    def test_prefers_a_slug_quoted_in_the_posting(self):
        # The trading name and the LinkedIn slug often differ, so a slug written
        # down in the posting beats anything derived from the name.
        slug = company_slug("Acme Widgets Inc", description="Follow https://linkedin.com/company/acme-hq/")
        assert slug == "acme-hq"

    def test_reads_the_slug_out_of_a_url(self):
        assert company_slug("Acme", url="https://www.linkedin.com/company/acme-corp/jobs") == "acme-corp"

    def test_falls_back_to_the_company_name(self):
        assert company_slug("Acme Widgets") == "acme-widgets"

    def test_drops_legal_suffixes(self):
        assert company_slug("Stripe, Inc.") == "stripe"

    def test_keeps_something_when_the_name_is_all_noise(self):
        assert company_slug("The Group") != ""

    def test_empty_company(self):
        assert company_slug("") == ""


class TestUrls:
    def test_people_search_encodes_terms(self):
        url = people_search_url("Acme Corp", "recruiter")
        assert url.startswith("https://www.linkedin.com/search/results/people/?keywords=")
        assert "Acme+Corp+recruiter" in url

    def test_people_search_is_empty_without_terms(self):
        assert people_search_url("", "") == ""

    def test_company_people_tab(self):
        assert company_people_url("stripe") == "https://www.linkedin.com/company/stripe/people/"

    def test_company_people_tab_filtered(self):
        url = company_people_url("stripe", "recruiter")
        assert url == "https://www.linkedin.com/company/stripe/people/?keywords=recruiter"

    def test_company_people_needs_a_slug(self):
        assert company_people_url("") == ""

    def test_profile_search_scopes_to_the_company(self):
        url = profile_url_for("Sam Recruiter", "Acme")
        assert "Sam+Recruiter+Acme" in url

    def test_profile_search_needs_a_name(self):
        assert profile_url_for("", "Acme") == ""


class TestCompanyLinks:
    def test_alumni_angle_comes_first(self):
        links = company_links("Stripe", PROFILE)
        assert links[0]["label"] == "Northeastern University alumni at Stripe"
        assert links[0]["hint"]

    def test_only_the_alumni_angle_is_flagged_primary(self):
        # The flag drives the UI's highlight, so flagging the generic "everyone"
        # link would tell the user the wrong thing to click.
        links = company_links("Stripe", PROFILE)
        primary = [l["label"] for l in links if l["primary"]]
        assert primary == ["Northeastern University alumni at Stripe", "IIT Bombay alumni at Stripe"]

    def test_one_link_per_school(self):
        labels = [l["label"] for l in company_links("Stripe", PROFILE)]
        assert "IIT Bombay alumni at Stripe" in labels

    def test_no_alumni_links_without_an_education_history(self):
        links = company_links("Stripe", {"personal": {"name": "Jane"}})
        assert not any("alumni" in l["label"] for l in links)
        assert links[0]["label"] == "Recruiters at Stripe"

    def test_covers_the_three_angles_and_everyone(self):
        labels = [l["label"] for l in company_links("Stripe", {})]
        assert labels == [
            "Recruiters at Stripe",
            "Engineering managers at Stripe",
            "Engineers at Stripe",
            "Everyone at Stripe",
        ]

    def test_every_link_has_a_url(self):
        assert all(l["url"].startswith("https://") for l in company_links("Stripe", PROFILE))

    def test_uses_the_company_people_tab_when_a_slug_is_known(self):
        links = company_links("Stripe", {})
        assert "/company/stripe/people/" in links[0]["url"]

    def test_survives_a_company_with_no_usable_name(self):
        # No slug means no /company/ URL; the global search still works, and
        # "Everyone at" is dropped rather than emitted as a broken link.
        links = company_links("", {})
        assert all(l["url"] for l in links)


class TestContactLink:
    def test_prefers_a_known_profile(self):
        contact = SimpleNamespace(linkedin_url="https://linkedin.com/in/sam", name="Sam", company="Acme")
        assert contact_link(contact) == "https://linkedin.com/in/sam"

    def test_searches_by_name_when_there_is_no_profile(self):
        contact = SimpleNamespace(linkedin_url=None, name="Sam Recruiter", company="Acme")
        assert "Sam+Recruiter+Acme" in contact_link(contact)

    def test_nothing_for_an_anonymous_contact(self):
        contact = SimpleNamespace(linkedin_url=None, name=None, company="Acme")
        assert contact_link(contact) == ""


class TestIsProfileUrl:
    def test_recognises_a_profile(self):
        assert is_profile_url("https://www.linkedin.com/in/janedoe") is True

    def test_rejects_a_search(self):
        assert is_profile_url("https://www.linkedin.com/search/results/people/?keywords=x") is False

    def test_rejects_junk(self):
        assert is_profile_url("") is False
