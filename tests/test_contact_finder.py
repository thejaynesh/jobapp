from unittest.mock import MagicMock, patch

from app.services.contact_finder import (
    apply_pattern, classify_role, contact_score, contacts_from_description,
    find_email, find_linkedin_contact, find_linkedin_contacts, guess_emails,
    hunter_contacts, hunter_email_finder, name_from_local_part, split_name,
    verify_email,
)


def _response(payload: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# Names and roles
# ---------------------------------------------------------------------------

class TestSplitName:
    def test_splits_first_and_last(self):
        assert split_name("Jane Doe") == ("Jane", "Doe")

    def test_drops_middle_initial(self):
        assert split_name("Jane Q. Doe") == ("Jane", "Doe")

    def test_single_word_has_no_surname(self):
        assert split_name("Jane") == ("Jane", "")

    def test_empty(self):
        assert split_name("") == ("", "")


class TestNameFromLocalPart:
    def test_derives_a_name(self):
        assert name_from_local_part("jane.doe") == "Jane Doe"

    def test_ignores_a_role_mailbox(self):
        assert name_from_local_part("careers") == ""

    def test_ignores_a_generic_mailbox(self):
        assert name_from_local_part("info") == ""

    def test_ignores_an_ambiguous_single_token(self):
        assert name_from_local_part("jdoe") == ""


class TestClassifyRole:
    def test_recruiter_from_title(self):
        assert classify_role("Technical Recruiter") == "recruiter"

    def test_recruiting_beats_engineering_in_a_hybrid_title(self):
        assert classify_role("Engineering Recruiter") == "recruiter"

    def test_hiring_manager(self):
        assert classify_role("Engineering Manager") == "hiring_manager"

    def test_executive(self):
        assert classify_role("Co-Founder & CTO") == "executive"

    def test_engineer(self):
        assert classify_role("Senior Backend Engineer") == "engineer"

    def test_recruiter_from_mailbox(self):
        assert classify_role(email="careers@acme.com") == "recruiter"

    def test_generic_from_mailbox(self):
        assert classify_role(email="info@acme.com") == "generic"

    def test_unknown_when_nothing_is_known(self):
        assert classify_role() == "unknown"


class TestContactScore:
    def test_recruiter_outranks_engineer(self):
        recruiter = {"role": "recruiter", "email": "a@x.com", "email_status": "verified"}
        engineer = {"role": "engineer", "email": "b@x.com", "email_status": "verified"}
        assert contact_score(recruiter) > contact_score(engineer)

    def test_verified_outranks_guessed(self):
        verified = {"role": "recruiter", "email": "a@x.com", "email_status": "verified"}
        guessed = {"role": "recruiter", "email": "a@x.com", "email_status": "guessed"}
        assert contact_score(verified) > contact_score(guessed)

    def test_unreachable_contact_scores_worst(self):
        unreachable = {"role": "recruiter", "name": "Jane"}
        reachable = {"role": "unknown", "email": "x@y.com", "email_status": "unverified"}
        assert contact_score(unreachable) < contact_score(reachable)


# ---------------------------------------------------------------------------
# Email patterns
# ---------------------------------------------------------------------------

class TestApplyPattern:
    def test_first_dot_last(self):
        assert apply_pattern("{first}.{last}", "Jane", "Doe", "acme.com") == "jane.doe@acme.com"

    def test_initial_plus_last(self):
        assert apply_pattern("{f}{last}", "Jane", "Doe", "acme.com") == "jdoe@acme.com"

    def test_first_only(self):
        assert apply_pattern("{first}", "Jane", "", "acme.com") == "jane@acme.com"

    def test_strips_accents_and_punctuation(self):
        assert apply_pattern("{first}.{last}", "Renée", "O'Brien", "acme.com") == "rene.obrien@acme.com"

    def test_refuses_a_pattern_needing_a_missing_surname(self):
        assert apply_pattern("{first}.{last}", "Jane", "", "acme.com") == ""

    def test_refuses_an_unknown_placeholder(self):
        assert apply_pattern("{nickname}", "Jane", "Doe", "acme.com") == ""

    def test_needs_a_domain(self):
        assert apply_pattern("{first}", "Jane", "Doe", "") == ""


class TestGuessEmails:
    def test_puts_the_known_pattern_first(self):
        guesses = guess_emails("Jane", "Doe", "acme.com", pattern="{f}{last}")
        assert guesses[0] == "jdoe@acme.com"

    def test_falls_back_to_common_shapes(self):
        assert guess_emails("Jane", "Doe", "acme.com")[0] == "jane.doe@acme.com"

    def test_no_duplicates(self):
        guesses = guess_emails("Jane", "Doe", "acme.com", pattern="{first}.{last}")
        assert len(guesses) == len(set(guesses))

    def test_nothing_without_a_name(self):
        assert guess_emails("", "", "acme.com") == []


# ---------------------------------------------------------------------------
# Hunter
# ---------------------------------------------------------------------------

class TestFindEmail:
    def test_reads_the_emails_array(self):
        payload = {"data": {"pattern": "{first}.{last}", "emails": [
            {"value": "recruiter@acme.com", "position": "Technical Recruiter", "confidence": 92},
        ]}}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            assert find_email("Acme", "acme.com", "key") == "recruiter@acme.com"

    def test_reads_a_top_level_email(self):
        payload = {"data": {"email": "hr@acme.com"}}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            assert find_email("Acme", "acme.com", "key") == "hr@acme.com"

    def test_returns_none_when_nothing_found(self):
        with patch("app.services.contact_finder.httpx.get", return_value=_response({"data": {}})):
            assert find_email("Acme", "acme.com", "key") is None

    def test_returns_none_on_api_error(self):
        with patch("app.services.contact_finder.httpx.get", side_effect=Exception("timeout")):
            assert find_email("Acme", "acme.com", "key") is None

    def test_returns_none_on_a_quota_error_payload(self):
        payload = {"errors": [{"details": "You have reached your monthly limit"}]}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            assert find_email("Acme", "acme.com", "key") is None

    def test_returns_none_without_a_key(self):
        assert find_email("Acme", "acme.com", "") is None


class TestHunterContacts:
    def _payload(self):
        return {"data": {"pattern": "{first}.{last}", "emails": [
            {"value": "sales@acme.com", "position": "Account Executive",
             "department": "sales", "confidence": 90},
            {"value": "jane.doe@acme.com", "first_name": "Jane", "last_name": "Doe",
             "position": "Technical Recruiter", "department": "hr", "confidence": 95,
             "linkedin": "https://linkedin.com/in/janedoe",
             "verification": {"status": "deliverable"}},
        ]}}

    def test_ranks_the_recruiter_first(self):
        with patch("app.services.contact_finder.httpx.get", return_value=_response(self._payload())):
            contacts = hunter_contacts("acme.com", "key")
        assert contacts[0]["email"] == "jane.doe@acme.com"
        assert contacts[0]["role"] == "recruiter"

    def test_carries_verification_through(self):
        with patch("app.services.contact_finder.httpx.get", return_value=_response(self._payload())):
            contacts = hunter_contacts("acme.com", "key")
        assert contacts[0]["email_status"] == "verified"

    def test_names_a_person_from_the_mailbox_when_hunter_does_not(self):
        payload = {"data": {"emails": [{"value": "john.smith@acme.com", "confidence": 70}]}}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            contacts = hunter_contacts("acme.com", "key")
        assert contacts[0]["name"] == "John Smith"

    def test_reuses_a_response_instead_of_paying_twice(self):
        with patch("app.services.contact_finder.httpx.get") as mock_get:
            hunter_contacts("acme.com", "key", data=self._payload()["data"])
        mock_get.assert_not_called()

    def test_empty_without_a_key(self):
        assert hunter_contacts("acme.com", "") == []


class TestHunterEmailFinder:
    def test_returns_the_address_and_score(self):
        payload = {"data": {"email": "jane.doe@acme.com", "score": 88}}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            assert hunter_email_finder("acme.com", "Jane", "Doe", "key") == {
                "email": "jane.doe@acme.com", "score": 88,
            }

    def test_empty_when_hunter_has_nobody(self):
        with patch("app.services.contact_finder.httpx.get", return_value=_response({"data": {}})):
            assert hunter_email_finder("acme.com", "Jane", "Doe", "key") == {}

    def test_empty_without_a_full_name(self):
        assert hunter_email_finder("acme.com", "Jane", "", "key") == {}


class TestVerifyEmail:
    def test_maps_deliverable_to_verified(self):
        payload = {"data": {"result": "deliverable", "score": 97}}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            assert verify_email("jane@acme.com", "key") == {"status": "verified", "confidence": 97}

    def test_maps_undeliverable_to_invalid(self):
        payload = {"data": {"result": "undeliverable", "score": 0}}
        with patch("app.services.contact_finder.httpx.get", return_value=_response(payload)):
            assert verify_email("jane@acme.com", "key")["status"] == "invalid"

    def test_empty_when_the_verifier_is_unavailable(self):
        with patch("app.services.contact_finder.httpx.get", side_effect=Exception("boom")):
            assert verify_email("jane@acme.com", "key") == {}


# ---------------------------------------------------------------------------
# The posting
# ---------------------------------------------------------------------------

class TestContactsFromDescription:
    def test_finds_an_address_at_the_company_domain(self):
        found = contacts_from_description("Questions? Email jane.doe@acme.com", "acme.com")
        assert found[0]["email"] == "jane.doe@acme.com"
        assert found[0]["name"] == "Jane Doe"

    def test_skips_free_mail(self):
        assert contacts_from_description("mail me at someone@gmail.com", "acme.com") == []

    def test_skips_a_different_company_when_a_domain_is_known(self):
        found = contacts_from_description("posted via jobs@boards.greenhouse.io", "acme.com")
        assert found == []

    def test_ranks_a_recruiting_mailbox_above_a_generic_one(self):
        found = contacts_from_description(
            "support@acme.com or careers@acme.com", "acme.com"
        )
        assert found[0]["email"] == "careers@acme.com"

    def test_deduplicates(self):
        found = contacts_from_description("jane@acme.com and jane@acme.com", "acme.com")
        assert len(found) == 1

    def test_empty_description(self):
        assert contacts_from_description("", "acme.com") == []


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------

class TestLinkedIn:
    def test_no_cookie_means_no_contacts(self):
        assert find_linkedin_contacts("Acme", ["recruiter"], "") == []

    def test_no_company_means_no_contacts(self):
        assert find_linkedin_contacts("", ["recruiter"], "cookie") == []

    def test_a_scrape_failure_is_not_an_exception(self):
        async def blocked(*args, **kwargs):
            raise RuntimeError("challenge page")

        with patch("app.services.contact_finder._scrape_people", blocked):
            assert find_linkedin_contacts("Acme", ["recruiter"], "cookie") == []

    def test_single_contact_helper_returns_a_dict(self):
        assert find_linkedin_contact("Acme", "Engineering", "") == {}

    def test_single_contact_helper_takes_the_first_result(self):
        async def people(*args, **kwargs):
            return [{"name": "Jane Doe", "role": "recruiter"}, {"name": "John", "role": "engineer"}]

        with patch("app.services.contact_finder._scrape_people", people):
            assert find_linkedin_contact("Acme", "Engineering", "cookie")["name"] == "Jane Doe"
