"""
Recognising the posting someone is looking at.

One job has many URLs — the aggregator redirect, the employer link that redirect
resolved to, and whatever the address bar holds after three tracking parameters.
Lookup has to see through that, and has to stop short of guessing: a missed
badge is a small loss, and the wrong job's score on screen is worse than none.
"""

from datetime import datetime, timezone

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.services import job_context
from app.services.deduplication import compute_dedupe_hash

LINKEDIN = "https://www.linkedin.com/jobs/view/3901234567/"


def make_job(db, **kwargs):
    url = kwargs.pop("url", LINKEDIN)
    title = kwargs.pop("title", "Senior Backend Engineer")
    company = kwargs.pop("company", "Acme Corp")
    location = kwargs.pop("location", "Boston, MA")
    job = Job(
        source=kwargs.pop("source", "linkedin"),
        source_urls=kwargs.pop("source_urls", [url]),
        url=url,
        title=title,
        company=company,
        location=location,
        description=kwargs.pop("description", "A job."),
        dedupe_hash=kwargs.pop(
            "dedupe_hash", compute_dedupe_hash(company, title, location)
        ),
        fetched_at=datetime.now(timezone.utc),
        **kwargs,
    )
    db.add(job)
    db.commit()
    return job


class TestUrlVariants:
    def test_keeps_the_original_first(self):
        assert job_context.url_variants(LINKEDIN)[0] == LINKEDIN

    def test_offers_the_url_without_tracking(self):
        noisy = f"{LINKEDIN}?refId=abc&trackingId=xyz"
        assert LINKEDIN in job_context.url_variants(noisy)

    def test_rebuilds_linkedins_canonical_form_from_a_search_url(self):
        # On a search page the id is a query parameter and the path is not the
        # posting at all.
        search = "https://www.linkedin.com/jobs/search/?currentJobId=3901234567&f_TPR=r86400"
        assert LINKEDIN in job_context.url_variants(search)

    def test_an_empty_url_has_no_variants(self):
        assert job_context.url_variants("") == []
        assert job_context.url_variants(None) == []


class TestFindJob:
    def test_finds_by_exact_url(self, db):
        job = make_job(db)
        assert job_context.find_job(db, LINKEDIN).id == job.id

    def test_finds_through_tracking_parameters(self, db):
        job = make_job(db)
        found = job_context.find_job(db, f"{LINKEDIN}?refId=abc&trk=public")
        assert found is not None and found.id == job.id

    def test_finds_from_a_search_page_url(self, db):
        job = make_job(db)
        found = job_context.find_job(
            db, "https://www.linkedin.com/jobs/search/?currentJobId=3901234567"
        )
        assert found is not None and found.id == job.id

    def test_finds_by_the_resolved_apply_url(self, db):
        # The user is on Greenhouse; we stored the Adzuna link it came from.
        employer = "https://boards.greenhouse.io/acme/jobs/4001"
        job = make_job(db, url="https://www.adzuna.com/land/ad/1", apply_url=employer)
        found = job_context.find_job(db, employer)
        assert found is not None and found.id == job.id

    def test_finds_by_a_url_the_job_was_previously_seen_at(self, db):
        job = make_job(db, url="https://a.example/1", source_urls=["https://a.example/1", "https://b.example/2"])
        found = job_context.find_job(db, "https://b.example/2")
        assert found is not None and found.id == job.id

    def test_an_unrelated_url_finds_nothing(self, db):
        make_job(db)
        assert job_context.find_job(db, "https://example.com/something-else") is None

    def test_an_empty_url_finds_nothing(self, db):
        make_job(db)
        assert job_context.find_job(db, "") is None


class TestContext:
    def test_reports_an_unknown_posting_plainly(self, db):
        assert job_context.context(db, "https://example.com/job/1") == {"known": False}

    def test_reports_the_score(self, db):
        make_job(db, llm_score=82, matched_by="llm")
        data = job_context.context(db, LINKEDIN)
        assert data["known"] is True
        assert data["job"]["score"] == 82
        assert data["job"]["matched_by"] == "llm"

    def test_falls_back_to_the_keyword_score(self, db):
        make_job(db, keyword_score=44)
        assert job_context.context(db, LINKEDIN)["job"]["score"] == 44

    def test_an_unscored_job_reports_no_score_rather_than_zero(self, db):
        # Zero would read as "scored badly" rather than "not scored yet".
        make_job(db)
        assert job_context.context(db, LINKEDIN)["job"]["score"] is None

    def test_carries_the_filter_reason(self, db):
        make_job(
            db,
            status=JobStatus.filtered_out,
            filter_reason="restricted",
            filter_detail="US citizenship required.",
        )
        data = job_context.context(db, LINKEDIN)["job"]
        assert data["filter_reason"] == "restricted"
        assert "citizenship" in data["filter_detail"]

    def test_carries_the_sponsorship_note(self, db):
        make_job(
            db,
            sponsorship_direction="no_sponsorship",
            sponsorship_note="We cannot sponsor visas.",
        )
        data = job_context.context(db, LINKEDIN)["job"]
        assert data["sponsorship_direction"] == "no_sponsorship"
        assert "sponsor" in data["sponsorship_note"]

    def test_reports_that_you_already_applied(self, db):
        job = make_job(db)
        db.add(Application(job_id=job.id, status=ApplicationStatus.applied))
        db.commit()
        assert job_context.context(db, LINKEDIN)["application"]["status"] == "applied"

    def test_no_application_reads_as_none_not_missing(self, db):
        make_job(db)
        assert job_context.context(db, LINKEDIN)["application"] is None


class TestPrepare:
    def test_opens_an_application_for_a_known_job(self, db):
        job = make_job(db)
        result = job_context.prepare(db, LINKEDIN)
        assert result["ok"] is True
        assert result["job_id"] == str(job.id)
        assert result["created_application"] is True

    def test_reuses_an_application_that_already_exists(self, db):
        job = make_job(db)
        db.add(Application(job_id=job.id))
        db.commit()
        result = job_context.prepare(db, LINKEDIN)
        assert result["created_application"] is False

    def test_stores_a_posting_the_pipeline_never_fetched(self, db):
        result = job_context.prepare(
            db,
            "https://careers.example.com/jobs/99",
            {"title": "Platform Engineer", "company": "Example Inc", "description": "..."},
        )
        assert result["ok"] is True
        assert db.query(Job).filter(Job.company == "Example Inc").count() == 1

    def test_refuses_a_posting_with_nothing_to_match_on(self, db):
        # A row with no title or company cannot be matched or written for; a
        # placeholder would be worse than declining.
        result = job_context.prepare(db, "https://careers.example.com/jobs/99", {})
        assert result["ok"] is False
        assert db.query(Job).count() == 0
