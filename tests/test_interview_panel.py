"""
Interview reports on an application page.

The trigger rule the whole system follows: automation decides when something
usually happens, the user decides when it happens now. The mailbox poller is
meant to fire this on an interview invite; the button is the same work on
demand, because "I have an interview on Thursday" arrives before any automation
notices.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.application import Application
from app.models.job import Job
from app.services import interview_corpus
from app.services.deduplication import compute_dedupe_hash

NOW = datetime.now(timezone.utc)


@pytest.fixture
def application(db):
    job = Job(
        source="greenhouse",
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Backend Engineer",
        company="Acme Corp",
        location="Boston, MA",
        description="A job.",
        dedupe_hash=compute_dedupe_hash("Acme Corp", "Backend Engineer", "Boston, MA"),
        fetched_at=NOW,
    )
    db.add(job)
    db.commit()
    app_obj = Application(job_id=job.id)
    db.add(app_obj)
    db.commit()
    return app_obj


def add_report(db, **kwargs):
    interview_corpus.ingest(db, [{
        "company": kwargs.pop("company", "Acme Corp"),
        "source": kwargs.pop("source", "reddit"),
        "url": kwargs.pop("url", "https://reddit.com/r/leetcode/comments/1"),
        "title": kwargs.pop("title", "Acme Corp interview experience — SDE-1"),
        "body": kwargs.pop("body", "Three rounds. " * 40),
        "posted_at": kwargs.pop("posted_at", NOW - timedelta(days=40)),
        "role_hint": kwargs.pop("role_hint", "SDE-1"),
    }])


class TestPanelOnTheApplication:
    def test_it_appears(self, client, application):
        body = client.get(f"/apps/{application.id}").text
        assert "Interview reports" in body

    def test_an_empty_corpus_says_why_it_is_worth_gathering(self, client, application):
        # Before applying, not just before interviewing: round count is how you
        # tell a two-week loop from a four-week one.
        body = client.get(f"/apps/{application.id}").text
        assert "Nothing gathered" in body
        assert "two-week loop" in body

    def test_stored_reports_are_listed(self, client, db, application):
        add_report(db)
        body = client.get(f"/apps/{application.id}").text
        assert "interview experience" in body.lower()
        assert "reddit" in body

    def test_a_report_shows_its_date_and_level(self, client, db, application):
        add_report(db)
        body = client.get(f"/apps/{application.id}").text
        assert (NOW - timedelta(days=40)).strftime("%b %Y") in body
        assert "SDE-1" in body

    def test_another_companys_reports_are_not_shown(self, client, db, application):
        add_report(db, company="Globex", url="https://x/globex", title="Globex writeup")
        assert "Globex writeup" not in client.get(f"/apps/{application.id}").text

    def test_coverage_is_summarised(self, client, db, application):
        add_report(db)
        assert "from the last six months" in client.get(f"/apps/{application.id}").text


class TestGatherButton:
    def test_it_reports_what_each_source_returned(self, client, db, application, monkeypatch):
        # Per-source counts, because a broken source and a company nobody wrote
        # about are the same zero in a total.
        from app.services import interview_sources

        def fake_fetch_all(company, only=None):
            return {
                "reports": [{
                    "source": "reddit", "company": company,
                    "url": "https://reddit.com/r/leetcode/comments/new",
                    "title": "Acme Corp interview experience",
                    "body": "Two rounds. " * 40,
                    "posted_at": NOW - timedelta(days=5), "role_hint": None,
                }],
                "sources": {
                    "reddit": {"count": 1, "error": None},
                    "github": {"count": 0, "error": None},
                    "geeksforgeeks": {"count": 0, "error": "index moved"},
                },
            }

        monkeypatch.setattr(interview_sources, "fetch_all", fake_fetch_all)
        body = client.post(f"/apps/{application.id}/interview-research").text
        assert "Stored 1" in body
        assert "index moved" in body

    def test_what_it_gathered_is_immediately_visible(self, client, db, application, monkeypatch):
        from app.services import interview_sources

        monkeypatch.setattr(interview_sources, "fetch_all", lambda company, only=None: {
            "reports": [{
                "source": "reddit", "company": company,
                "url": "https://reddit.com/r/leetcode/comments/fresh",
                "title": "Fresh Acme Corp writeup",
                "body": "Rounds. " * 40,
                "posted_at": NOW - timedelta(days=3), "role_hint": None,
            }],
            "sources": {"reddit": {"count": 1, "error": None}},
        })
        assert "Fresh Acme Corp writeup" in client.post(
            f"/apps/{application.id}/interview-research"
        ).text

    def test_a_fetch_failure_is_shown_not_raised(self, client, application, monkeypatch):
        from app.services import interview_sources

        def explode(company, only=None):
            raise RuntimeError("everything is on fire")

        monkeypatch.setattr(interview_sources, "fetch_all", explode)
        response = client.post(f"/apps/{application.id}/interview-research")
        assert response.status_code == 200
        assert "everything is on fire" in response.text

    def test_an_unknown_application_is_a_404(self, client):
        response = client.post(
            "/apps/11111111-1111-1111-1111-111111111111/interview-research"
        )
        assert response.status_code == 404


class TestDegradation:
    def test_a_corpus_failure_does_not_break_the_application_page(self, client, application, monkeypatch):
        # The application page is what somebody was actually trying to read.
        from app.services import interview_corpus as corpus

        def explode(db, company, **kwargs):
            raise RuntimeError("corpus unavailable")

        monkeypatch.setattr(corpus, "reports_for", explode)
        assert client.get(f"/apps/{application.id}").status_code == 200
