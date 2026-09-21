"""
The posting's stated facts, read into columns once.

The rule under test everywhere: **null when the posting does not say.** A
guessed salary is worse than a missing one — the salary floor filter would then
drop jobs on a number nobody ever wrote down, and the matcher would weigh
"required years" against a figure the model invented.
"""

import pytest

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
    def _priced(self, db, low, high, period="year", currency="USD", **kwargs):
        """
        A job whose pay the filter can actually compare.

        The filter reads the annualised columns, not the stated ones — an
        hourly rate, a monthly rate and a euro figure are not on the same axis,
        and comparing the stated figure to a floor is what hid a $65/hour
        posting from a $100k floor. Production derives these on write (see
        `job_details.with_annual`); a test building a row by hand has to do the
        same.
        """
        from app.services.job_details import with_annual

        annual = with_annual({
            "salary_min": low, "salary_max": high,
            "salary_period": period, "salary_currency": currency,
        })
        job = _job(status=JobStatus.matched, llm_score=80,
                   url=f"https://x/{uuid.uuid4()}", **annual, **kwargs)
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


class TestSeniorityTrustsTheStatedNumber:
    """
    A title word is a guess about a number. Now that postings state the number,
    the number wins — a "Senior Engineer" asking for 3 years is a job a
    2.4-year candidate should be scored against, and dropping it on the word
    "Senior" is the kind of confident mistake that makes the list smaller than
    it should be.
    """

    def _profile(self):
        """
        A candidate with ~2.4 years, derived from dates the way the filter
        reads them. Fixed dates rather than relative ones: a profile that
        drifts past the junior threshold as time passes would make this suite
        start passing for the wrong reason.
        """
        return {
            "target_roles": ["Backend Engineer"],
            "skills": {"lang": ["Python"]},
            "experience": [{
                "role": "Engineer", "company": "Acme",
                "start_date": "Apr 2024", "end_date": "Aug 2026",
            }],
        }

    def test_a_senior_title_asking_few_years_reaches_the_model(self):
        from app.services.matcher import _blocked_by_seniority

        job = _job(title="Senior Backend Engineer", required_years=3.0)
        assert _blocked_by_seniority(job, self._profile()) is False

    def test_a_senior_title_asking_many_years_is_still_dropped(self):
        from app.services.matcher import _blocked_by_seniority

        job = _job(title="Senior Backend Engineer", required_years=10.0)
        assert _blocked_by_seniority(job, self._profile()) is True

    def test_a_junior_title_asking_many_years_is_dropped_too(self):
        # The number cuts both ways: the title said nothing alarming, and the
        # posting did.
        from app.services.matcher import _blocked_by_seniority

        job = _job(title="Backend Engineer", required_years=12.0)
        assert _blocked_by_seniority(job, self._profile()) is True

    def test_a_posting_stating_nothing_falls_back_to_the_title(self):
        from app.services.matcher import _blocked_by_seniority

        assert _blocked_by_seniority(
            _job(title="Senior Backend Engineer"), self._profile()) is True
        assert _blocked_by_seniority(
            _job(title="Backend Engineer"), self._profile()) is False

    def test_the_reason_names_the_number_when_there_is_one(self, db):
        from app.services.matcher import evaluate_keyword_filter

        outcome = evaluate_keyword_filter(
            _job(title="Backend Engineer", required_years=12.0, description=LONG),
            self._profile(),
        )
        assert outcome.reason == "seniority"
        assert "asks for 12 years" in outcome.detail

    def test_the_reason_says_so_when_only_the_title_was_evidence(self, db):
        from app.services.matcher import evaluate_keyword_filter

        outcome = evaluate_keyword_filter(
            _job(title="Senior Backend Engineer", description=LONG), self._profile()
        )
        assert outcome.reason == "seniority"
        assert "states no required years" in outcome.detail


class TestTheModelSeesTheWholePosting:
    def test_a_long_description_is_no_longer_cut_at_4000_chars(self):
        """
        4,000 characters was chosen when descriptions were 500-character
        stubs. Once enrichment fetched the real text it cut off
        mid-requirements, so the model scored skill and seniority fit against
        the marketing half of the posting.
        """
        from app.services.matcher import _build_match_prompt

        marketing = "We are a mission-driven company changing the world. " * 100
        requirements = "You must have deep Kubernetes and Rust experience."
        job = _job(description=marketing + requirements)

        user = _build_match_prompt(job, {"target_roles": ["Backend Engineer"]})[1]["content"]
        assert len(marketing) > 4000
        assert requirements in user

    def test_a_pathological_page_is_still_capped(self):
        # "The full text" and "unbounded" are not the same promise: a page that
        # cleaned badly can be hundreds of kilobytes and is not a posting.
        from app.config import settings
        from app.services.matcher import _build_match_prompt

        job = _job(description="x" * (settings.MATCH_DESCRIPTION_CHARS + 50_000))
        user = _build_match_prompt(job, {"target_roles": ["Backend Engineer"]})[1]["content"]

        assert "[description truncated]" in user
        assert len(user) < settings.MATCH_DESCRIPTION_CHARS + 5000


class TestASilentDescriptionDoesNotDeleteAStatedFact:
    """
    The prompt's rule is "null when the posting does not say", and the writer
    has to honour the same reading: a null means *this description* is silent,
    not that what we already hold is wrong.

    Adapters carry pay and employment type as their own fields, alongside the
    description rather than inside it — USAJOBS states a grade band, every
    `base.jobs_from_listing` board (iCIMS, Teamtailor, Jobvite) puts one in the
    listing JSON, hiring.cafe ships a structured block. The prose then very
    often does not restate it, so writing the null back deleted a figure the
    employer had published, on a job the salary filter was about to read.
    """

    def test_a_stated_band_survives_a_description_that_never_mentions_pay(self):
        job = _job(salary_min=120000.0, salary_max=160000.0, salary_currency="USD")

        job_details.apply(job, job_details.normalize({"salary_min": None}))

        assert (job.salary_min, job.salary_max) == (120000.0, 160000.0)
        assert job.salary_currency == "USD"

    def test_the_adapters_employment_type_survives_too(self):
        job = _job(employment_type="contract")

        job_details.apply(job, job_details.normalize({}))

        assert job.employment_type == "contract"

    def test_a_fuller_description_may_still_correct_the_band(self):
        # The whole point of re-reading a grown description: a non-null is a
        # correction and wins. Only silence is treated as "no news".
        job = _job(salary_min=120000.0, salary_max=160000.0, salary_currency="USD")

        job_details.apply(job, job_details.normalize({
            "salary_min": 150000, "salary_max": 190000, "salary_currency": "USD",
        }))

        assert (job.salary_min, job.salary_max) == (150000.0, 190000.0)

    def test_pay_is_replaced_as_a_band_never_half_of_one(self):
        # A minimum from the model over a maximum from the adapter is a range
        # nobody stated, and the salary floor would then filter on it.
        job = _job(salary_min=120000.0, salary_max=160000.0, salary_currency="USD")

        job_details.apply(job, job_details.normalize({"salary_max": 190000}))

        # normalize() reads a lone maximum as "the figure", so both move.
        assert job.salary_min == 190000.0
        assert job.salary_max == 190000.0

    def test_an_empty_skill_list_does_not_wipe_the_skills_we_have(self):
        job = _job(required_skills=["Python", "Go"])

        job_details.apply(job, job_details.normalize({"required_skills": []}))

        assert job.required_skills == ["Python", "Go"]

    def test_the_read_is_still_stamped_even_when_the_posting_said_nothing(self):
        # Otherwise `needs_extraction` would ask again on every pass forever.
        job = _job()

        job_details.apply(job, job_details.normalize({}))

        assert job.details_extracted_at is not None

    def test_a_hand_typed_figure_still_outranks_the_model(self):
        job = _job(salary_min=99000.0, salary_max=99000.0, manual_fields=["salary_min"])

        job_details.apply(job, job_details.normalize({"salary_min": 150000}))

        assert job.salary_min == 99000.0


class TestPayHasAPeriodOrItIsNotComparable:
    """
    `salary_min`/`salary_max` were floats with a currency and nothing recording
    *per what*. The prompt asked the model to pick a convention, so an hourly
    rate and an annual salary landed in the same column — `Job.salary_label`
    said so out loud in a comment.

    The display coped. The filter compared the stated figure to a floor, so a
    $100k floor hid a $65/hour posting worth about $135k a year, and admitted a
    €100,000 one against a floor the user meant in dollars. Three incompatible
    kinds of number on one axis, in the one place this project keeps saying a
    wrong number is worse than a missing one.
    """

    @pytest.mark.parametrize("raw,expected", [
        ("/hr", "hour"), ("hourly", "hour"), ("Hour", "hour"),
        ("per year", "year"), ("annually", "year"), ("yr", "year"),
        ("mo", "month"), ("weekly", "week"), ("day", "day"),
        ("fortnightly", None), ("", None), (None, None), (7, None),
    ])
    def test_the_period_is_read_from_what_boards_actually_write(
            self, raw, expected):
        from app.services.job_details import normalise_period

        assert normalise_period(raw) == expected

    def test_an_hourly_rate_becomes_a_comparable_number(self):
        from app.services.job_details import annualise

        # 40 hours x 52 weeks. The exact figure matters less than the fact that
        # it is now on the same axis as a stated salary.
        assert annualise(65, "hour", "USD") == 135_200.0

    @pytest.mark.parametrize("period,expected", [
        ("hour", 135_200.0), ("day", 16_900.0), ("week", 3_380.0),
        ("month", 780.0), ("year", 65.0),
    ])
    def test_every_period_converts(self, period, expected):
        from app.services.job_details import annualise

        assert annualise(65, period, "USD") == expected

    def test_no_stated_period_yields_no_annual_figure(self):
        """
        Guessing "year" is how a $65 rate became a $65 salary. A NULL here is
        excluded from a floor rather than admitted to it, which is the same
        treatment a posting stating no pay already gets.
        """
        from app.services.job_details import annualise

        assert annualise(65, None, "USD") is None

    def test_a_currency_we_cannot_convert_yields_no_annual_figure(self):
        """€100,000 is not 100,000 dollars, and the floor is in dollars."""
        from app.services.job_details import annualise

        assert annualise(100_000, "year", "EUR") is None
        assert annualise(100_000, "year", "USD") == 100_000.0

    def test_normalize_carries_the_period_and_the_derived_band(self):
        from app.services.job_details import normalize

        details = normalize({
            "salary_min": "65", "salary_max": "80",
            "salary_period": "/hr", "salary_currency": "usd",
        })
        assert details["salary_period"] == "hour"
        assert details["salary_min"] == 65.0
        assert details["salary_annual_min"] == 135_200.0
        assert details["salary_annual_max"] == 166_400.0

    def test_a_posting_stating_no_pay_states_no_period(self):
        from app.services.job_details import normalize

        details = normalize({"salary_period": "hour"})
        assert details["salary_min"] is None
        assert details["salary_period"] is None
        assert details["salary_annual_min"] is None

    @pytest.mark.parametrize("amount,period,expected", [
        (65, "hour", "$65/hr"),
        (9000, "month", "$9k/mo"),
        (135000, "year", "$135k"),
        # No stated period reads as a bare figure rather than a wrong unit.
        (65, None, "$65"),
    ])
    def test_the_label_says_what_the_figure_is_per(
            self, amount, period, expected):
        job = _job(salary_min=amount, salary_currency="USD",
                   salary_period=period)
        assert job.salary_label == expected


class TestTheSalaryFloorComparesLikeWithLike:
    """
    The filter compared `coalesce(salary_max, salary_min)` to the floor, and
    those columns hold whatever the posting wrote. So a $100k floor hid a
    $65/hour posting worth about $135k a year — one of the best-paying things
    in the table — and admitted a €100,000 one against a floor the user meant
    in dollars.
    """

    def _job_at(self, db, title, low, period, currency="USD"):
        from app.services.job_details import with_annual

        annual = with_annual({
            "salary_min": low, "salary_max": None,
            "salary_period": period, "salary_currency": currency,
        })
        job = _job(title=title, status=JobStatus.matched, llm_score=80,
                   url=f"https://x/{uuid.uuid4()}", **annual)
        job.source_urls = [job.url]
        db.add(job)
        return job

    def test_an_hourly_rate_clears_a_floor_it_actually_clears(self, client, db):
        self._job_at(db, "Hourly Contractor", 65, "hour")
        db.commit()
        assert "Hourly Contractor" in client.get("/jobs?min_salary=100000").text

    def test_an_hourly_rate_below_the_floor_is_still_hidden(self, client, db):
        self._job_at(db, "Cheap Contractor", 20, "hour")   # ~$41.6k
        db.commit()
        assert "Cheap Contractor" not in client.get("/jobs?min_salary=100000").text

    def test_a_currency_we_cannot_convert_is_excluded_not_admitted(
            self, client, db):
        """€100,000 is not 100,000 dollars, and the floor is in dollars."""
        self._job_at(db, "Euro Engineer", 100_000, "year", currency="EUR")
        db.commit()
        body = client.get("/jobs?min_salary=90000").text
        assert "Euro Engineer" not in body
        # Still visible with no floor applied — excluded from the filter, not
        # hidden from the list.
        assert "Euro Engineer" in client.get("/jobs").text

    def test_a_band_with_no_stated_period_is_excluded(self, client, db):
        """Same treatment as a posting that states no pay at all."""
        self._job_at(db, "Unitless Engineer", 150_000, None)
        db.commit()
        assert "Unitless Engineer" not in client.get("/jobs?min_salary=100000").text


class TestEveryPathThatWritesAPayBandStatesItsPeriod:
    """
    The filter reads `salary_annual_*`, which is derived from the period. So a
    write path that knows its period and does not record it takes every row it
    inserts off the pay filter — a posting that states its pay, missing from a
    pay search. These are the paths that know.
    """

    def test_usajobs_states_year_because_it_only_keeps_annual_grades(self):
        from app.services.sources.usajobs import _salary

        found = _salary({
            "PositionRemuneration": [
                {"RateIntervalCode": "PA", "MinimumRange": "120000",
                 "MaximumRange": "150000"},
            ],
        })

        assert found["salary_period"] == "year"

    def test_usajobs_still_drops_an_hourly_grade_entirely(self):
        # The period is stated *because* the filter above it already ran, not
        # instead of it: an hourly grade is skipped, not relabelled annual.
        from app.services.sources.usajobs import _salary

        assert _salary({
            "PositionRemuneration": [
                {"RateIntervalCode": "PH", "MinimumRange": "60",
                 "MaximumRange": "80"},
            ],
        }) == {}

    def test_harvest_states_year_on_the_bands_it_keeps(self):
        from app.services.harvest import _annual_salary

        found = _annual_salary(
            {"compensation": {"min": 130000, "max": 160000,
                              "currencyCode": "USD"}},
            "greenhouse",
        )

        assert found["salary_min"] == 130000
        assert found["salary_period"] == "year"

    def test_harvest_still_drops_an_implausible_annual_figure(self):
        # `_MIN_PLAUSIBLE_ANNUAL` is what decides the band is annual in the
        # first place, so a rate that fails it must not come back labelled.
        from app.services.harvest import _annual_salary

        assert _annual_salary({"compensation": {"min": 65, "max": 80}},
                              "greenhouse") == {}

    def test_the_fetch_insert_path_derives_the_annual_pair(self):
        # `_adapter_details` goes straight into `Job(**...)`, so if it does not
        # carry the derived columns nothing else will fill them for that row.
        from app.services.job_fetcher import _adapter_details

        details = _adapter_details({
            "salary_min": 65.0, "salary_max": 75.0,
            "salary_currency": "USD", "salary_period": "hourly",
        })

        assert details["salary_period"] == "hour"
        assert details["salary_annual_min"] == 65.0 * 2080
        assert details["salary_annual_max"] == 75.0 * 2080

    def test_a_period_with_no_figures_is_dropped_not_stored(self):
        from app.services.job_fetcher import _adapter_details

        details = _adapter_details({"salary_period": "hour",
                                    "salary_currency": "USD"})

        assert "salary_period" not in details
        assert "salary_currency" not in details


class TestTheMergeDerivesRatherThanTrustsAKey:
    def test_a_cross_source_sighting_gets_a_usable_annual_band(self):
        """
        Ingest dicts carry a period and never the annual pair, so reading
        `data["salary_annual_min"]` stored a NULL and left the posting off the
        pay filter. It is derived from the figures being written instead.
        """
        from app.services.deduplication import enrich_from

        job = _job(salary_min=None, salary_max=None)
        filled = enrich_from(job, {
            "salary_min": 65.0, "salary_max": 80.0,
            "salary_currency": "USD", "salary_period": "HOUR",
        })

        assert "salary" in filled
        assert job.salary_period == "hour"          # normalised, not raw
        assert job.salary_annual_min == 65.0 * 2080

    def test_a_period_it_cannot_read_leaves_the_band_unannualised(self):
        from app.services.deduplication import enrich_from

        job = _job(salary_min=None, salary_max=None)
        enrich_from(job, {"salary_min": 1000.0, "salary_currency": "USD",
                          "salary_period": "per fortnight"})

        assert job.salary_period is None
        assert job.salary_annual_min is None


class TestTheDerivedColumnsFollowTheStatedOnes:
    def test_a_hand_edited_figure_is_what_gets_annualised(self):
        """
        `apply` writes field by field and skips manual ones, so copying the
        annual pair across from the extraction left the stated band showing the
        user's number and the filtered band showing the model's.
        """
        from app.services import job_details

        job = _job(salary_min=200_000.0, salary_max=200_000.0,
                   salary_currency="USD", salary_period="year")
        job.manual_fields = ["salary_min", "salary_max"]

        job_details.apply(job, job_details.normalize({
            "salary_min": 90_000, "salary_max": 90_000,
            "salary_currency": "USD", "salary_period": "year",
        }))

        assert job.salary_min == 200_000.0        # the lock held
        assert job.salary_annual_min == 200_000.0  # and the filter agrees

    def test_editing_the_period_re_derives_the_annual_band(self, db):
        from app.services import job_edits

        job = _job(salary_min=65.0, salary_max=80.0,
                   salary_currency="USD", salary_period="year")

        job_edits.apply(db, job, {"salary_period": "hour"})

        assert job.salary_period == "hour"
        assert job.salary_annual_min == 65.0 * 2080

    def test_editing_a_figure_re_derives_it_too(self, db):
        from app.services import job_edits

        job = _job(salary_min=100_000.0, salary_max=100_000.0,
                   salary_currency="USD", salary_period="year")

        job_edits.apply(db, job, {"salary_min": "150000"})

        assert job.salary_annual_min == 150_000.0

    def test_the_form_will_not_take_a_period_the_conversion_cannot_use(self):
        from app.services.job_edits import parse

        _, errors = parse({"salary_period": "fortnight"})

        assert "salary_period" in errors

    def test_a_blank_period_stays_blank_rather_than_becoming_a_year(self):
        """"The posting does not say" is a fact, and not the same as "annual"."""
        from app.services.job_edits import parse

        parsed, errors = parse({"salary_period": ""})

        assert errors == {}
        assert parsed["salary_period"] is None
