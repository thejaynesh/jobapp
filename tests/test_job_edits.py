"""
Correcting a job by hand, and the promise that nothing takes it back.

The feature is a textarea. Almost every test here is about the promise instead,
because that is the part that can fail silently: an edit that gets overwritten
by the next enrichment pass looks exactly like an edit that never saved, and
the user finds out days later when the documents come out wrong.

So there is one test per automatic writer — enrichment, the detail extractor,
cross-post merges, the browser harvest, link resolution — each asserting that a
locked field survives the write that would previously have replaced it.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.job import Job, JobStatus
from app.services import job_edits
from app.services.job_edits import EditError

# Stripped: an edit is stored as typed apart from surrounding whitespace, so a
# constant with a trailing space would fail on the trim rather than the point.
REAL_POSTING = ("The actual posting text, pasted off the page. " * 12).strip()
BOILERPLATE = ("Cookie notice and site navigation boilerplate. " * 40).strip()


def make_job(db, **overrides):
    fields = {
        "source": "adzuna",
        "source_urls": [f"https://x/{uuid.uuid4()}"],
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote",
        "url": f"https://x/{uuid.uuid4()}",
        "description": "A thin teaser. " * 5,
        "status": JobStatus.new,
        "fetched_at": datetime.now(timezone.utc),
        "dedupe_hash": uuid.uuid4().hex,
    }
    fields.update(overrides)
    job = Job(**fields)
    db.add(job)
    db.commit()
    return job


class TestMakingAnEdit:
    def test_it_stores_what_was_typed(self, db):
        job = make_job(db)
        job_edits.apply(db, job, {"description": REAL_POSTING})
        db.commit()

        assert job.description == REAL_POSTING

    def test_it_locks_only_what_changed(self, db):
        # The form posts every field. Saving a description edit must not also
        # freeze the title against every future correction — the user did not
        # edit it, they just did not delete it.
        job = make_job(db)
        job_edits.apply(db, job, {
            "description": REAL_POSTING,
            "title": "Backend Engineer",   # unchanged
            "company": "Acme",             # unchanged
        })

        assert job.manual_fields == ["description"]

    def test_it_records_when_a_person_touched_the_row(self, db):
        job = make_job(db)
        assert job.edited_at is None

        job_edits.apply(db, job, {"location": "Berlin"})
        assert job.edited_at is not None

    def test_a_no_op_save_changes_nothing(self, db):
        job = make_job(db)
        outcome = job_edits.apply(db, job, {"title": "Backend Engineer"})

        assert outcome["changed"] == []
        assert job.manual_fields == []
        assert job.edited_at is None

    def test_several_fields_at_once(self, db):
        job = make_job(db)
        job_edits.apply(db, job, {
            "description": REAL_POSTING,
            "location": "London",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "salary_min": "120000",
        })

        assert set(job.manual_fields) == {
            "description", "location", "apply_url", "salary_min",
        }
        assert job.salary_min == 120000


class TestValidation:
    def test_a_bad_link_is_refused(self, db):
        job = make_job(db)
        with pytest.raises(EditError, match="http"):
            job_edits.apply(db, job, {"apply_url": "greenhouse.io/acme"})

    def test_a_bad_salary_is_refused(self, db):
        job = make_job(db)
        with pytest.raises(EditError, match="not a number"):
            job_edits.apply(db, job, {"salary_min": "about 120k"})

    def test_nothing_is_saved_when_one_field_is_bad(self, db):
        # Validation runs over the whole form before anything is applied, so a
        # typo in the salary cannot leave a half-written description behind.
        job = make_job(db)
        original = job.description

        with pytest.raises(EditError):
            job_edits.apply(db, job, {
                "description": REAL_POSTING, "salary_max": "lots",
            })

        assert job.description == original
        assert job.manual_fields == []

    def test_a_title_cannot_be_emptied(self, db):
        job = make_job(db)
        with pytest.raises(EditError):
            job_edits.apply(db, job, {"title": "   "})

    def test_an_unknown_field_is_ignored(self, db):
        job = make_job(db)
        job_edits.apply(db, job, {"dedupe_hash": "hijacked", "location": "Oslo"})

        assert job.dedupe_hash != "hijacked"
        assert job.location == "Oslo"

    def test_the_url_is_not_editable(self, db):
        # It is the key three quarters of deduplication is built on. Correcting
        # a typo in it would detach the job from its own cross-posts.
        assert "url" not in job_edits.EDITABLE
        assert "apply_url" in job_edits.EDITABLE
        assert "dedupe_hash" not in job_edits.EDITABLE


class TestPastedText:
    def test_plain_text_arrives_exactly_as_typed(self, db):
        # `descriptions.clean` normalises source soup. A paste is not soup, and
        # a description that came back subtly different from what was pasted
        # would be the first thing to distrust about this feature.
        job = make_job(db)
        typed = "Line one.\n\n  Indented line two.\n\nR&D team."
        job_edits.apply(db, job, {"description": typed})

        assert job.description == typed

    def test_pasted_markup_is_cleaned(self, db):
        job = make_job(db)
        job_edits.apply(db, job, {
            "description": "<p>We need a <strong>backend</strong> engineer.</p>",
        })

        assert "<p>" not in job.description
        assert "backend" in job.description

    def test_a_paste_that_quotes_a_block_page_is_kept(self, db):
        # `clean` returns "" for anything it reads as a challenge page, which
        # is right for a scrape and catastrophic for a paste: the user would
        # press save and watch their text vanish with no error.
        job = make_job(db)
        text = "<p>Access denied to unauthorised systems is a security rule here.</p>"
        job_edits.apply(db, job, {"description": text})

        assert job.description
        assert "security rule" in job.description


class TestWhatAnEditedDescriptionTriggers:
    def test_it_stamps_the_description_as_updated(self, db):
        # Documents written against the old text are stale whether a scraper or
        # a person replaced it.
        job = make_job(db)
        job_edits.apply(db, job, {"description": REAL_POSTING})

        assert job.description_updated_at is not None

    def test_a_job_filtered_for_no_description_goes_back_in_the_queue(self, db):
        job = make_job(db, status=JobStatus.filtered_out,
                       filter_reason="no_description", description=None)
        outcome = job_edits.apply(db, job, {"description": REAL_POSTING})

        assert outcome["requeued"] is True
        assert job.status == JobStatus.new
        assert job.filter_reason is None

    def test_a_job_the_user_filtered_by_hand_stays_filtered(self, db):
        # Editing the description of a job you rejected is not the same as
        # changing your mind about it. Re-match is one click away for that.
        job = make_job(db, status=JobStatus.filtered_out, filter_reason="manual")
        outcome = job_edits.apply(db, job, {"description": REAL_POSTING})

        assert outcome["requeued"] is False
        assert job.status == JobStatus.filtered_out

    def test_editing_the_location_does_not_requeue(self, db):
        job = make_job(db, status=JobStatus.filtered_out,
                       filter_reason="no_description")
        outcome = job_edits.apply(db, job, {"location": "Oslo"})

        assert outcome["requeued"] is False

    def test_the_dedupe_hash_is_left_alone(self, db):
        # It is how a cross-post arriving tomorrow recognises this row.
        job = make_job(db)
        before = job.dedupe_hash
        job_edits.apply(db, job, {"title": "Senior Backend Engineer",
                                  "company": "Acme Corp"})

        assert job.dedupe_hash == before


class TestNothingAutomaticOverwritesAnEdit:
    """One test per writer that used to be allowed to."""

    def _edited(self, db, **overrides):
        job = make_job(db, **overrides)
        job_edits.apply(db, job, {"description": REAL_POSTING})
        db.commit()
        return job

    def test_enrichment_leaves_it_alone(self, db):
        from app.services.enrichment import Extraction, apply_extraction

        job = self._edited(db)
        outcome = apply_extraction(
            db, job, Extraction(description=BOILERPLATE, method="json-ld")
        )

        assert outcome["improved"] is False
        assert job.description == REAL_POSTING

    def test_enrichment_still_works_on_an_untouched_job(self, db):
        # The guard must be a guard, not an off switch.
        from app.services.enrichment import Extraction, apply_extraction

        job = make_job(db)
        outcome = apply_extraction(
            db, job, Extraction(description=BOILERPLATE, method="json-ld")
        )

        assert outcome["improved"] is True

    def test_a_cross_post_merge_leaves_it_alone(self, db):
        from app.services.deduplication import merge_or_skip

        job = self._edited(db)
        merge_or_skip(db, job, "https://elsewhere/1", BOILERPLATE, layer=3)

        assert job.description == REAL_POSTING
        # The second listing is still real and still worth recording.
        assert "https://elsewhere/1" in job.source_urls

    def test_the_harvest_leaves_it_alone(self, db):
        from app.services.harvest import save_harvested_jobs

        job = self._edited(db, source="linkedin_harvest", source_job_id="998877")
        counts = save_harvested_jobs(db, [{
            "source": "linkedin_harvest", "source_job_id": "998877",
            "url": job.url, "title": job.title, "company": job.company,
            "location": job.location, "description": BOILERPLATE,
        }])
        db.refresh(job)

        assert job.description == REAL_POSTING
        assert counts["merged"] == 0

    def test_the_detail_extractor_leaves_a_typed_salary_alone(self, db):
        # This runs on every description change, so without the check one pass
        # would quietly undo a figure the user typed after reading the posting.
        from app.services import job_details

        job = make_job(db)
        job_edits.apply(db, job, {"salary_min": "150000"})
        db.commit()

        job_details.apply(job, {"salary_min": 90000.0, "required_years": 5.0})

        assert job.salary_min == 150000
        # Fields the user never touched are still written.
        assert job.required_years == 5.0

    def test_link_resolution_leaves_a_typed_apply_url_alone(self, db):
        from app.services.link_resolver import retarget_tracker_links

        job = make_job(db, apply_url="https://click.appcast.io/x1")
        job_edits.apply(db, job, {"apply_url": "https://click.appcast.io/x1"})
        job.manual_fields = ["apply_url"]
        db.commit()

        retarget_tracker_links(db)
        db.refresh(job)

        assert job.apply_url == "https://click.appcast.io/x1"


class TestReleasingAField:
    def test_a_released_field_is_managed_again(self, db):
        from app.services.enrichment import Extraction, apply_extraction

        job = make_job(db)
        job_edits.apply(db, job, {"description": REAL_POSTING})
        job_edits.apply(db, job, {}, release_fields=["description"])
        db.commit()

        assert job.manual_fields == []
        assert apply_extraction(
            db, job, Extraction(description=BOILERPLATE, method="json-ld")
        )["improved"] is True

    def test_releasing_one_field_keeps_the_others(self, db):
        job = make_job(db)
        job_edits.apply(db, job, {"description": REAL_POSTING, "location": "Oslo"})
        job_edits.apply(db, job, {}, release_fields=["location"])

        assert job.manual_fields == ["description"]

    def test_releasing_something_unlocked_is_harmless(self, db):
        job = make_job(db)
        outcome = job_edits.apply(db, job, {}, release_fields=["salary_min", "nonsense"])

        assert outcome["released"] == []


class TestThePage:
    def test_the_form_renders(self, client, db):
        job = make_job(db)
        body = client.get(f"/jobs/{job.id}/edit").text

        assert 'name="description"' in body
        assert job.title in body

    def test_saving_redirects_back_where_you_came_from(self, client, db):
        job = make_job(db)
        response = client.post(
            f"/jobs/{job.id}/edit",
            data={"description": REAL_POSTING, "next": "/jobs"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/jobs"
        db.refresh(job)
        assert job.description == REAL_POSTING

    def test_it_will_not_redirect_off_the_site(self, client, db):
        job = make_job(db)
        response = client.post(
            f"/jobs/{job.id}/edit",
            data={"description": REAL_POSTING, "next": "https://evil.example/"},
            follow_redirects=False,
        )

        assert response.headers["location"] == f"/jobs/{job.id}/application"

    def test_an_unchecked_box_turns_the_flag_off(self, client, db):
        # An unchecked checkbox posts nothing at all, which would otherwise be
        # indistinguishable from "this field was not on the form".
        job = make_job(db, is_remote=True)
        client.post(f"/jobs/{job.id}/edit", data={"_checkbox": "is_remote"})
        db.refresh(job)

        assert job.is_remote is False
        assert "is_remote" in job.manual_fields

    def test_a_bad_value_comes_back_as_the_form_with_a_message(self, client, db):
        job = make_job(db)
        response = client.post(
            f"/jobs/{job.id}/edit", data={"salary_min": "loads"}
        )

        assert response.status_code == 422
        assert "not a number" in response.text
        db.refresh(job)
        assert job.manual_fields == []

    def test_an_unknown_job_is_a_404(self, client, db):
        assert client.get(f"/jobs/{uuid.uuid4()}/edit").status_code == 404
        assert client.post(f"/jobs/{uuid.uuid4()}/edit", data={}).status_code == 404

    def test_the_card_links_to_it(self, client, db):
        job = make_job(db)
        assert f"/jobs/{job.id}/edit" in client.get("/jobs").text

    def test_the_application_page_offers_it(self, client, db):
        job = make_job(db)
        response = client.get(f"/jobs/{job.id}/application", follow_redirects=True)

        assert f"/jobs/{job.id}/edit" in response.text
        # And says how much text there is, which is the fastest read on whether
        # this is the real posting or a teaser.
        assert "characters" in response.text
