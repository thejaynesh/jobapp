"""
The "not interested" menu: a correction that teaches.

The scope is always the user's explicit choice — hide one job, exclude the
company, or block a title word they picked — because a guessed-at blanket rule
is how a feed loses jobs nobody meant to lose.
"""

from datetime import datetime, timezone

import pytest

from app.models.job import Job, JobStatus
from app.models.profile import Profile

_NOW = datetime.now(timezone.utc)


def _make_job(db, suffix, *, title="Backend Engineer", company="Acme"):
    job = Job(
        source="adzuna",
        title=title,
        company=company,
        location="NYC",
        url=f"https://ex.com/ni/{suffix}",
        description="A job.",
        experience_level="mid",
        status=JobStatus.matched,
        fetched_at=_NOW,
        dedupe_hash=f"ni-{suffix}",
    )
    db.add(job)
    db.flush()
    return job


def _make_profile(db):
    profile = Profile(data={
        "target_roles": ["Backend Engineer"],
        "excluded_companies": [],
    })
    db.add(profile)
    db.commit()
    return profile


class TestHideJustThisJob:
    def test_the_job_is_filtered_manually(self, client, db):
        _make_profile(db)
        job = _make_job(db, "j1")
        db.commit()
        response = client.post(f"/jobs/{job.id}/not-interested", data={"scope": "job"})
        assert response.status_code == 200
        db.refresh(job)
        assert job.status == JobStatus.filtered_out
        assert job.filter_reason == "manual"


class TestExcludeCompany:
    def test_the_company_lands_on_the_profile(self, client, db):
        profile = _make_profile(db)
        job = _make_job(db, "c1", company="SpamCorp")
        db.commit()
        response = client.post(
            f"/jobs/{job.id}/not-interested", data={"scope": "company"})
        assert response.status_code == 200
        db.refresh(job)
        db.refresh(profile)
        assert job.filter_reason == "excluded_company"
        assert "SpamCorp" in profile.data["excluded_companies"]

    def test_excluding_twice_stores_it_once(self, client, db):
        profile = _make_profile(db)
        first = _make_job(db, "c2", company="SpamCorp")
        second = _make_job(db, "c3", company="SpamCorp")
        db.commit()
        client.post(f"/jobs/{first.id}/not-interested", data={"scope": "company"})
        client.post(f"/jobs/{second.id}/not-interested", data={"scope": "company"})
        db.refresh(profile)
        assert profile.data["excluded_companies"].count("SpamCorp") == 1

    def test_future_matching_actually_honours_it(self, client, db):
        # The whole point: the correction feeds the filter that runs next cycle.
        from app.services.matcher import evaluate_keyword_filter

        profile = _make_profile(db)
        job = _make_job(db, "c4", company="SpamCorp")
        db.commit()
        client.post(f"/jobs/{job.id}/not-interested", data={"scope": "company"})
        db.refresh(profile)

        newcomer = _make_job(db, "c5", company="SpamCorp")
        outcome = evaluate_keyword_filter(newcomer, profile.data)
        assert outcome.reason == "excluded_company"


class TestBlockTitleWord:
    def test_the_word_lands_on_the_profile(self, client, db):
        profile = _make_profile(db)
        job = _make_job(db, "w1", title="Embedded Backend Engineer")
        db.commit()
        response = client.post(
            f"/jobs/{job.id}/not-interested",
            data={"scope": "title_word", "word": "Embedded"},
        )
        assert response.status_code == 200
        db.refresh(job)
        db.refresh(profile)
        assert job.filter_reason == "blocked_title"
        assert "Embedded" in profile.data["blocked_title_words"]

    def test_a_word_not_in_this_title_is_refused(self, client, db):
        # The button list is the interface; a stray word would silently block
        # half the feed.
        _make_profile(db)
        job = _make_job(db, "w2", title="Backend Engineer")
        db.commit()
        response = client.post(
            f"/jobs/{job.id}/not-interested",
            data={"scope": "title_word", "word": "Manager"},
        )
        assert response.status_code == 422

    def test_future_matching_honours_the_blocked_word(self, client, db):
        from app.services.matcher import evaluate_keyword_filter

        profile = _make_profile(db)
        job = _make_job(db, "w3", title="Embedded Backend Engineer")
        db.commit()
        client.post(
            f"/jobs/{job.id}/not-interested",
            data={"scope": "title_word", "word": "Embedded"},
        )
        db.refresh(profile)

        newcomer = _make_job(db, "w4", title="Embedded Systems Engineer")
        outcome = evaluate_keyword_filter(newcomer, profile.data)
        assert outcome.reason == "blocked_title"


class TestGuardRails:
    def test_an_unknown_scope_is_refused(self, client, db):
        _make_profile(db)
        job = _make_job(db, "g1")
        db.commit()
        response = client.post(
            f"/jobs/{job.id}/not-interested", data={"scope": "everything"})
        assert response.status_code == 422

    def test_a_missing_job_is_a_404(self, client, db):
        _make_profile(db)
        response = client.post(
            "/jobs/00000000-0000-0000-0000-000000000000/not-interested",
            data={"scope": "job"},
        )
        assert response.status_code == 404

    def test_the_word_options_come_off_the_title(self, db):
        job = _make_job(db, "g2", title="Embedded Software Manager (Remote)")
        # Meaningful words only: "Remote" is noise, the rest are candidates.
        words = job.title_block_candidates
        assert "Embedded" in words
        assert "Manager" in words
        assert "Remote" not in words
