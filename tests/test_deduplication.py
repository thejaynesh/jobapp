from datetime import datetime, timezone

import pytest

from app.models.job import Job, JobStatus

_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# parse_experience_level tests
# ---------------------------------------------------------------------------

class TestParseExperienceLevel:
    def test_senior_in_title(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Senior Software Engineer", "") == "senior"

    def test_lead_maps_to_senior(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Lead Backend Engineer", "") == "senior"

    def test_principal_maps_to_senior(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Principal Engineer", "") == "senior"

    def test_junior_in_title(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Junior Developer", "") == "entry"

    def test_entry_level_in_description(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Software Engineer", "entry level position") == "entry"

    def test_zero_to_two_years(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("SWE", "0-2 years of experience required") == "entry"

    def test_default_mid(self):
        from app.services.sources.base import parse_experience_level
        assert parse_experience_level("Software Engineer", "Python experience required") == "mid"


# ---------------------------------------------------------------------------
# compute_dedupe_hash tests
# ---------------------------------------------------------------------------

class TestComputeDedupeHash:
    def test_returns_32_char_hex(self):
        from app.services.deduplication import compute_dedupe_hash
        h = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_inputs_same_hash(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        h2 = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        assert h1 == h2

    def test_case_insensitive(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("STRIPE", "SOFTWARE ENGINEER", "NEW YORK, NY")
        h2 = compute_dedupe_hash("stripe", "software engineer", "new york, ny")
        assert h1 == h2

    def test_punctuation_normalized(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe, Inc.", "Software Engineer", "New York")
        h2 = compute_dedupe_hash("Stripe Inc", "Software Engineer", "New York")
        assert h1 == h2

    def test_different_company_different_hash(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe", "Software Engineer", "New York")
        h2 = compute_dedupe_hash("Airbnb", "Software Engineer", "New York")
        assert h1 != h2


# ---------------------------------------------------------------------------
# find_existing_job + merge_or_skip tests
# ---------------------------------------------------------------------------

def _make_job(db, *, company="ACME", title="SWE", location="NYC",
              url="https://ex.com/1", source="adzuna", source_job_id="AZ1",
              dedupe_hash="aabbccdd11223344aabbccdd11223344") -> Job:
    job = Job(
        source=source,
        source_job_id=source_job_id,
        source_urls=[url],
        title=title,
        company=company,
        location=location,
        is_remote=False,
        url=url,
        description="A great job.",
        experience_level="mid",
        status=JobStatus.new,
        fetched_at=_NOW,
        dedupe_hash=dedupe_hash,
    )
    db.add(job)
    db.flush()
    return job


class TestFindExistingJob:
    def test_layer1_url_match(self, db):
        from app.services.deduplication import find_existing_job
        job = _make_job(db, url="https://ex.com/1", dedupe_hash="a" * 32)
        result = find_existing_job(db, source="adzuna", url="https://ex.com/1",
                                   source_job_id=None, dedupe_hash="x" * 32)
        assert result is not None
        assert result.id == job.id

    def test_layer2_source_job_id_match(self, db):
        from app.services.deduplication import find_existing_job
        job = _make_job(db, url="https://ex.com/2", source_job_id="JOBID42",
                        dedupe_hash="b" * 32)
        result = find_existing_job(db, source="adzuna", url="https://other.com/999",
                                   source_job_id="JOBID42", dedupe_hash="y" * 32)
        assert result is not None
        assert result.id == job.id

    def test_layer3_dedupe_hash_match(self, db):
        from app.services.deduplication import find_existing_job
        job = _make_job(db, url="https://ex.com/3", source_job_id="ORIG",
                        dedupe_hash="c" * 32)
        result = find_existing_job(db, source="indeed", url="https://indeed.com/999",
                                   source_job_id="DIFF", dedupe_hash="c" * 32)
        assert result is not None
        assert result.id == job.id

    def test_no_match_returns_none(self, db):
        from app.services.deduplication import find_existing_job
        result = find_existing_job(db, source="adzuna", url="https://new.com/1",
                                   source_job_id="NEWID", dedupe_hash="d" * 32)
        assert result is None

    def test_layer2_skipped_when_source_job_id_none(self, db):
        from app.services.deduplication import find_existing_job
        _make_job(db, url="https://ex.com/4", source_job_id="REALID",
                  dedupe_hash="e" * 32)
        result = find_existing_job(db, source="adzuna", url="https://other.com/5",
                                   source_job_id=None, dedupe_hash="f" * 32)
        assert result is None


class TestMergeOrSkip:
    def test_new_url_appended_to_source_urls(self, db):
        from app.services.deduplication import merge_or_skip
        job = _make_job(db, url="https://ex.com/original", dedupe_hash="f1" * 16)
        merge_or_skip(db, job, new_url="https://crosspost.com/job1",
                      new_description="Short desc.", layer=3)
        db.flush()
        assert "https://crosspost.com/job1" in job.source_urls

    def test_longer_description_replaces_shorter(self, db):
        from app.services.deduplication import merge_or_skip
        job = _make_job(db, url="https://ex.com/a", dedupe_hash="f2" * 16)
        job.description = "Short."
        db.flush()
        merge_or_skip(db, job, new_url="https://new.com/b",
                      new_description="Much longer description with lots of details.",
                      layer=3)
        db.flush()
        assert job.description == "Much longer description with lots of details."

    def test_shorter_description_not_replaced(self, db):
        from app.services.deduplication import merge_or_skip
        job = _make_job(db, url="https://ex.com/c", dedupe_hash="f3" * 16)
        job.description = "A very long existing description with lots of content."
        db.flush()
        merge_or_skip(db, job, new_url="https://new.com/d",
                      new_description="Short.",
                      layer=3)
        db.flush()
        assert "very long" in job.description
