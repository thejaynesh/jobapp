import uuid
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


# ---------------------------------------------------------------------------
# Normalization v2 — cross-aggregator variants must collide
# ---------------------------------------------------------------------------

class TestNormalizationV2:
    def test_company_legal_suffixes_collapse(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe, Inc.", "Software Engineer", "New York, NY")
        h2 = compute_dedupe_hash("Stripe", "Software Engineer", "New York, NY")
        h3 = compute_dedupe_hash("Stripe Inc", "Software Engineer", "New York, NY")
        assert h1 == h2 == h3

    def test_title_abbreviations_collapse(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Acme", "Sr. Software Engineer", "Austin, TX")
        h2 = compute_dedupe_hash("Acme", "Senior Software Engineer", "Austin, TX")
        assert h1 == h2

    def test_title_mode_tags_collapse(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Acme", "Backend Engineer (Remote)", "Toronto")
        h2 = compute_dedupe_hash("Acme", "Backend Engineer", "Toronto")
        assert h1 == h2

    def test_location_country_suffix_collapses(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Acme", "SWE", "San Francisco, CA, United States")
        h2 = compute_dedupe_hash("Acme", "SWE", "San Francisco, CA")
        h3 = compute_dedupe_hash("Acme", "SWE", "San Francisco")
        assert h1 == h2 == h3

    def test_remote_variants_collapse(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Acme", "SWE", "Remote (US)")
        h2 = compute_dedupe_hash("Acme", "SWE", "Remote - Worldwide")
        h3 = compute_dedupe_hash("Acme", "SWE", "Work from home")
        assert h1 == h2 == h3

    def test_different_cities_stay_distinct(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe", "Backend Engineer", "New York, NY")
        h2 = compute_dedupe_hash("Stripe", "Backend Engineer", "Toronto, Canada")
        assert h1 != h2

    def test_different_titles_stay_distinct(self):
        from app.services.deduplication import compute_dedupe_hash
        h1 = compute_dedupe_hash("Stripe", "Backend Engineer", "NYC")
        h2 = compute_dedupe_hash("Stripe", "Frontend Engineer", "NYC")
        assert h1 != h2

    def test_single_word_suffix_company_survives(self):
        from app.services.deduplication import normalize_company
        # a company literally named "Co" must not normalize to empty
        assert normalize_company("Co") == "co"


# ---------------------------------------------------------------------------
# Cross-post duplicate application guard
# ---------------------------------------------------------------------------

class TestFindDuplicateApplicationJob:
    """
    The dedupe hash catches exact-normalized cross-posts at fetch time; this
    catches the near-misses at application time, because each one that slips
    through used to cost a full duplicate document generation.
    """

    def _with_application(self, db, **kwargs):
        from app.models.application import Application

        job = _make_job(db, **kwargs)
        db.add(Application(job_id=job.id))
        db.flush()
        return job

    def test_a_cosmetic_title_difference_is_caught(self, db):
        from app.services.deduplication import find_duplicate_application_job

        original = self._with_application(
            db, company="Stripe", title="Backend Engineer",
            url="https://ex.com/dup1", source_job_id="D1", dedupe_hash="d" * 32,
        )
        newcomer = _make_job(
            db, company="Stripe, Inc.", title="Backend Engineer - Remote",
            url="https://ex.com/dup2", source_job_id="D2", dedupe_hash="e" * 32,
        )
        found = find_duplicate_application_job(db, newcomer)
        assert found is not None
        assert found.id == original.id

    def test_a_genuinely_different_role_is_not_a_duplicate(self, db):
        from app.services.deduplication import find_duplicate_application_job

        self._with_application(
            db, company="Stripe", title="Backend Engineer",
            url="https://ex.com/dup3", source_job_id="D3", dedupe_hash="f" * 32,
        )
        newcomer = _make_job(
            db, company="Stripe", title="Data Scientist, Payments",
            url="https://ex.com/dup4", source_job_id="D4", dedupe_hash="1" * 32,
        )
        assert find_duplicate_application_job(db, newcomer) is None

    def test_same_title_at_another_company_is_not_a_duplicate(self, db):
        from app.services.deduplication import find_duplicate_application_job

        self._with_application(
            db, company="Stripe", title="Backend Engineer",
            url="https://ex.com/dup5", source_job_id="D5", dedupe_hash="2" * 32,
        )
        newcomer = _make_job(
            db, company="Square", title="Backend Engineer",
            url="https://ex.com/dup6", source_job_id="D6", dedupe_hash="3" * 32,
        )
        assert find_duplicate_application_job(db, newcomer) is None

    def test_a_job_without_an_application_does_not_block_anything(self, db):
        from app.services.deduplication import find_duplicate_application_job

        _make_job(
            db, company="Stripe", title="Backend Engineer",
            url="https://ex.com/dup7", source_job_id="D7", dedupe_hash="4" * 32,
        )
        newcomer = _make_job(
            db, company="Stripe", title="Backend Engineer - Remote",
            url="https://ex.com/dup8", source_job_id="D8", dedupe_hash="5" * 32,
        )
        assert find_duplicate_application_job(db, newcomer) is None


# ---------------------------------------------------------------------------
# The "description got meaningfully fuller" stamp
# ---------------------------------------------------------------------------

class TestDescriptionGrowthStamp:
    def test_a_much_fuller_description_stamps_the_job(self, db):
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/g1", source_job_id="G1",
                        dedupe_hash="6" * 32)
        merge_or_skip(db, job, "https://ex.com/g1b", "long description " * 50, layer=3)
        assert job.description_updated_at is not None

    def test_a_marginally_longer_description_does_not(self, db):
        # A few extra characters isn't new grounding; stamping it would nag
        # about rewriting documents for nothing.
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/g2", source_job_id="G2",
                        dedupe_hash="7" * 32)
        merge_or_skip(db, job, "https://ex.com/g2b", "A greater job.", layer=3)
        assert job.description_updated_at is None


# ---------------------------------------------------------------------------
# Taking the better half of two sightings
# ---------------------------------------------------------------------------

class TestASecondSightingFillsInWhatIsMissing:
    """
    The same posting reaches us from several places, each missing something
    another one has.

    LinkedIn's guest API knows the title and almost never the pay; the
    employer's own board knows the pay, the employment type and the day it went
    up; an aggregator card knows the direct apply link LinkedIn buries behind a
    redirect. Which arrived first is an accident of scheduling, so the second
    sighting is not a duplicate to discard — it is the rest of the posting.
    """

    def enrich(self, db, data, **job_kwargs):
        from app.services.deduplication import enrich_from

        job = _make_job(db, dedupe_hash=uuid.uuid4().hex[:32],
                        url=f"https://ex.com/{uuid.uuid4().hex[:8]}",
                        **job_kwargs)
        return job, enrich_from(job, data)

    def test_a_stated_pay_band_is_taken(self, db):
        job, filled = self.enrich(db, {"salary_min": 120000, "salary_max": 160000,
                                       "salary_currency": "USD"})
        assert (job.salary_min, job.salary_max, job.salary_currency) == (
            120000, 160000, "USD")
        assert "salary" in filled

    def test_a_pay_band_never_replaces_one_we_have(self, db):
        # The detail extractor reads pay out of the description with a model
        # told never to guess. A card is a summary of the same posting.
        job, filled = self.enrich(db, {"salary_min": 90000})
        job.salary_min = 120000
        from app.services.deduplication import enrich_from
        assert enrich_from(job, {"salary_min": 90000, "salary_max": 95000}) == []
        assert job.salary_min == 120000

    def test_half_a_band_does_not_join_the_other_source_s_half(self, db):
        # A minimum from one source and a maximum from another is not a band
        # anybody stated, and the salary filter would drop jobs on it.
        from app.services.deduplication import enrich_from

        job, _ = self.enrich(db, {"salary_max": 200000})
        assert job.salary_min is None and job.salary_max == 200000
        enrich_from(job, {"salary_min": 10000})
        assert job.salary_min is None

    def test_an_employment_type_the_first_source_did_not_state(self, db):
        job, filled = self.enrich(db, {"employment_type": "contract"})
        assert job.employment_type == "contract"
        assert "employment_type" in filled

    def test_a_posting_date_the_first_source_did_not_carry(self, db):
        when = datetime(2026, 3, 1, tzinfo=timezone.utc)
        job, filled = self.enrich(db, {"posted_at": when})
        assert job.posted_at == when
        assert "posted_at" in filled

    def test_a_date_that_is_still_a_string_is_left_alone(self, db):
        # A mis-parsed date silently ages a job out of the pipeline, so this
        # refuses to guess: callers parse, or the field stays null.
        job, filled = self.enrich(db, {"posted_at": "3 days ago"})
        assert job.posted_at is None
        assert filled == []

    def test_remote_can_be_gained(self, db):
        # The column cannot tell "not remote" from "the source didn't say" —
        # both are false — so true is a one-way ratchet.
        job, filled = self.enrich(db, {"is_remote": True})
        assert job.is_remote is True
        assert "is_remote" in filled

    def test_remote_cannot_be_lost(self, db):
        from app.services.deduplication import enrich_from

        job, _ = self.enrich(db, {"is_remote": True})
        assert enrich_from(job, {"is_remote": False}) == []
        assert job.is_remote is True

    def test_skills_are_taken_when_we_have_none(self, db):
        job, filled = self.enrich(db, {"required_skills": ["python", "sql"]})
        assert job.required_skills == ["python", "sql"]
        assert "required_skills" in filled

    def test_skills_do_not_replace_the_ones_we_read(self, db):
        from app.services.deduplication import enrich_from

        job, _ = self.enrich(db, {"required_skills": ["python"]})
        assert enrich_from(job, {"required_skills": ["cobol"]}) == []
        assert job.required_skills == ["python"]

    def test_an_apply_url_only_ever_fills_a_blank(self, db):
        from app.services.deduplication import enrich_from

        job, filled = self.enrich(db, {"apply_url": "https://ats.example/1"})
        assert job.apply_url == "https://ats.example/1"
        assert enrich_from(job, {"apply_url": "https://tracker.example/2"}) == []
        assert job.apply_url == "https://ats.example/1"

    def test_nothing_new_reports_nothing(self, db):
        # The return value is what lets a caller say "enriched" honestly.
        job, filled = self.enrich(db, {"title": "SWE", "company": "ACME"})
        assert filled == []

    def test_the_location_is_never_touched(self, db):
        # It is a third of the dedupe hash. A row whose stored location no
        # longer agrees with the hash computed from it splits in two the next
        # time the hashes are recomputed.
        from app.services.deduplication import enrich_from

        job, _ = self.enrich(db, {}, location="")
        enrich_from(job, {"location": "Berlin, Germany"})
        assert job.location == ""

    def test_an_edited_field_is_not_refilled(self, db):
        # The rule the whole `manual_fields` feature exists for, and the one
        # the fetch cycle's own backfill used to be the single exception to.
        from app.services.deduplication import enrich_from

        job, _ = self.enrich(db, {})
        job.manual_fields = ["salary_min", "employment_type"]
        assert enrich_from(job, {"salary_min": 5, "salary_max": 9,
                                 "employment_type": "internship"}) == []
        assert job.salary_min is None
        assert job.employment_type is None


class TestACrossPostBringsMoreThanItsUrl:
    """
    `merge_or_skip` used to take exactly two things from a cross-post: its URL
    and, if longer, its description. Everything else that second source knew
    and the first did not — the pay, the employment type, the posting date —
    was read, matched to an existing row, and dropped on the floor.
    """

    def test_it_reports_what_it_improved(self, db):
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/x1", source_job_id="X1",
                        dedupe_hash="c" * 32)
        improved = merge_or_skip(
            db, job, "https://other.com/x1", "long description " * 50,
            layer=3, data={"salary_min": 100000, "employment_type": "full_time"},
        )
        assert set(improved) == {"source_urls", "salary", "employment_type",
                                 "description"}

    def test_the_cross_post_s_pay_reaches_the_row(self, db):
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/x2", source_job_id="X2",
                        dedupe_hash="d" * 32)
        merge_or_skip(db, job, "https://other.com/x2", "", layer=3,
                      data={"salary_min": 90000, "salary_currency": "EUR"})
        assert (job.salary_min, job.salary_currency) == (90000, "EUR")

    def test_a_repeat_that_adds_nothing_reports_nothing(self, db):
        # Which is what lets the panel's "enriched" count mean the row got
        # better rather than that it was touched.
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/x3", source_job_id="X3",
                        dedupe_hash="e" * 32)
        assert merge_or_skip(db, job, "https://ex.com/x3", "A great job.",
                             layer=3, data={}) == []

    def test_the_url_is_still_recorded_when_nothing_else_is(self, db):
        # A second listing is a real second listing: this array is how the
        # overlay finds the row from whichever URL the user is looking at.
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/x4", source_job_id="X4",
                        dedupe_hash="f" * 32)
        assert merge_or_skip(db, job, "https://other.com/x4", "", layer=3,
                             data={}) == ["source_urls"]
        assert "https://other.com/x4" in job.source_urls

    def test_it_still_works_without_the_rest_of_the_sighting(self, db):
        # `data` is optional so the old two-argument behaviour is intact for
        # any caller that only has a URL and some text.
        from app.services.deduplication import merge_or_skip

        job = _make_job(db, url="https://ex.com/x5", source_job_id="X5",
                        dedupe_hash="1" * 32)
        assert merge_or_skip(db, job, "https://other.com/x5",
                             "long description " * 50, layer=3) == [
            "source_urls", "description"]
