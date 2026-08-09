import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.application import Application
from app.models.job import Job, JobStatus
from app.models.outreach import Contact, OutreachMessage
from app.models.profile import Profile
from app.services import outreach as outreach_service
from app.services.outreach import (
    BANNED_PHRASES, channel_spec, compose_message, discover_contacts,
    draft_due_follow_ups, draft_message, due_follow_ups, enforce_limit,
    fallback_message, followup_days, mark_replied, mark_sent, next_step,
    outreach_stats, regenerate_message, run_outreach, scrub, set_message_status,
    upsert_contact,
)

PROFILE = {
    "personal": {"name": "Jane Doe", "email": "jane@example.com",
                 "linkedin": "linkedin.com/in/janedoe"},
    "narrative": {"summary": "Backend engineer who likes data-heavy systems."},
    "skills": {"languages": ["Python", "Go"], "clouds": ["AWS"]},
    "experience": [
        {"company": "Initech", "role": "Engineer",
         "bullets": ["Cut p99 latency by 40% on the billing API"]},
    ],
    "projects": [{"name": "Ledger", "description": "A double-entry ledger",
                  "bullets": ["Handled 2M entries/day"]}],
}


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

def _make_job(db, **overrides) -> Job:
    job = Job(
        source="greenhouse",
        title=overrides.pop("title", "Backend Engineer"),
        company=overrides.pop("company", "Acme Corp"),
        url=overrides.pop("url", "https://boards.greenhouse.io/acme/jobs/1"),
        description=overrides.pop("description", "We need Python and AWS."),
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
        status=JobStatus.matched,
        **overrides,
    )
    db.add(job)
    db.flush()
    return job


def _make_application(db, **job_kwargs) -> Application:
    app = Application(job_id=_make_job(db, **job_kwargs).id)
    db.add(app)
    db.flush()
    return app


def _make_contact(db, app, **overrides) -> Contact:
    contact = Contact(
        application_id=app.id,
        company=app.job.company,
        company_key="acme",
        name=overrides.pop("name", "Sam Recruiter"),
        email=overrides.pop("email", "sam@acme.com"),
        email_status=overrides.pop("email_status", "verified"),
        role=overrides.pop("role", "recruiter"),
        source=overrides.pop("source", "hunter"),
        **overrides,
    )
    db.add(contact)
    db.flush()
    return contact


@pytest.fixture
def profile(db):
    db.add(Profile(data=PROFILE))
    db.flush()


@pytest.fixture(autouse=True)
def no_outbound_calls():
    """
    Keep discovery off the network by default.

    Team-page mining is on by default in config, so without this every test that
    reaches discover_contacts really does fetch six URLs from the resolved
    domain — slow, flaky, and rude to whoever owns acme.com. Tests that care
    about a source patch it themselves, and an inner patch wins over this one.
    """
    with patch("app.services.outreach.team_page_contacts", return_value=[]), \
         patch("app.services.outreach.github_contacts", return_value=[]):
        yield


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

class TestScrub:
    def test_removes_a_leaked_subject_line(self):
        assert "Subject:" not in scrub("Subject: Hello\n\nHi Sam, ...")

    def test_replaces_a_name_placeholder_with_the_candidate(self):
        assert scrub("Thanks,\n[Your Name]", "Jane Doe").endswith("Jane Doe")

    def test_removes_other_placeholders(self):
        assert "[" not in scrub("I admire [Company]'s work.", "Jane Doe")

    def test_strips_code_fences_and_wrapping_quotes(self):
        assert scrub('```\n"Hi Sam."\n```') == "Hi Sam."

    def test_collapses_runaway_blank_lines(self):
        assert "\n\n\n" not in scrub("a\n\n\n\n\nb")


class TestEnforceLimit:
    def test_leaves_a_short_message_alone(self):
        assert enforce_limit("Hi Sam.", "linkedin_note") == "Hi Sam."

    def test_trims_a_connection_note_to_the_limit(self):
        text = "Sentence one is here. " * 40
        assert len(enforce_limit(text, "linkedin_note")) <= channel_spec("linkedin_note")["max_chars"]

    def test_prefers_to_end_on_a_sentence(self):
        text = "A" * 200 + ". " + "B" * 200
        assert enforce_limit(text, "linkedin_note").endswith(".")

    def test_never_ends_mid_word_when_there_is_no_sentence_break(self):
        assert not enforce_limit("word " * 200, "twitter").endswith("wor")

    def test_email_allows_much_more_than_a_note(self):
        assert channel_spec("email")["max_chars"] > channel_spec("linkedin_note")["max_chars"]


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

class TestComposeMessage:
    CONTACT = {"name": "Sam Recruiter", "title": "Technical Recruiter", "role": "recruiter"}
    JOB = {"title": "Backend Engineer", "company": "Acme Corp",
           "description": "Python, AWS", "matched_skills": ["Python"]}

    def test_returns_subject_and_body_for_email(self):
        payload = (
            '{"subject": "Backend Engineer - Jane Doe", "body": "Hi Sam, I built a ledger '
            'that handled two million entries a day. Worth a chat?"}'
        )
        with patch("app.services.outreach.generation_chat", return_value=payload):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        assert result["subject"] == "Backend Engineer - Jane Doe"
        assert "ledger" in result["body"]

    def test_plain_text_reply_still_produces_a_body(self):
        text = "Hi Sam, I cut p99 latency by 40% on a billing API and would love to talk."
        with patch("app.services.outreach.generation_chat", return_value=text):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        assert result["body"].startswith("Hi Sam")

    def test_invents_a_subject_when_the_model_omits_one(self):
        with patch("app.services.outreach.generation_chat",
                   return_value='{"body": "Hi Sam, here is why I would be useful to you."}'):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        assert result["subject"]

    def test_no_subject_for_a_channel_that_has_none(self):
        with patch("app.services.outreach.generation_chat",
                   return_value="Hi Sam, I built a ledger that handled two million entries a day."):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="linkedin_note")
        assert result["subject"] is None

    def test_a_connection_note_is_cut_to_the_limit(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam. " * 100):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="linkedin_note")
        assert len(result["body"]) <= channel_spec("linkedin_note")["max_chars"]

    def test_falls_back_to_a_template_when_no_provider_answers(self):
        with patch("app.services.outreach.generation_chat", side_effect=Exception("all down")):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        assert "Acme Corp" in result["body"] or "Backend Engineer" in result["body"]
        assert result["generated_by"] is None

    def test_falls_back_when_the_model_returns_almost_nothing(self):
        with patch("app.services.outreach.generation_chat", return_value="ok"):
            result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        assert len(result["body"]) > 40

    def test_records_which_model_wrote_it(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            with patch("app.services.outreach.collect_llm_log", return_value=["anthropic/claude"]):
                result = compose_message(PROFILE, self.CONTACT, self.JOB, channel="linkedin")
        assert result["generated_by"] == "anthropic/claude"

    def test_prompt_bans_the_stock_phrases(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30) as chat:
            compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        system = chat.call_args.kwargs["messages"][0]["content"]
        assert BANNED_PHRASES[0] in system

    def test_prompt_grounds_on_real_accomplishments(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30) as chat:
            compose_message(PROFILE, self.CONTACT, self.JOB, channel="email")
        user = chat.call_args.kwargs["messages"][1]["content"]
        assert "Cut p99 latency by 40%" in user

    def test_an_unknown_name_forbids_dear_hiring_manager(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi, " + "x " * 30) as chat:
            compose_message(PROFILE, {"name": None}, self.JOB, channel="email")
        system = chat.call_args.kwargs["messages"][0]["content"]
        assert "Dear Hiring Manager" in system

    def test_feedback_reaches_the_prompt(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30) as chat:
            compose_message(PROFILE, self.CONTACT, self.JOB, feedback="Make it shorter")
        assert "Make it shorter" in chat.call_args.kwargs["messages"][1]["content"]

    def test_a_follow_up_is_told_not_to_repeat_the_thread(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30) as chat:
            compose_message(PROFILE, self.CONTACT, self.JOB, kind="follow_up",
                            thread="[initial, Jan 01] earlier text")
        user = chat.call_args.kwargs["messages"][1]["content"]
        assert "earlier text" in user


class TestFallbackMessage:
    def test_addresses_a_known_contact_by_first_name(self):
        body = fallback_message(PROFILE, {"name": "Sam Recruiter"},
                                {"title": "Backend Engineer", "company": "Acme"},
                                "email", "initial")["body"]
        assert body.startswith("Hi Sam,")

    def test_stays_neutral_without_a_name(self):
        body = fallback_message(PROFILE, {}, {"title": "Backend Engineer", "company": "Acme"},
                                "email", "initial")["body"]
        assert body.startswith("Hi,")

    def test_signs_an_email_with_the_candidate(self):
        body = fallback_message(PROFILE, {}, {"title": "X", "company": "Acme"},
                                "email", "initial")["body"]
        assert body.rstrip().endswith("Jane Doe")

    def test_a_note_fits_the_channel(self):
        result = fallback_message(PROFILE, {"name": "Sam"}, {"title": "Backend Engineer",
                                  "company": "Acme"}, "linkedin_note", "initial")
        assert len(result["body"]) <= channel_spec("linkedin_note")["max_chars"]
        assert result["subject"] is None

    def test_a_follow_up_reads_as_a_follow_up(self):
        body = fallback_message(PROFILE, {"name": "Sam"}, {"title": "X", "company": "Acme"},
                                "email", "follow_up")["body"]
        assert "wrote last week" in body

    def test_a_referral_request_asks_for_a_referral(self):
        body = fallback_message(PROFILE, {"name": "Sam"}, {"title": "X", "company": "Acme"},
                                "email", "referral_request")["body"]
        assert "referring me" in body


class TestLegacyDraftHelper:
    def test_returns_a_string(self):
        with patch("app.services.outreach.generation_chat", return_value="Hi John, " + "x " * 30):
            msg = outreach_service.draft_outreach_message(
                {"name": "Jane", "narrative": {"summary": "Engineer."},
                 "skills": {"languages": ["Python"]}},
                "John Smith", "Recruiter", "Backend Engineer", "Acme Corp",
                "key", "url", "model",
            )
        assert isinstance(msg, str) and msg

    def test_falls_back_rather_than_raising(self):
        with patch("app.services.outreach.generation_chat", side_effect=Exception("fail")):
            msg = outreach_service.draft_outreach_message(
                {"name": "Jane"}, "John", "Recruiter", "Backend Engineer", "Acme Corp",
                "key", "url", "model",
            )
        assert "Acme Corp" in msg or "Backend Engineer" in msg

    def test_reads_a_top_level_name_from_the_old_profile_shape(self):
        assert outreach_service.candidate_name({"name": "Old Shape"}) == "Old Shape"

    def test_prefers_the_current_profile_shape(self):
        assert outreach_service.candidate_name(
            {"name": "Old", "personal": {"name": "New"}}
        ) == "New"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestUpsertContact:
    def test_creates_a_contact(self, db):
        app = _make_application(db)
        contact = upsert_contact(db, app, {"name": "Sam", "email": "sam@acme.com",
                                           "source": "hunter", "role": "recruiter"})
        db.flush()
        assert contact.id and contact.company == "Acme Corp"

    def test_reuses_the_row_for_the_same_company_and_address(self, db):
        app = _make_application(db)
        first = upsert_contact(db, app, {"email": "sam@acme.com", "source": "hunter"})
        db.flush()
        second = upsert_contact(db, app, {"email": "sam@acme.com", "name": "Sam",
                                          "source": "linkedin"})
        db.flush()
        assert first.id == second.id
        assert second.name == "Sam"

    def test_a_thinner_source_never_blanks_a_known_field(self, db):
        app = _make_application(db)
        upsert_contact(db, app, {"email": "sam@acme.com", "title": "Recruiter"})
        db.flush()
        contact = upsert_contact(db, app, {"email": "sam@acme.com", "title": None})
        db.flush()
        assert contact.title == "Recruiter"

    def test_two_applications_at_one_company_get_their_own_contact(self, db):
        # The same recruiter approached about two roles is two conversations —
        # sharing one row would leave the second application's panel empty.
        first_app = _make_application(db, title="Backend Engineer")
        second_app = _make_application(db, title="Platform Engineer", company="Acme, Inc.")
        first = upsert_contact(db, first_app, {"email": "sam@acme.com", "name": "Sam"})
        db.flush()
        second = upsert_contact(db, second_app, {"email": "sam@acme.com", "name": "Sam"})
        db.flush()
        assert first.id != second.id
        assert second.application_id == second_app.id

    def test_a_contact_found_by_name_gains_an_address_later(self, db):
        app = _make_application(db)
        upsert_contact(db, app, {"name": "Sam Recruiter", "linkedin_url": "https://li/in/sam"})
        db.flush()
        contact = upsert_contact(db, app, {"name": "Sam Recruiter", "email": "sam@acme.com",
                                           "email_confidence": 90})
        db.flush()
        assert contact.email == "sam@acme.com"
        assert contact.linkedin_url == "https://li/in/sam"


class TestDiscoverContacts:
    def test_stores_an_address_out_of_the_posting(self, db):
        app = _make_application(db, description="Questions? Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "name")):
            contacts = discover_contacts(db, app, use_linkedin=False, verify=False)
        assert [c.email for c in contacts] == ["talent@acme.com"]
        assert contacts[0].source == "description"

    def test_merges_hunter_results(self, db):
        app = _make_application(db, description="no addresses here")
        hunter = [{"name": "Sam Recruiter", "email": "sam@acme.com", "role": "recruiter",
                   "email_status": "verified", "email_confidence": 95, "source": "hunter"}]
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.hunter_domain_search", return_value={"pattern": "{first}"}):
                with patch("app.services.outreach.hunter_contacts", return_value=hunter):
                    with patch.object(outreach_service.settings, "HUNTER_IO_API_KEY", "key"):
                        contacts = discover_contacts(db, app, use_linkedin=False, verify=False)
        assert contacts[0].email == "sam@acme.com"
        assert contacts[0].email_status == "verified"

    def test_guesses_an_address_for_a_linkedin_name(self, db):
        app = _make_application(db, description="")
        people = [{"name": "Sam Recruiter", "first_name": "Sam", "last_name": "Recruiter",
                   "role": "recruiter", "linkedin_url": "https://li/in/sam", "source": "linkedin"}]
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "name")):
            with patch("app.services.outreach.find_linkedin_contacts", return_value=people):
                with patch.object(outreach_service.settings, "LINKEDIN_SESSION_COOKIE", "cookie"):
                    contacts = discover_contacts(db, app, use_linkedin=True, verify=False)
        assert contacts[0].email == "sam.recruiter@acme.com"
        assert contacts[0].email_status == "guessed"

    def test_falls_back_to_the_careers_mailbox(self, db):
        app = _make_application(db, description="")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "name")):
            contacts = discover_contacts(db, app, use_linkedin=False, verify=False)
        assert [c.email for c in contacts] == ["careers@acme.com"]
        assert contacts[0].email_status == "guessed"

    def test_no_domain_means_no_invented_contacts(self, db):
        app = _make_application(db, description="")
        with patch("app.services.outreach.resolve_company_domain", return_value=("", "")):
            assert discover_contacts(db, app, use_linkedin=False, verify=False) == []

    def test_respects_the_per_application_cap(self, db):
        app = _make_application(db, description="")
        hunter = [
            {"name": f"P{i}", "email": f"p{i}@acme.com", "role": "recruiter",
             "email_status": "verified", "email_confidence": 90 - i, "source": "hunter"}
            for i in range(6)
        ]
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.hunter_domain_search", return_value={}):
                with patch("app.services.outreach.hunter_contacts", return_value=hunter):
                    with patch.object(outreach_service.settings, "HUNTER_IO_API_KEY", "key"):
                        contacts = discover_contacts(db, app, use_linkedin=False,
                                                     verify=False, max_contacts=2)
        assert len(contacts) == 2

    def test_verification_updates_the_stored_status(self, db):
        app = _make_application(db, description="Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.verify_email",
                       return_value={"status": "invalid", "confidence": 0}):
                with patch.object(outreach_service.settings, "HUNTER_IO_API_KEY", "key"):
                    with patch("app.services.outreach.hunter_domain_search", return_value={}):
                        with patch("app.services.outreach.hunter_contacts", return_value=[]):
                            contacts = discover_contacts(db, app, use_linkedin=False, verify=True)
        assert contacts[0].email_status == "invalid"

    def test_pulls_profiles_off_the_company_team_page(self, db):
        app = _make_application(db, description="")
        team = [{"name": "Dana Lead", "linkedin_url": "https://linkedin.com/in/dana-lead",
                 "role": "hiring_manager", "source": "team_page"}]
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.team_page_contacts", return_value=team):
                contacts = discover_contacts(db, app, use_linkedin=False, verify=False)
        stored = {c.name: c for c in contacts}
        assert stored["Dana Lead"].linkedin_url == "https://linkedin.com/in/dana-lead"
        assert stored["Dana Lead"].source == "team_page"

    def test_keeps_a_github_contact_reachable_only_by_profile(self, db):
        # No email and no LinkedIn, but a GitHub page and an X handle is still a
        # way to reach someone — the old filter would have dropped them.
        app = _make_application(db, description="")
        people = [{"name": "Ada Engineer", "role": "engineer", "source": "github",
                   "profile_url": "https://github.com/ada", "twitter": "ada"}]
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.github_contacts", return_value=people):
                with patch.object(outreach_service.settings, "GITHUB_TOKEN", "tok"):
                    contacts = discover_contacts(db, app, use_linkedin=False, verify=False)
        found = {c.name: c for c in contacts}
        assert found["Ada Engineer"].profile_url == "https://github.com/ada"
        assert found["Ada Engineer"].twitter == "ada"

    def test_github_is_skipped_without_a_token(self, db):
        app = _make_application(db, description="")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.github_contacts") as gh:
                with patch.object(outreach_service.settings, "GITHUB_TOKEN", ""):
                    discover_contacts(db, app, use_linkedin=False, verify=False)
        gh.assert_not_called()

    def test_a_failing_source_does_not_sink_the_others(self, db):
        app = _make_application(db, description="Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.team_page_contacts", side_effect=Exception("timeout")):
                contacts = discover_contacts(db, app, use_linkedin=False, verify=False)
        assert [c.email for c in contacts] == ["talent@acme.com"]

    def test_the_linkedin_scrape_is_capped(self, db):
        app = _make_application(db, description="")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.find_linkedin_contacts", return_value=[]) as scrape:
                with patch.object(outreach_service.settings, "LINKEDIN_SESSION_COOKIE", "cookie"):
                    with patch.object(outreach_service.settings, "OUTREACH_TARGET_TITLES",
                                      "recruiter,manager,engineer,designer"):
                        with patch.object(outreach_service.settings,
                                          "OUTREACH_LINKEDIN_MAX_SEARCHES", 2):
                            discover_contacts(db, app, use_linkedin=True, verify=False)
        assert scrape.call_count == 2

    def test_records_when_the_search_ran(self, db):
        app = _make_application(db, description="Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            discover_contacts(db, app, use_linkedin=False, verify=False)
        assert app.outreach_checked_at is not None


# ---------------------------------------------------------------------------
# Drafting against the database
# ---------------------------------------------------------------------------

class TestDraftMessage:
    def test_stores_a_draft(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        with patch("app.services.outreach.generation_chat",
                   return_value='{"subject": "Hello", "body": "Hi Sam, a real message here."}'):
            message = draft_message(db, contact)
        assert message.status == "draft"
        assert message.channel == "email"
        assert message.sequence_step == 1

    def test_defaults_to_linkedin_without_an_address(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app, email=None, email_status="unknown",
                                linkedin_url="https://li/in/sam")
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            assert draft_message(db, contact).channel == "linkedin"

    def test_a_second_message_advances_the_sequence(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            draft_message(db, contact)
            db.refresh(contact)
            second = draft_message(db, contact, kind="follow_up")
        assert second.sequence_step == 2

    def test_an_unknown_channel_falls_back_rather_than_storing_junk(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            assert draft_message(db, contact, channel="carrier-pigeon").channel == "email"

    def test_the_thread_is_given_to_a_follow_up(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        db.add(OutreachMessage(contact_id=contact.id, application_id=app.id,
                               body="The first note.", status="sent", sequence_step=1))
        db.flush()
        db.refresh(contact)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30) as chat:
            draft_message(db, contact, kind="follow_up")
        assert "The first note." in chat.call_args.kwargs["messages"][1]["content"]


class TestRegenerateMessage:
    def test_replaces_the_body_in_place(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            message = draft_message(db, contact)
        original_id, original_step = message.id, message.sequence_step
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "y " * 30):
            regenerate_message(db, message, feedback="warmer")
        assert message.id == original_id
        assert message.sequence_step == original_step
        assert "y" in message.body

    def test_clears_the_edited_flag(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            message = draft_message(db, contact)
        message.edited = True
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "y " * 30):
            regenerate_message(db, message)
        assert message.edited is False

    def test_refuses_to_rewrite_a_sent_message(self, db, profile):
        app = _make_application(db)
        contact = _make_contact(db, app)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            message = draft_message(db, contact)
        mark_sent(db, message)
        with pytest.raises(ValueError):
            regenerate_message(db, message)


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------

class TestSequence:
    def _sent_message(self, db, days_ago: float = 0.0) -> OutreachMessage:
        app = _make_application(db)
        contact = _make_contact(db, app)
        message = OutreachMessage(contact_id=contact.id, application_id=app.id,
                                  body="Hello.", status="draft", sequence_step=1)
        db.add(message)
        db.flush()
        mark_sent(db, message, when=datetime.now(timezone.utc) - timedelta(days=days_ago))
        return message

    def test_followup_days_parses_the_setting(self):
        with patch.object(outreach_service.settings, "OUTREACH_FOLLOWUP_DAYS", "3, 6,9"):
            assert followup_days() == [3, 6, 9]

    def test_followup_days_ignores_junk(self):
        with patch.object(outreach_service.settings, "OUTREACH_FOLLOWUP_DAYS", "3,,x,-2,0"):
            assert followup_days() == [3]

    def test_sending_schedules_the_first_follow_up(self, db):
        message = self._sent_message(db)
        assert message.follow_up_due_at is not None
        assert message.status == "sent"

    def test_the_last_step_schedules_nothing_further(self, db):
        message = self._sent_message(db)
        message.sequence_step = len(followup_days()) + 1
        mark_sent(db, message)
        assert message.follow_up_due_at is None

    def test_a_reply_stops_the_sequence(self, db):
        message = self._sent_message(db)
        mark_replied(db, message)
        assert message.status == "replied"
        assert message.follow_up_due_at is None

    def test_a_reply_discards_pending_follow_up_drafts(self, db):
        message = self._sent_message(db)
        contact = message.contact
        db.add(OutreachMessage(contact_id=contact.id, kind="follow_up",
                               body="chasing", status="draft", sequence_step=2))
        db.flush()
        db.refresh(contact)
        mark_replied(db, message)
        db.refresh(contact)
        assert [m.kind for m in contact.messages] == ["initial"]

    def test_due_follow_ups_finds_an_elapsed_one(self, db):
        self._sent_message(db, days_ago=30)
        assert len(due_follow_ups(db)) == 1

    def test_due_follow_ups_ignores_a_fresh_one(self, db):
        self._sent_message(db, days_ago=0)
        assert due_follow_ups(db) == []

    def test_drafting_due_follow_ups_writes_the_next_step(self, db, profile):
        message = self._sent_message(db, days_ago=30)
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            drafted = draft_due_follow_ups(db)
        assert len(drafted) == 1
        assert drafted[0].kind == "follow_up"
        assert drafted[0].sequence_step == 2
        assert message.follow_up_due_at is None

    def test_an_archived_contact_is_not_chased(self, db, profile):
        message = self._sent_message(db, days_ago=30)
        message.contact.archived = True
        db.flush()
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            assert draft_due_follow_ups(db) == []

    def test_the_sequence_stops_at_the_configured_length(self, db, profile):
        message = self._sent_message(db, days_ago=30)
        message.sequence_step = 99
        db.flush()
        with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "x " * 30):
            assert draft_due_follow_ups(db) == []

    def test_next_step_counts_existing_messages(self, db):
        app = _make_application(db)
        contact = _make_contact(db, app)
        assert next_step(contact) == 1
        db.add(OutreachMessage(contact_id=contact.id, body="x", sequence_step=3))
        db.flush()
        db.refresh(contact)
        assert next_step(contact) == 4


class TestSetMessageStatus:
    def test_skipping_clears_the_follow_up(self, db):
        app = _make_application(db)
        contact = _make_contact(db, app)
        message = OutreachMessage(contact_id=contact.id, body="x", status="draft")
        db.add(message)
        db.flush()
        mark_sent(db, message)
        set_message_status(db, message, "skipped")
        assert message.follow_up_due_at is None

    def test_approving_leaves_it_a_draft_in_spirit(self, db):
        app = _make_application(db)
        contact = _make_contact(db, app)
        message = OutreachMessage(contact_id=contact.id, body="x", status="draft")
        db.add(message)
        db.flush()
        set_message_status(db, message, "approved")
        assert message.status == "approved" and message.sent_at is None

    def test_rejects_an_unknown_status(self, db):
        app = _make_application(db)
        contact = _make_contact(db, app)
        message = OutreachMessage(contact_id=contact.id, body="x", status="draft")
        db.add(message)
        db.flush()
        with pytest.raises(ValueError):
            set_message_status(db, message, "posted-a-letter")


# ---------------------------------------------------------------------------
# Orchestration and stats
# ---------------------------------------------------------------------------

class TestRunOutreach:
    def test_discovers_and_drafts(self, db, profile):
        app = _make_application(db, description="Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.generation_chat", return_value="Hi, " + "x " * 30):
                contacts = run_outreach(db, app)
        assert len(contacts) == 1
        assert len(contacts[0].messages) == 1

    def test_does_not_start_a_second_thread_for_a_known_contact(self, db, profile):
        app = _make_application(db, description="Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            with patch("app.services.outreach.generation_chat", return_value="Hi, " + "x " * 30):
                run_outreach(db, app)
                contacts = run_outreach(db, app)
        assert len(contacts[0].messages) == 1

    def test_draft_false_only_discovers(self, db, profile):
        app = _make_application(db, description="Email talent@acme.com")
        with patch("app.services.outreach.resolve_company_domain", return_value=("acme.com", "url")):
            contacts = run_outreach(db, app, draft=False)
        assert contacts and contacts[0].messages == []

    def test_disabled_configuration_does_nothing(self, db):
        app = _make_application(db)
        with patch.object(outreach_service.settings, "OUTREACH_ENABLED", False):
            assert run_outreach(db, app) == []


class TestSearchLinks:
    def test_offers_the_standard_angles(self, db, profile):
        app = _make_application(db)
        labels = [l["label"] for l in outreach_service.search_links(db, app)]
        assert "Recruiters at Acme Corp" in labels
        assert "Engineering managers at Acme Corp" in labels

    def test_leads_with_the_alumni_angle(self, db):
        db.add(Profile(data={**PROFILE, "education": [{"school": "Northeastern University"}]}))
        db.flush()
        app = _make_application(db)
        first = outreach_service.search_links(db, app)[0]
        assert first["label"] == "Northeastern University alumni at Acme Corp"

    def test_uses_a_slug_quoted_in_the_posting(self, db, profile):
        app = _make_application(
            db, description="We're at https://linkedin.com/company/acme-hq/ — say hi"
        )
        assert all("/company/acme-hq/" in l["url"] for l in outreach_service.search_links(db, app))

    def test_every_link_is_a_real_url(self, db, profile):
        app = _make_application(db)
        assert all(l["url"].startswith("https://") for l in outreach_service.search_links(db, app))


class TestDiscoveryStale:
    def test_a_fresh_search_is_not_stale(self, db):
        app = _make_application(db)
        app.outreach_status = "discovering"
        app.outreach_checked_at = datetime.now(timezone.utc)
        assert outreach_service.discovery_stale(app) is False

    def test_an_old_search_is_stale(self, db):
        app = _make_application(db)
        app.outreach_status = "discovering"
        app.outreach_checked_at = datetime.now(timezone.utc) - timedelta(hours=2)
        assert outreach_service.discovery_stale(app) is True

    def test_a_search_with_no_start_time_is_stale(self, db):
        app = _make_application(db)
        app.outreach_status = "discovering"
        assert outreach_service.discovery_stale(app) is True

    def test_an_idle_application_is_never_stale(self, db):
        app = _make_application(db)
        assert outreach_service.discovery_stale(app) is False


class TestPriorConversations:
    def _two_apps_one_person(self, db):
        first_app = _make_application(db, title="Backend Engineer")
        second_app = _make_application(db, title="Platform Engineer", company="Acme, Inc.")
        first = _make_contact(db, first_app, email="sam@acme.com")
        second = _make_contact(db, second_app, email="sam@acme.com")
        second.company_key = first.company_key
        db.flush()
        return first, second

    def test_reports_an_earlier_sent_message(self, db):
        first, second = self._two_apps_one_person(db)
        message = OutreachMessage(contact_id=first.id, application_id=first.application_id,
                                  body="Hello.", status="draft")
        db.add(message)
        db.flush()
        mark_sent(db, message)
        earlier = outreach_service.prior_conversations(db, second)
        assert [e["title"] for e in earlier] == ["Backend Engineer"]

    def test_ignores_an_unsent_draft(self, db):
        first, second = self._two_apps_one_person(db)
        db.add(OutreachMessage(contact_id=first.id, body="Hello.", status="draft"))
        db.flush()
        assert outreach_service.prior_conversations(db, second) == []

    def test_does_not_report_the_contact_to_itself(self, db):
        first, _ = self._two_apps_one_person(db)
        message = OutreachMessage(contact_id=first.id, application_id=first.application_id,
                                  body="Hello.", status="draft")
        db.add(message)
        db.flush()
        mark_sent(db, message)
        assert outreach_service.prior_conversations(db, first) == []

    def test_nothing_for_a_contact_with_no_address(self, db):
        app = _make_application(db)
        contact = _make_contact(db, app, email=None, email_status="unknown")
        assert outreach_service.prior_conversations(db, contact) == []


class TestOutreachStats:
    def test_counts_by_status(self, db):
        app = _make_application(db)
        contact = _make_contact(db, app)
        db.add_all([
            OutreachMessage(contact_id=contact.id, body="a", status="draft"),
            OutreachMessage(contact_id=contact.id, body="b", status="sent"),
            OutreachMessage(contact_id=contact.id, body="c", status="replied"),
        ])
        db.flush()
        stats = outreach_stats(db)
        assert stats["drafts"] == 1
        assert stats["sent"] == 2      # a reply was also sent
        assert stats["replied"] == 1
        assert stats["reply_rate"] == 50

    def test_reply_rate_is_zero_with_nothing_sent(self, db):
        assert outreach_stats(db)["reply_rate"] == 0


# ---------------------------------------------------------------------------
# The JSON trigger endpoint
# ---------------------------------------------------------------------------

class TestOutreachEndpoint:
    def _client(self, mock_db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.routers.outreach import router

        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_returns_202_for_a_valid_application(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock()
        with patch("app.routers.outreach.run_outreach"):
            response = self._client(mock_db).post(f"/api/apps/{uuid.uuid4()}/outreach")
        assert response.status_code == 202
        assert response.json()["status"] == "ok"

    def test_returns_404_for_a_missing_application(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        response = self._client(mock_db).post(f"/api/apps/{uuid.uuid4()}/outreach")
        assert response.status_code == 404

    def test_calls_run_outreach(self):
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        with patch("app.routers.outreach.run_outreach") as mock_run:
            self._client(mock_db).post(f"/api/apps/{uuid.uuid4()}/outreach")
        mock_run.assert_called_once_with(mock_db, mock_app)
