"""
Applications in flight: status moves, next actions, the board, reminders and
what gets replies.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import tracker, tunables
from app.services.document_edit import EDITED_BY

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def make_app(db, title="Engineer", company="Acme", status=ApplicationStatus.not_applied,
             score=80, **extra):
    job = Job(source="greenhouse", url=f"https://x/{uuid.uuid4()}", source_urls=[], title=title,
              company=company, description="Python.", status=JobStatus.matched, llm_score=score,
              fetched_at=NOW, dedupe_hash=uuid.uuid4().hex)
    db.add(job)
    db.flush()
    application = Application(job_id=job.id, status=status, **extra)
    db.add(application)
    db.flush()
    return application


def add_doc(db, application, doc_type=DocType.resume, generated_by="model-a", content=None):
    doc = ApplicationDocument(application_id=application.id, doc_type=doc_type, version=1,
                              path="/tmp/x.pdf", is_current=True, generated_by=generated_by,
                              content=content)
    db.add(doc)
    db.flush()
    db.refresh(application)
    return doc


@pytest.fixture
def profile(db):
    db.query(Profile).delete()
    db.add(Profile(data={}))
    db.commit()


class TestAStatusMove:
    def test_applying_stamps_the_time_and_what_was_sent(self, db):
        application = make_app(db)
        resume = add_doc(db, application)
        add_doc(db, application, DocType.cover_letter)
        tracker.set_status(db, application, ApplicationStatus.applied, now=NOW)
        assert application.applied_at == NOW and application.status_changed_at == NOW
        assert application.sent_resume_id == resume.id and application.sent_cover_letter is True
        assert application.next_action == "Follow up if there is no reply"
        assert application.next_action_due == TODAY + timedelta(days=7)

    def test_the_follow_up_comes_from_the_settings_page(self, db):
        application = make_app(db)
        tracker.set_status(db, application, ApplicationStatus.applied, now=NOW,
                           profile_data={tunables.STORE_KEY: {"followup_after_days": 3}})
        assert application.next_action_due == TODAY + timedelta(days=3)
        assert application.sent_cover_letter is False and application.sent_resume_id is None

    def test_a_default_moves_with_the_status_and_your_own_stays(self, db):
        application = make_app(db)
        tracker.set_status(db, application, ApplicationStatus.applied, now=NOW)
        tracker.set_status(db, application, ApplicationStatus.interviewing, now=NOW)
        assert application.next_action.startswith("Send a thank-you")
        assert application.next_action_due == TODAY + timedelta(days=1)
        tracker.set_next_action(application, "Email Ana the take-home", TODAY + timedelta(days=2))
        tracker.set_status(db, application, ApplicationStatus.offered, now=NOW)
        assert application.next_action == "Email Ana the take-home"

    def test_what_was_sent_is_recorded_once(self, db):
        application = make_app(db)
        first = add_doc(db, application)
        tracker.set_status(db, application, ApplicationStatus.applied, now=NOW)
        first.is_current = False
        add_doc(db, application)
        tracker.set_status(db, application, ApplicationStatus.interviewing, now=NOW)
        tracker.set_status(db, application, ApplicationStatus.applied, now=NOW)
        assert application.sent_resume_id == first.id

    def test_straight_to_interviewing_still_records_the_sending(self, db):
        application = make_app(db)
        resume = add_doc(db, application)
        tracker.set_status(db, application, ApplicationStatus.interviewing, now=NOW)
        assert application.applied_at == NOW and application.sent_resume_id == resume.id

    def test_withdrawing_before_applying_records_nothing_sent(self, db):
        application = make_app(db)
        add_doc(db, application)
        tracker.set_status(db, application, ApplicationStatus.withdrawn, now=NOW)
        assert application.applied_at is None and application.sent_resume_id is None

    def test_rejection_clears_the_next_action(self, db):
        application = make_app(db)
        tracker.set_status(db, application, ApplicationStatus.applied, now=NOW)
        tracker.set_status(db, application, ApplicationStatus.rejected, now=NOW)
        assert application.next_action is None and application.next_action_due is None

    def test_the_application_page_goes_through_it(self, client, db, profile):
        application = make_app(db)
        db.commit()
        client.post(f"/apps/{application.id}/status", data={"status": "applied"})
        db.refresh(application)
        # The page never set applied_at before; only the extension did.
        assert application.applied_at is not None and application.next_action


class TestTheExtensionMarkingApplied:
    def test_it_records_the_same_as_the_page(self, db, profile):
        from app.routers import agent

        application = make_app(db)
        resume = add_doc(db, application)
        db.commit()
        with patch("app.services.job_context.find_job", return_value=application.job):
            result = agent._mark_applied(db, application.job.url)
        assert result["changed"] is True
        db.refresh(application)
        assert application.sent_resume_id == resume.id
        assert application.next_action == "Follow up if there is no reply"


class TestNextActionAndLetterRoutes:
    def test_setting_your_own(self, client, db, profile):
        application = make_app(db, status=ApplicationStatus.applied)
        db.commit()
        client.post(f"/apps/{application.id}/next-action",
                    data={"next_action": "Ping the recruiter", "due": "2026-10-02"})
        db.refresh(application)
        assert (application.next_action, application.next_action_due) == (
            "Ping the recruiter", date(2026, 10, 2))
        assert client.post(f"/apps/{application.id}/next-action",
                           data={"next_action": "x", "due": "soon"}).status_code == 422

    def test_whether_a_letter_went(self, client, db, profile):
        application = make_app(db, status=ApplicationStatus.applied, sent_cover_letter=True)
        db.commit()
        client.post(f"/apps/{application.id}/sent-letter", data={})
        db.refresh(application)
        assert application.sent_cover_letter is False

    def test_the_page_shows_them(self, client, db, profile):
        application = make_app(db, status=ApplicationStatus.applied,
                               next_action="Follow up", next_action_due=date(2020, 1, 1))
        db.commit()
        page = client.get(f"/apps/{application.id}").text
        assert 'value="Follow up"' in page and "border-red-400" in page
        assert "A cover letter went with it" in page


class TestTheBoard:
    def test_columns_hold_their_applications_with_what_is_next(self, db):
        applied = make_app(db, title="Applied one", status=ApplicationStatus.applied,
                           next_action="Follow up", next_action_due=TODAY - timedelta(days=2),
                           status_changed_at=NOW - timedelta(days=9))
        make_app(db, title="Interviewing", status=ApplicationStatus.interviewing)
        ready = make_app(db, title="Ready to send", generation_status="done", score=90)
        make_app(db, title="Backlog", generation_status="idle")
        columns = tracker.board(db, now=NOW)
        card = columns["applied"][0]
        assert card["app"].id == applied.id and card["overdue"] and card["days_in_column"] == 9
        assert [c["job"].title for c in columns["interviewing"]] == ["Interviewing"]
        # To apply: documents written (or starred), not the whole backlog.
        assert [c["app"].id for c in columns["not_applied"]] == [ready.id]

    def test_the_page_renders_and_moving_a_card_reloads(self, client, db, profile):
        application = make_app(db, title="Platform Engineer", status=ApplicationStatus.applied,
                               next_action="Follow up", next_action_due=date(2020, 1, 1))
        db.commit()
        page = client.get("/apps/board").text
        assert "Applications board" in page and "Platform Engineer" in page and "d late" in page
        reply = client.post(f"/apps/{application.id}/status?fragment=board",
                            data={"status": "interviewing"})
        assert reply.headers.get("HX-Refresh") == "true"
        assert "Board view" in client.get("/apps").text


class TestReminders:
    def test_due_actions_are_one_warning_in_the_log(self, db, caplog):
        from app.tasks.tracker import remind_due_actions

        make_app(db, title="Late", status=ApplicationStatus.applied,
                 next_action="Follow up", next_action_due=date.today() - timedelta(days=3))
        make_app(db, title="Today", status=ApplicationStatus.interviewing,
                 next_action="Thank-you note", next_action_due=date.today())
        make_app(db, title="Later", status=ApplicationStatus.applied,
                 next_action="Follow up", next_action_due=date.today() + timedelta(days=3))
        make_app(db, title="Closed", status=ApplicationStatus.rejected,
                 next_action="Follow up", next_action_due=date.today() - timedelta(days=3))
        db.commit()
        with patch("app.tasks.tracker.SessionLocal", return_value=db), \
                patch.object(db, "close"), caplog.at_level("WARNING"):
            result = remind_due_actions()
        assert result["due"] == 2
        warning = next(r for r in caplog.records if r.levelname == "WARNING")
        assert "2 application follow-ups due" in warning.message
        assert "Late at Acme (3 days late)" in warning.message and "(today)" in warning.message

    def test_its_interval_comes_from_the_settings_page(self):
        from app.tasks.schedule import HOUR, SCHEDULE, interval_seconds

        entry = next(e for e in SCHEDULE if e.name == "remind-due-actions")
        assert interval_seconds(
            entry, {tunables.STORE_KEY: {"reminder_interval_hours": 6}}) == 6 * HOUR


class TestWhatGetsReplies:
    def sent(self, db, status, letter, edited=False, coverage=(4, 5), model="model-a"):
        application = make_app(db, status=status, sent_cover_letter=letter)
        doc = add_doc(db, application, generated_by=EDITED_BY if edited else model,
                      content={"ats": {"keywords": ["k"] * coverage[1],
                                       "present": ["k"] * coverage[0]}})
        application.sent_resume_id = doc.id
        db.flush()

    def test_grouped_by_what_was_sent_with_rates_only_on_enough(self, db):
        for status in (ApplicationStatus.interviewing, ApplicationStatus.rejected,
                       ApplicationStatus.applied, ApplicationStatus.applied,
                       ApplicationStatus.offered):
            self.sent(db, status, letter=True)
        self.sent(db, ApplicationStatus.applied, letter=False, edited=True, coverage=(1, 5))
        make_app(db)   # never sent: not counted
        db.commit()
        rates = tracker.response_rates(db)
        assert rates["total"] == 6
        letter = {r["answer"]: r for r in rates["groups"]["Cover letter"]}
        assert (letter["With a cover letter"]["sent"], letter["With a cover letter"]["heard"],
                letter["With a cover letter"]["interviews"]) == (5, 3, 2)
        assert letter["With a cover letter"]["heard_rate"] == pytest.approx(0.6)
        assert letter["Without one"]["heard_rate"] is None   # one application is not a rate
        resume = {r["answer"]: r["sent"] for r in rates["groups"]["Resume"]}
        assert resume == {"As generated": 5, "Edited by hand": 1}
        coverage = {r["answer"]: r["sent"] for r in rates["groups"]["Keywords a parser read"]}
        assert coverage == {"80% or more": 5, "under 50%": 1}

    def test_the_applications_page_shows_them(self, client, db, profile):
        self.sent(db, ApplicationStatus.interviewing, letter=True)
        db.commit()
        page = client.get("/apps").text
        assert "What gets replies" in page and "With a cover letter" in page
