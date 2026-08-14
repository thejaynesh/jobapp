"""
/api/agent/* over HTTP.

Two things this checks that the service-layer tests cannot: that the prefix is
actually closed to unauthenticated callers, and that the long poll returns
rather than hanging when the queue is empty.
"""

import pytest

from app.config import settings
from app.services import browser_tasks

TOKEN = "test-agent-token-value"


@pytest.fixture
def agent(client, monkeypatch):
    """A client with agent auth switched on and a known token."""
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "APP_PASSWORD", "irrelevant-but-required")
    monkeypatch.setattr(settings, "SECRET_KEY", "not-the-placeholder-value")
    # Keep polls from actually waiting; the waiting itself is tested separately.
    monkeypatch.setattr(settings, "AGENT_POLL_MAX_WAIT_SECONDS", 0)
    return client


def auth_header(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def no_broker(monkeypatch):
    """
    Queue tasks into nothing.

    Without this the endpoint really tries to reach Redis and waits out the
    connect timeout, which makes these tests slow and — worse — makes them
    depend on whether a broker happens to be reachable from the test host.
    """
    from app.tasks import generate

    sent = []
    monkeypatch.setattr(
        generate.generate_docs, "apply_async", lambda *a, **k: sent.append((a, k))
    )
    return sent


class TestAuthentication:
    def test_rejects_a_missing_token(self, agent):
        assert agent.get("/api/agent/hello").status_code == 401

    def test_rejects_a_wrong_token(self, agent):
        response = agent.get("/api/agent/hello", headers=auth_header("wrong"))
        assert response.status_code == 401

    def test_rejects_a_non_bearer_scheme(self, agent):
        response = agent.get(
            "/api/agent/hello", headers={"Authorization": f"Basic {TOKEN}"}
        )
        assert response.status_code == 401

    def test_accepts_the_configured_token(self, agent):
        assert agent.get("/api/agent/hello", headers=auth_header()).status_code == 200

    def test_closed_when_no_token_is_configured(self, agent, monkeypatch):
        # Not 401: there is no token that would work, and saying so is the
        # difference between "your credential is wrong" and "fix your server".
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        response = agent.get("/api/agent/hello", headers=auth_header())
        assert response.status_code == 503
        assert "AGENT_TOKEN" in response.json()["detail"]

    def test_the_session_cookie_does_not_open_the_agent_api(self, agent):
        # The two credentials are deliberately separate; a browser session must
        # not be a way into the queue.
        agent.post("/login", data={"password": "irrelevant-but-required"})
        assert agent.get("/api/agent/hello").status_code == 401


class TestHello:
    def test_reports_what_the_server_speaks(self, agent):
        body = agent.get("/api/agent/hello", headers=auth_header()).json()
        assert body["ok"] is True
        assert "ping" in body["kinds"]
        assert body["protocol"] == 1
        assert body["lease_seconds"] > 0

    def test_reports_queue_depth(self, agent, db):
        browser_tasks.enqueue(db, "ping")
        body = agent.get("/api/agent/hello", headers=auth_header()).json()
        assert body["queue"]["queued"] == 1


class TestLease:
    def test_empty_queue_returns_no_tasks_not_an_error(self, agent):
        response = agent.post("/api/agent/lease", json={}, headers=auth_header())
        assert response.status_code == 200
        assert response.json()["tasks"] == []

    def test_leases_available_work(self, agent, db):
        browser_tasks.enqueue(db, "ping", {"hello": "world"})
        body = agent.post(
            "/api/agent/lease",
            json={"kinds": ["ping"], "agent_id": "ext-1"},
            headers=auth_header(),
        ).json()
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["payload"] == {"hello": "world"}

    def test_an_unknown_kind_is_a_client_error(self, agent):
        response = agent.post(
            "/api/agent/lease", json={"kinds": ["nope"]}, headers=auth_header()
        )
        assert response.status_code == 400

    def test_a_missing_body_is_tolerated(self, agent):
        assert agent.post("/api/agent/lease", headers=auth_header()).status_code == 200

    def test_wait_is_capped_by_the_server(self, agent, monkeypatch):
        # A client asking to wait an hour would otherwise tie up a worker for
        # one; the server's ceiling wins.
        monkeypatch.setattr(settings, "AGENT_POLL_MAX_WAIT_SECONDS", 0)
        response = agent.post(
            "/api/agent/lease", json={"wait": 3600}, headers=auth_header()
        )
        assert response.status_code == 200


class TestReporting:
    def _leased_id(self, agent, db):
        browser_tasks.enqueue(db, "ping")
        body = agent.post(
            "/api/agent/lease", json={"agent_id": "ext-1"}, headers=auth_header()
        ).json()
        return body["tasks"][0]["id"]

    def test_posting_a_result_completes_the_task(self, agent, db):
        task_id = self._leased_id(agent, db)
        body = agent.post(
            f"/api/agent/tasks/{task_id}/result",
            json={"result": {"pong": True}, "agent_id": "ext-1"},
            headers=auth_header(),
        ).json()
        assert body["status"] == "done"

    def test_a_non_dict_result_is_still_stored(self, agent, db):
        task_id = self._leased_id(agent, db)
        response = agent.post(
            f"/api/agent/tasks/{task_id}/result",
            json={"result": "a bare string", "agent_id": "ext-1"},
            headers=auth_header(),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "done"

    def test_posting_a_failure_requeues_it(self, agent, db):
        task_id = self._leased_id(agent, db)
        body = agent.post(
            f"/api/agent/tasks/{task_id}/fail",
            json={"error": "page never loaded", "agent_id": "ext-1"},
            headers=auth_header(),
        ).json()
        # Requeued rather than retired, so the agent can tell a retry from a
        # dead end without asking again.
        assert body["status"] == "queued"

    def test_an_agent_can_flag_a_failure_as_final(self, agent, db):
        # Only the agent knows whether a retry could ever help.
        task_id = self._leased_id(agent, db)
        body = agent.post(
            f"/api/agent/tasks/{task_id}/fail",
            json={"error": "HTTP 403 from reddit.com", "permanent": True,
                  "agent_id": "ext-1"},
            headers=auth_header(),
        ).json()
        assert body["status"] == "failed"

    def test_a_failure_with_no_body_is_accepted(self, agent, db):
        task_id = self._leased_id(agent, db)
        response = agent.post(
            f"/api/agent/tasks/{task_id}/fail", headers=auth_header()
        )
        assert response.status_code == 200

    def test_heartbeat_keeps_the_task_leased(self, agent, db):
        task_id = self._leased_id(agent, db)
        body = agent.post(
            f"/api/agent/tasks/{task_id}/heartbeat",
            json={"agent_id": "ext-1"},
            headers=auth_header(),
        ).json()
        assert body["status"] == "leased"

    def test_reporting_on_unknown_work_is_a_client_error(self, agent):
        response = agent.post(
            "/api/agent/tasks/11111111-1111-1111-1111-111111111111/result",
            json={"result": {}},
            headers=auth_header(),
        )
        assert response.status_code == 400

    def test_reporting_twice_is_refused(self, agent, db):
        task_id = self._leased_id(agent, db)
        agent.post(
            f"/api/agent/tasks/{task_id}/result",
            json={"result": {}, "agent_id": "ext-1"},
            headers=auth_header(),
        )
        second = agent.post(
            f"/api/agent/tasks/{task_id}/result",
            json={"result": {}, "agent_id": "ext-1"},
            headers=auth_header(),
        )
        assert second.status_code == 400


class TestRoundTrip:
    def test_enqueue_lease_execute_report(self, agent, db):
        """The whole protocol, as the extension actually walks it."""
        task = browser_tasks.enqueue(db, "ping", {"n": 1})

        leased = agent.post(
            "/api/agent/lease",
            json={"kinds": ["ping"], "agent_id": "ext-1"},
            headers=auth_header(),
        ).json()["tasks"]
        assert [t["id"] for t in leased] == [str(task.id)]

        agent.post(
            f"/api/agent/tasks/{task.id}/result",
            json={"result": {"pong": True, "echo": {"n": 1}}, "agent_id": "ext-1"},
            headers=auth_header(),
        )

        db.expire_all()
        db.refresh(task)
        assert task.status == "done"
        assert task.result["echo"] == {"n": 1}


class TestHarvest:
    """Job JSON the browser offers, rather than work it was given."""

    PAYLOAD = {
        "elements": [
            {
                "jobPostingId": 8812345,
                "title": "Platform Engineer",
                "companyName": "Initech",
                "formattedLocation": "Remote",
                "description": {"text": "Kubernetes, mostly."},
            }
        ]
    }

    def test_needs_the_agent_token(self, agent):
        response = agent.post("/api/agent/harvest", json={"payload": self.PAYLOAD})
        assert response.status_code == 401

    def test_stores_what_it_finds(self, agent, db):
        from app.models.job import Job

        body = agent.post(
            "/api/agent/harvest",
            json={"payload": self.PAYLOAD, "source_url": "https://www.linkedin.com/jobs/"},
            headers=auth_header(),
        ).json()
        assert body["found"] == 1
        assert body["inserted"] == 1
        assert db.query(Job).one().company == "Initech"

    def test_a_payload_with_no_jobs_is_a_normal_outcome(self, agent):
        # The interceptor forwards indiscriminately on purpose, so "nothing in
        # this one" is the common case rather than a client error.
        response = agent.post(
            "/api/agent/harvest",
            json={"payload": {"feed": ["nothing relevant"]}},
            headers=auth_header(),
        )
        assert response.status_code == 200
        assert response.json()["found"] == 0

    def test_a_missing_payload_is_refused(self, agent):
        response = agent.post("/api/agent/harvest", json={}, headers=auth_header())
        assert response.status_code == 400

    def test_offering_the_same_page_twice_stores_one_job(self, agent, db):
        from app.models.job import Job

        for _ in range(2):
            agent.post(
                "/api/agent/harvest", json={"payload": self.PAYLOAD}, headers=auth_header()
            )
        assert db.query(Job).count() == 1

    def test_an_ingestion_failure_is_not_charged_to_the_browser(self, agent, monkeypatch):
        # The browser volunteered this and has nothing to retry; a parsing bug
        # of ours must not come back to it as an error.
        from app.services import harvest as harvest_service

        def explode(payload):
            raise RuntimeError("parser is broken")

        monkeypatch.setattr(harvest_service, "extract_jobs", explode)
        response = agent.post(
            "/api/agent/harvest", json={"payload": self.PAYLOAD}, headers=auth_header()
        )
        assert response.status_code == 200
        assert response.json()["found"] == 0


class TestJobContextEndpoint:
    """What the on-page overlay reads."""

    def _job(self, db):
        from datetime import datetime, timezone
        from app.models.job import Job
        from app.services.deduplication import compute_dedupe_hash

        job = Job(
            source="linkedin",
            source_urls=["https://www.linkedin.com/jobs/view/3901234567/"],
            url="https://www.linkedin.com/jobs/view/3901234567/",
            title="Senior Backend Engineer",
            company="Acme Corp",
            location="Boston, MA",
            description="A job.",
            llm_score=77,
            matched_by="llm",
            dedupe_hash=compute_dedupe_hash("Acme Corp", "Senior Backend Engineer", "Boston, MA"),
            fetched_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        return job

    def test_needs_the_agent_token(self, agent):
        response = agent.get("/api/agent/job-context?url=https://example.com/1")
        assert response.status_code == 401

    def test_reports_an_unknown_posting(self, agent):
        body = agent.get(
            "/api/agent/job-context?url=https://example.com/nope", headers=auth_header()
        ).json()
        assert body == {"known": False}

    def test_reports_a_known_posting(self, agent, db):
        self._job(db)
        body = agent.get(
            "/api/agent/job-context?url=https://www.linkedin.com/jobs/view/3901234567/?refId=x",
            headers=auth_header(),
        ).json()
        assert body["known"] is True
        assert body["job"]["score"] == 77

    def test_a_missing_url_is_refused(self, agent):
        response = agent.get("/api/agent/job-context", headers=auth_header())
        assert response.status_code == 400

    def test_prepare_opens_an_application(self, agent, db, no_broker):
        job = self._job(db)
        body = agent.post(
            "/api/agent/prepare",
            json={"url": job.url},
            headers=auth_header(),
        ).json()
        assert body["ok"] is True
        assert body["path"].startswith("/apps/")

    def test_prepare_without_a_url_is_refused(self, agent):
        response = agent.post("/api/agent/prepare", json={}, headers=auth_header())
        assert response.status_code == 400

    def test_prepare_stores_an_unknown_posting_from_the_page(self, agent, db, no_broker):
        from app.models.job import Job

        body = agent.post(
            "/api/agent/prepare",
            json={
                "url": "https://boards.greenhouse.io/example/jobs/7",
                "posting": {"title": "Platform Engineer", "company": "Example Inc"},
            },
            headers=auth_header(),
        ).json()
        assert body["ok"] is True
        assert db.query(Job).filter(Job.company == "Example Inc").count() == 1
