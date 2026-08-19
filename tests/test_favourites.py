"""
Starring a job, and finding it again.

Between "the matcher scored this 82" and "I have written documents for it"
there was nothing. The matched list is hundreds of rows deep and re-sorts
itself every time a pass runs, so a job noticed on Tuesday was genuinely hard
to get back to on Friday.

The tests worth having are about what a star does *not* do. It is orthogonal to
`status` on purpose: starring a filtered job must not re-open it, and filtering
a starred job must not un-star it, because folding the two together would lose
whichever was set second. The one consequence a star does carry is that the
job stops being archivable — which is the case that would otherwise delete the
shortlist sixty days later without anybody noticing.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.job import Job, JobStatus


def make_job(db, **overrides):
    fields = {
        "source": "greenhouse",
        "source_urls": [f"https://x/{uuid.uuid4()}"],
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote",
        "url": f"https://x/{uuid.uuid4()}",
        "description": "d" * 400,
        "status": JobStatus.matched,
        "fetched_at": datetime.now(timezone.utc),
        "dedupe_hash": uuid.uuid4().hex,
    }
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


class TestTheToggle:
    def test_starring_a_job(self, client, db):
        job = make_job(db)
        response = client.post(f"/jobs/{job.id}/favourite")
        db.refresh(job)

        assert response.status_code == 200
        assert job.favourite is True
        assert job.favourited_at is not None

    def test_pressing_it_again_unstars(self, client, db):
        job = make_job(db)
        client.post(f"/jobs/{job.id}/favourite")
        client.post(f"/jobs/{job.id}/favourite")
        db.refresh(job)

        assert job.favourite is False
        # Cleared, not left behind: it is what the shortlist sorts by, and a
        # stale one would order an unstarred job among the starred.
        assert job.favourited_at is None

    def test_it_returns_the_updated_card(self, client, db):
        job = make_job(db)
        body = client.post(f"/jobs/{job.id}/favourite").text

        assert f'id="job-{job.id}"' in body
        assert "&#9733;" in body or "★" in body

    def test_an_unknown_job_is_a_404(self, client, db):
        assert client.post(f"/jobs/{uuid.uuid4()}/favourite").status_code == 404


class TestAStarIsNotAVerdict:
    def test_starring_a_filtered_job_does_not_reopen_it(self, client, db):
        # The clearest disagreement with the matcher there is — and turning it
        # into an override the user did not ask for would make the star unsafe
        # to press on exactly the jobs it matters most for.
        job = make_job(db, status=JobStatus.filtered_out,
                       filter_reason="low_score", filter_detail="scored 45")
        client.post(f"/jobs/{job.id}/favourite")
        db.refresh(job)

        assert job.favourite is True
        assert job.status == JobStatus.filtered_out
        assert job.filter_reason == "low_score"
        assert job.filter_detail == "scored 45"

    def test_filtering_a_starred_job_does_not_unstar_it(self, client, db):
        job = make_job(db)
        client.post(f"/jobs/{job.id}/favourite")
        client.post(f"/jobs/{job.id}/not-interested", data={"scope": "job"})
        db.refresh(job)

        assert job.status == JobStatus.filtered_out
        assert job.favourite is True

    def test_a_rematch_leaves_the_star_alone(self, client, db):
        from unittest.mock import patch

        from app.models.profile import Profile

        db.add(Profile(data={"target_roles": ["Backend Engineer"],
                             "skills": {"lang": ["Python"]},
                             "min_match_score": 60}))
        job = make_job(db)
        db.commit()
        client.post(f"/jobs/{job.id}/favourite")

        reply = {"score": 20, "reasoning": "no", "matched_skills": [],
                 "missing_skills": [], "seniority_fit": False, "scored_by": "x"}
        with patch("app.services.matcher.llm_score_job", return_value=reply), \
             patch("app.llm.providers.deep_matching_provider", return_value=None), \
             patch("app.services.job_details.needs_extraction", return_value=False):
            client.post(f"/jobs/{job.id}/rematch")
        db.refresh(job)

        assert job.status == JobStatus.filtered_out
        assert job.favourite is True


class TestTheFavouritesView:
    def test_it_shows_only_starred_jobs(self, client, db):
        starred = make_job(db, title="Starred Role")
        make_job(db, title="Ignored Role")
        client.post(f"/jobs/{starred.id}/favourite")

        body = client.get("/jobs?favourite=1").text
        assert "Starred Role" in body
        assert "Ignored Role" not in body

    def test_it_shows_a_starred_job_the_matcher_rejected(self, client, db):
        # The whole point. This job is invisible on every other view.
        job = make_job(db, title="Rejected But Wanted",
                       status=JobStatus.filtered_out, filter_reason="low_score")
        client.post(f"/jobs/{job.id}/favourite")

        assert "Rejected But Wanted" in client.get("/jobs?favourite=1").text

    def test_it_sorts_by_when_you_starred_them(self, client, db):
        # Not by score: a shortlist ordered by the number you disagreed with
        # buries the job you starred this morning.
        first = make_job(db, title="Starred First", llm_score=95)
        second = make_job(db, title="Starred Second", llm_score=61)
        client.post(f"/jobs/{first.id}/favourite")
        client.post(f"/jobs/{second.id}/favourite")
        db.refresh(first)
        first.favourited_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()

        body = client.get("/jobs?favourite=1").text
        assert body.index("Starred Second") < body.index("Starred First")

    def test_an_explicit_sort_still_wins(self, client, db):
        job = make_job(db, title="Only One")
        client.post(f"/jobs/{job.id}/favourite")

        body = client.get("/jobs?favourite=1&sort=company_asc").text
        assert "Only One" in body

    def test_the_count_ignores_the_page_filters(self, client, db):
        # A starred job the matcher filtered out is still on the shortlist, and
        # a number that disagreed with the view would be worse than none.
        job = make_job(db, status=JobStatus.filtered_out, filter_reason="low_score")
        client.post(f"/jobs/{job.id}/favourite")

        assert "Favourites (1)" in client.get("/jobs").text

    def test_the_nav_links_to_it(self, client, db):
        assert "/jobs?favourite=1" in client.get("/jobs").text

    def test_the_star_is_on_every_card(self, client, db):
        job = make_job(db)
        assert f'hx-post="/jobs/{job.id}/favourite"' in client.get("/jobs").text

    def test_paging_keeps_you_in_the_shortlist(self, client, db, monkeypatch):
        from app.routers import jobs as jobs_router

        monkeypatch.setattr(jobs_router, "_PAGE_SIZE", 1)
        for n in range(2):
            job = make_job(db, title=f"Starred {n}")
            client.post(f"/jobs/{job.id}/favourite")

        assert "favourite=1" in client.get("/jobs?favourite=1").text


class TestFavouritesAreNeverArchived:
    def _old_rejection(self, db, **overrides):
        return make_job(
            db,
            status=JobStatus.filtered_out,
            filter_reason="low_score",
            fetched_at=datetime.now(timezone.utc) - timedelta(days=120),
            **overrides,
        )

    def test_a_starred_rejection_is_not_a_candidate(self, db):
        from app.services import archive

        self._old_rejection(db, favourite=True,
                            favourited_at=datetime.now(timezone.utc))
        assert archive.candidates(db) == []

    def test_an_unstarred_rejection_still_is(self, db):
        # The guard must be a guard, not an off switch.
        from app.services import archive

        self._old_rejection(db)
        assert len(archive.candidates(db)) == 1

    def test_the_count_agrees_with_what_a_run_would_take(self, db):
        # `remaining` and `candidates` are the same question, and a protection
        # added to one but not the other makes the page disagree with the run.
        from app.services import archive

        self._old_rejection(db, favourite=True,
                            favourited_at=datetime.now(timezone.utc))
        self._old_rejection(db)

        assert archive.remaining(db) == len(archive.candidates(db)) == 1

    def test_an_archive_run_leaves_it_in_place(self, db):
        from app.services import archive

        job = self._old_rejection(db, favourite=True,
                                  favourited_at=datetime.now(timezone.utc))
        archive.archive(db)

        assert db.query(Job).filter(Job.id == job.id).first() is not None
