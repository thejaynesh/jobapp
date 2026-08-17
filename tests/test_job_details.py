"""
The posting's stated facts, read into columns once.

The rule under test everywhere: **null when the posting does not say.** A
guessed salary is worse than a missing one — the salary floor filter would then
drop jobs on a number nobody ever wrote down, and the matcher would weigh
"required years" against a figure the model invented.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models.job import Job, JobStatus
from app.services import job_details


def _job(**kwargs) -> Job:
    defaults = dict(
        source="greenhouse",
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        url="https://boards.greenhouse.io/acme/jobs/1",
        status=JobStatus.new,
        fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex,
    )
    defaults.update(kwargs)
    return Job(**defaults)


LONG = "We need a backend engineer with Python and Go. " * 20


class TestNormalize:
    def test_a_stated_band_is_kept_as_a_band(self):
        out = job_details.normalize({
            "salary_min": 120000, "salary_max": 160000, "salary_currency": "usd",
        })
        assert out["salary_min"] == 120000
        assert out["salary_max"] == 160000
        assert out["salary_currency"] == "USD"

    def test_formatted_numbers_are_read(self):
        # Free providers return "$120,000", "120k" and 120000 interchangeably.
        out = job_details.normalize({"salary_min": "$120,000", "salary_max": "160k"})
        assert out["salary_min"] == 120000
        assert out["salary_max"] == 160000

    def test_prose_where_a_number_belongs_becomes_null(self):
        """
        "Competitive" must not become 0 — every filter downstream would read
        that as "this job pays nothing".
        """
        out = job_details.normalize({"salary_min": "competitive", "salary_max": None})
        assert out["salary_min"] is None
        assert out["salary_max"] is None

    def test_a_single_figure_becomes_the_floor_not_the_ceiling(self):
        # "$150,000 salary" arriving as a max would read as "up to $150k".
        out = job_details.normalize({"salary_min": None, "salary_max": 150000})
        assert out["salary_min"] == 150000

    def test_a_reversed_band_is_put_back_in_order(self):
        out = job_details.normalize({"salary_min": 180000, "salary_max": 120000})
        assert out["salary_min"] == 120000
        assert out["salary_max"] == 180000

    def test_a_currency_without_a_salary_is_dropped(self):
        out = job_details.normalize({"salary_currency": "USD"})
        assert out["salary_currency"] is None

    def test_an_unknown_employment_type_becomes_null(self):
        assert job_details.normalize({"employment_type": "freelance-ish"})[
            "employment_type"] is None

    def test_employment_type_spelling_is_normalised(self):
        assert job_details.normalize({"employment_type": "Full-Time"})[
            "employment_type"] == "full_time"

    def test_an_implausible_year_count_becomes_null(self):
        assert job_details.normalize({"required_years": 45})["required_years"] is None

    def test_skills_are_deduplicated_and_capped(self):
        out = job_details.normalize({
            "required_skills": ["Python", "python", "Go"] + [f"S{i}" for i in range(40)],
        })
        assert out["required_skills"][:3] == ["Python", "Go", "S0"]
        assert len(out["required_skills"]) <= 25

    def test_a_comma_string_of_skills_is_split(self):
        assert job_details.normalize({"required_skills": "Python, Go, AWS"})[
            "required_skills"] == ["Python", "Go", "AWS"]

    def test_nothing_stated_yields_all_nulls(self):
        out = job_details.normalize({})
        assert out["salary_min"] is None
        assert out["required_years"] is None
        assert out["employment_type"] is None
        assert out["required_skills"] == []


class TestExtract:
    def test_a_reply_is_parsed_and_normalised(self):
        reply = (
            '{"salary_min": 130000, "salary_max": 170000, "salary_currency": "USD",'
            ' "employment_type": "full_time", "required_years": 3,'
            ' "required_skills": ["Python"], "nice_to_have_skills": ["Go"],'
            ' "education_required": "Bachelor\'s in CS",'
            ' "benefits_note": "Equity and visa sponsorship.", "language": "en"}'
        )
        with patch("app.llm.providers.generation_chat", return_value=reply):
            out = job_details.extract(LONG)
        assert out["salary_min"] == 130000
        assert out["required_years"] == 3
        assert out["language"] == "en"

    def test_a_description_too_short_to_state_anything_skips_the_call(self):
        with patch("app.llm.providers.generation_chat") as chat:
            assert job_details.extract("Too short.") is None
        chat.assert_not_called()

    def test_a_provider_failure_returns_none_rather_than_empty_details(self):
        """
        None and "all fields null" mean different things: the first is retried,
        the second is the posting genuinely saying nothing.
        """
        with patch("app.llm.providers.generation_chat",
                   side_effect=RuntimeError("down")):
            assert job_details.extract(LONG) is None

    def test_an_unreadable_reply_returns_none(self):
        with patch("app.llm.providers.generation_chat", return_value="no json here"):
            assert job_details.extract(LONG) is None

    def test_a_reply_wrapped_in_prose_is_still_read(self):
        with patch("app.llm.providers.generation_chat",
                   return_value='Sure:\n```json\n{"required_years": 5}\n```'):
            assert job_details.extract(LONG)["required_years"] == 5


class TestNeedsExtraction:
    def test_an_unread_job_needs_a_call(self):
        assert job_details.needs_extraction(_job(description=LONG)) is True

    def test_an_already_read_job_does_not(self):
        job = _job(description=LONG, details_extracted_at=datetime.now(timezone.utc))
        assert job_details.needs_extraction(job) is False

    def test_a_description_that_grew_since_the_read_needs_another(self):
        """
        Enrichment routinely replaces a 500-character stub with the real
        posting. The facts in it were not there before.
        """
        earlier = datetime.now(timezone.utc) - timedelta(hours=1)
        job = _job(
            description=LONG,
            details_extracted_at=earlier,
            description_updated_at=datetime.now(timezone.utc),
        )
        assert job_details.needs_extraction(job) is True

    def test_a_job_with_no_description_never_costs_a_call(self):
        assert job_details.needs_extraction(_job(description=None)) is False
        assert job_details.needs_extraction(_job(description="tiny")) is False


class TestSalaryLabel:
    def test_a_band_reads_as_a_range(self):
        job = _job(salary_min=120000, salary_max=160000, salary_currency="USD")
        assert job.salary_label == "$120k–$160k"

    def test_a_single_figure_reads_as_one_number(self):
        job = _job(salary_min=150000, salary_max=150000, salary_currency="USD")
        assert job.salary_label == "$150k"

    def test_an_hourly_rate_is_not_abbreviated(self):
        job = _job(salary_min=65, salary_max=85, salary_currency="USD")
        assert job.salary_label == "$65–$85"

    def test_an_unstated_salary_has_no_label(self):
        assert _job().salary_label is None

    def test_an_unknown_currency_is_named_rather_than_symbolised(self):
        job = _job(salary_min=90000, salary_max=90000, salary_currency="SEK")
        assert "SEK" in job.salary_label


class TestMatcherIntegration:
    def test_the_facts_reach_the_scoring_prompt(self):
        from app.services.matcher import _build_match_prompt

        job = _job(
            description=LONG, required_years=3, salary_min=140000,
            salary_max=170000, salary_currency="USD", employment_type="full_time",
            required_skills=["Python", "Kubernetes"],
            education_required="Bachelor's in CS",
        )
        prompt = _build_match_prompt(job, {"target_roles": ["Backend Engineer"]})
        user = prompt[1]["content"]
        assert "Required experience (stated in the posting): 3 years" in user
        assert "$140k–$170k" in user
        assert "Python, Kubernetes" in user
        assert "Bachelor's in CS" in user

    def test_a_job_stating_nothing_adds_no_empty_lines(self):
        """An empty "Salary:" line invites the model to fill the gap itself."""
        from app.services.matcher import _build_match_prompt

        prompt = _build_match_prompt(_job(description=LONG), {"target_roles": ["x"]})
        user = prompt[1]["content"]
        assert "Stated salary" not in user
        assert "Required experience" not in user

    def test_details_are_read_only_after_the_keyword_filter_passes(self, db):
        """
        The whole reason this is affordable: a title-reject never costs a call.
        """
        from app.models.profile import Profile
        from app.services.matcher import match_job

        profile_data = {
            "target_roles": ["Backend Engineer"],
            "skills": {"lang": ["Python", "Go"]},
        }
        db.add(Profile(data=profile_data))
        rejected = _job(title="Dental Hygienist", description=LONG,
                        url="https://x/1", source_urls=["https://x/1"])
        db.add(rejected)
        db.commit()

        with patch("app.services.job_details.extract_and_apply") as extract:
            outcome = match_job(db, rejected, profile_data, "k", "u", "m")

        assert outcome == "filtered_out"
        extract.assert_not_called()

    def test_a_passing_job_gets_its_details_read(self, db):
        from app.models.profile import Profile
        from app.services.matcher import match_job

        profile_data = {
            "target_roles": ["Backend Engineer"],
            "skills": {"lang": ["Python", "Go"]},
        }
        db.add(Profile(data=profile_data))
        job = _job(description=LONG, url="https://x/2", source_urls=["https://x/2"])
        db.add(job)
        db.commit()

        with patch("app.services.job_details.extract_and_apply") as extract, \
             patch("app.services.matcher.llm_score_job",
                   return_value={"score": 80, "reasoning": "good", "matched_skills": [],
                                 "missing_skills": [], "seniority_fit": True}):
            match_job(db, job, profile_data, "k", "u", "m")

        extract.assert_called_once()

    def test_a_detail_failure_does_not_stop_the_scoring(self, db):
        # Details improve scoring; they are not a precondition for it.
        from app.models.profile import Profile
        from app.services.matcher import match_job

        profile_data = {
            "target_roles": ["Backend Engineer"],
            "skills": {"lang": ["Python", "Go"]},
        }
        db.add(Profile(data=profile_data))
        job = _job(description=LONG, url="https://x/3", source_urls=["https://x/3"])
        db.add(job)
        db.commit()

        with patch("app.services.job_details.extract_and_apply",
                   side_effect=RuntimeError("provider down")), \
             patch("app.services.matcher.llm_score_job",
                   return_value={"score": 80, "reasoning": "good", "matched_skills": [],
                                 "missing_skills": [], "seniority_fit": True}):
            outcome = match_job(db, job, profile_data, "k", "u", "m")

        assert outcome == "matched"


class TestJobsPageSalaryFilter:
    def _priced(self, db, low, high, **kwargs):
        job = _job(salary_min=low, salary_max=high, status=JobStatus.matched,
                   llm_score=80, url=f"https://x/{uuid.uuid4()}", **kwargs)
        job.source_urls = [job.url]
        db.add(job)
        return job

    def test_a_band_clears_a_floor_inside_it(self, client, db):
        """
        Matched against the top of the band: "$120k–$180k" is a job worth
        seeing at a $150k floor, and filtering on the bottom would hide it.
        """
        self._priced(db, 120000, 180000, title="Wide Band Engineer")
        db.commit()

        body = client.get("/jobs?min_salary=150000").text
        assert "Wide Band Engineer" in body

    def test_a_band_below_the_floor_is_hidden(self, client, db):
        self._priced(db, 60000, 90000, title="Underpaid Engineer")
        db.commit()

        body = client.get("/jobs?min_salary=150000").text
        assert "Underpaid Engineer" not in body

    def test_jobs_stating_no_salary_are_excluded_from_a_floor(self, client, db):
        job = _job(title="Silent About Pay", status=JobStatus.matched, llm_score=90,
                   url="https://x/silent", source_urls=["https://x/silent"])
        db.add(job)
        db.commit()

        assert "Silent About Pay" not in client.get("/jobs?min_salary=80000").text
        assert "Silent About Pay" in client.get("/jobs").text

    def test_the_page_says_how_many_jobs_state_any_pay(self, client, db):
        # Otherwise a filter that hides 90% of the list reads as broken.
        self._priced(db, 120000, 180000, title="Priced Engineer")
        db.commit()
        assert "priced)" in client.get("/jobs").text

    def test_a_nonsense_floor_is_ignored_rather_than_erroring(self, client, db):
        assert client.get("/jobs?min_salary=lots").status_code == 200


class TestJobCardPills:
    def test_the_stated_facts_appear_on_the_card(self, client, db):
        job = _job(
            title="Pilled Engineer", status=JobStatus.matched, llm_score=80,
            salary_min=140000, salary_max=170000, salary_currency="USD",
            employment_type="full_time", required_years=3,
            education_required="Bachelor's in CS",
            url="https://x/pills", source_urls=["https://x/pills"],
        )
        db.add(job)
        db.commit()

        body = client.get("/jobs").text
        assert "$140k–$170k" in body
        assert "Full-Time" in body
        assert "asks 3 yrs" in body
        assert "Bachelor&#39;s in CS" in body or "Bachelor's in CS" in body

    def test_a_job_stating_nothing_shows_no_empty_pills(self, client, db):
        job = _job(title="Plain Engineer", status=JobStatus.matched, llm_score=80,
                   url="https://x/plain", source_urls=["https://x/plain"])
        db.add(job)
        db.commit()

        body = client.get("/jobs").text
        assert "Plain Engineer" in body
        assert "asks " not in body
