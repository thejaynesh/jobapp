from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.models.company import Company
from app.models.company_board import CompanyBoard
from app.models.profile import Profile
from app.services import company_identity
from app.services.tunables import STORE_KEY
from tests.test_application_history import application


def test_watch_links_confirmed_names_and_reuses_domain(db):
    job = application(db).job
    company = company_identity.watch(db, "Example", "https://employer.example", aliases=["Example Incorporated"])
    db.flush()
    assert job.company_id == company.id
    assert company_identity.verified_domain_for(db, job) == "employer.example"
    same = company_identity.watch(db, "Example Incorporated", "https://www.employer.example")
    assert same.id == company.id
    assert db.query(Company).count() == 1


def test_unconfirmed_name_does_not_prove_employer_identity(db):
    first, second = application(db).job, application(db).job
    inferred = company_identity.ensure_for_job(db, first)
    db.flush()
    assert company_identity.attach_known_companies(db, [second]) == 0
    assert company_identity.verified_domain_for(db, first) == ""
    assert inferred.identity_source == "job"


def test_new_watch_replaces_shortlist_placeholder_with_confirmed_identity(db):
    job = application(db).job
    placeholder = company_identity.ensure_for_job(db, job)
    db.flush()
    watched = company_identity.watch(db, job.company, "https://employer.example")
    assert watched.id != placeholder.id
    assert job.company_id == watched.id
    assert company_identity.verified_domain_for(db, job) == "employer.example"


def test_active_research_claim_does_not_fetch_twice(db, monkeypatch):
    company = company_identity.watch(db, "Example", "https://employer.example")
    company.research_status = "researching"
    company.last_researched_at = datetime.now(timezone.utc)
    db.commit()
    sniff = Mock(side_effect=AssertionError("duplicate network work"))
    monkeypatch.setattr("app.services.ats_sniffer.sniff_host", sniff)
    assert company_identity.research(db, company, {})["status"] == "researching"
    sniff.assert_not_called()


def test_careers_edit_during_research_rejects_old_results(db, monkeypatch):
    company = company_identity.watch(db, "Example", "https://employer.example")
    db.commit()
    def changed(*args, **kwargs):
        company_identity.watch(db, "Example", "https://employer.example", "https://employer.example/new-careers")
        db.commit()
        return {"greenhouse": ["old-board"]}
    monkeypatch.setattr("app.services.ats_sniffer.sniff_host", changed)
    assert company_identity.research(db, company, {})["status"] == "superseded"
    assert db.query(CompanyBoard).count() == 0
    assert company.research_status == "pending"


def test_ambiguous_confirmed_aliases_remain_unlinked(db):
    company_identity.watch(db, "One", "https://one.example", aliases=["Example"])
    company_identity.watch(db, "Two", "https://two.example", aliases=["Example"])
    job = application(db).job
    assert company_identity.attach_known_companies(db, [job]) == 0
    assert job.company_id is None


@pytest.mark.parametrize("url", ["http://127.0.0.1/careers", "file:///secret", "https://user:password@example.com"])
def test_watch_rejects_non_public_or_credential_urls(db, url):
    with pytest.raises(ValueError):
        company_identity.watch(db, "Example", url)


def test_watch_validation_keeps_form_and_entered_values(client):
    response = client.post("/companies", data={"name": "Example Robotics", "website": "https://jobs.lever.co",
                                              "aliases": "Example R&D"})
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert 'value="Example Robotics"' in response.text
    assert "Example R&amp;D" in response.text
    assert "employer&#39;s own website" in response.text


def test_known_ats_watch_registers_owned_board(db):
    company = company_identity.watch(db, "Example", careers_url="https://boards.greenhouse.io/exampleco")
    report = company_identity.research(db, company, {})
    board = db.query(CompanyBoard).filter_by(ats="greenhouse", slug="exampleco").one()
    assert report["status"] == "ready"
    assert board.company_id == company.id
    assert board.next_due_at is not None


def test_unknown_careers_site_enters_source_learning(db, monkeypatch):
    from app.services import ats_sniffer, source_learning
    sniff = Mock(return_value={})
    monkeypatch.setattr(ats_sniffer, "sniff_host", sniff)
    learn = Mock(return_value={"status": "waiting_browser", "reason": "Open the browser agent to capture this site."})
    monkeypatch.setattr(source_learning, "onboard", learn)
    company = company_identity.watch(db, "Example", "https://employer.example", "https://employer.example/openings")
    result = company_identity.research(db, company, {})
    assert result["status"] == "needs_learning"
    assert sniff.call_args.kwargs["allow_guess"] is False
    learn.assert_called_once_with(db, "https://employer.example/openings")
    assert "browser agent" in company.research_note


def test_saved_interval_changes_next_company_refresh(db):
    profile = {STORE_KEY: {"watched_company_refresh_hours": 2}}
    company = company_identity.watch(db, "Example", careers_url="https://boards.greenhouse.io/exampleco")
    company_identity.research(db, company, profile)
    assert company.next_refresh_at - company.last_researched_at == timedelta(hours=2)


def test_pins_create_watch_and_company_page(client, db, monkeypatch):
    monkeypatch.setattr("app.routers.companies._queue", lambda *args: None)
    response = client.post("/today/pins", data={"companies": "Example\nSecond"})
    assert response.status_code == 200
    assert db.query(Company).filter(Company.watched.is_(True)).count() == 2
    assert client.get("/companies").status_code == 200


def test_star_preparation_is_opt_in_and_uses_saved_setting(client, db, monkeypatch):
    from app.tasks.opportunities import prepare_shortlist
    enqueue = Mock()
    monkeypatch.setattr(prepare_shortlist, "delay", enqueue)
    job = application(db).job
    db.commit()
    client.post(f"/jobs/{job.id}/favourite")
    enqueue.assert_not_called()
    client.post(f"/jobs/{job.id}/favourite")
    profile = db.query(Profile).first()
    if profile is None:
        profile = Profile(data={})
        db.add(profile)
    profile.data = {**(profile.data or {}), STORE_KEY: {"shortlist_prepare_outreach": True}}
    db.commit()
    client.post(f"/jobs/{job.id}/favourite")
    enqueue.assert_called_once_with(str(job.id))


def test_paused_watch_is_not_researched(db, monkeypatch):
    company = company_identity.watch(db, "Example")
    company.watched = False
    db.flush()
    researcher = Mock()
    monkeypatch.setattr(company_identity, "research", researcher)
    assert company_identity.refresh_due(db, {}) == []
    researcher.assert_not_called()


def test_failed_shortlist_preparation_can_retry_and_never_sends(db, monkeypatch):
    from unittest.mock import patch
    from app.tasks import opportunities
    from app.services import outreach
    record = application(db)
    record.job.favourite = True
    record.job.description = "A complete description of this role. " * 40
    db.add(Profile(data={STORE_KEY: {"shortlist_prepare_outreach": True}}))
    db.commit()
    prepare = Mock(side_effect=[RuntimeError("research unavailable"), []])
    monkeypatch.setattr(outreach, "run_outreach", prepare)
    with patch.object(opportunities, "SessionLocal", return_value=db), patch.object(db, "close"):
        failed = opportunities.prepare_shortlist(str(record.job.id))
        assert failed["error"] == "research unavailable"
        assert record.outreach_status == "failed"
        recovered = opportunities.prepare_shortlist(str(record.job.id))
        assert recovered["contacts"] == 0
        assert record.outreach_status == "done"
        assert "skipped" in opportunities.prepare_shortlist(str(record.job.id))
    assert prepare.call_count == 2
    assert prepare.call_args.kwargs == {"draft": True}


def test_interrupted_shortlist_claim_can_be_reclaimed(db, monkeypatch):
    from unittest.mock import patch
    from app.tasks import opportunities
    record = application(db)
    record.job.favourite = True
    record.job.description = "A complete description of this role. " * 40
    record.outreach_status = "discovering"
    record.outreach_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.add(Profile(data={STORE_KEY: {"shortlist_prepare_outreach": True}}))
    db.commit()
    prepare = Mock(return_value=[])
    monkeypatch.setattr("app.services.outreach.run_outreach", prepare)
    with patch.object(opportunities, "SessionLocal", return_value=db), patch.object(db, "close"):
        assert opportunities.prepare_shortlist(str(record.job.id))["contacts"] == 0
    assert record.outreach_status == "done"
