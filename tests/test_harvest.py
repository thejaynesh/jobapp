"""
Reading jobs out of an intercepted API response.

The extractor is shape-based rather than path-based, so what these tests defend
is that property: nesting can move, wrappers can change, and jobs still come
out. The failure mode being avoided is a silent one — a payload reshuffle that
yields zero jobs looks exactly like an idle browser.
"""

from datetime import datetime, timezone

from app.models.job import Job
from app.services.deduplication import compute_dedupe_hash
from app.services import harvest

# Roughly the shape Voyager returns: cards nested several levels down, rich text
# as {"text": ...}, ids as urns.
VOYAGER = {
    "data": {
        "jobsDashJobCardsByJobSearch": {
            "elements": [
                {
                    "jobCardUnion": {
                        "jobPostingCard": {
                            "jobPosting": {
                                "entityUrn": "urn:li:fsd_jobPosting:3901234567",
                                "title": "Senior Backend Engineer",
                                "companyName": "Acme Corp",
                                "formattedLocation": "Boston, MA",
                                "description": {
                                    "text": "We need someone who knows Postgres.",
                                    "attributes": [],
                                },
                                "workRemoteAllowed": True,
                            }
                        }
                    }
                }
            ]
        }
    }
}


def make_job(db, **kwargs):
    """A job row shaped the way the fetcher writes them.

    `source_urls` and a real `dedupe_hash` matter: they are the two layers
    `find_existing_job` matches on, so a helper that omits them would test
    against rows the deduplicator could never have produced.
    """
    url = kwargs.pop("url", "https://www.linkedin.com/jobs/view/3901234567/")
    title = kwargs.pop("title", "Senior Backend Engineer")
    company = kwargs.pop("company", "Acme Corp")
    location = kwargs.pop("location", "Boston, MA")
    job = Job(
        source=kwargs.pop("source", harvest.HARVEST_SOURCE),
        source_urls=kwargs.pop("source_urls", [url]),
        url=url,
        title=title,
        company=company,
        location=location,
        description=kwargs.pop("description", "short"),
        dedupe_hash=kwargs.pop(
            "dedupe_hash", compute_dedupe_hash(company, title, location)
        ),
        fetched_at=datetime.now(timezone.utc),
        **kwargs,
    )
    db.add(job)
    db.commit()
    return job


class TestExtract:
    def test_finds_a_job_nested_deep(self):
        jobs = harvest.extract_jobs(VOYAGER)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"
        assert jobs[0]["company"] == "Acme Corp"
        assert jobs[0]["location"] == "Boston, MA"

    def test_reads_rich_text_descriptions(self):
        assert "Postgres" in harvest.extract_jobs(VOYAGER)[0]["description"]

    def test_pulls_the_id_out_of_an_urn(self):
        assert harvest.extract_jobs(VOYAGER)[0]["source_job_id"] == "3901234567"

    def test_reconstructs_a_url_from_the_id(self):
        # Nothing in the payload is a job URL; the id is enough to build one,
        # and building it beats dropping the job.
        assert harvest.extract_jobs(VOYAGER)[0]["url"].endswith("/3901234567/")

    def test_notices_remote(self):
        assert harvest.extract_jobs(VOYAGER)[0]["is_remote"] is True

    def test_survives_the_nesting_moving(self):
        # The whole point of walking rather than following a path: a redesign
        # that reorganizes the wrappers must not zero the harvest.
        reshuffled = {
            "included": [
                {
                    "$type": "com.linkedin.voyager.SomethingNew",
                    "jobPostingId": 5550001,
                    "title": "Data Engineer",
                    "companyName": "Globex",
                    "locationName": "Remote",
                }
            ]
        }
        jobs = harvest.extract_jobs(reshuffled)
        assert [j["title"] for j in jobs] == ["Data Engineer"]

    def test_accepts_a_top_level_list(self):
        payload = [
            {"jobPostingId": 1234567, "title": "SRE", "companyName": "Initech"}
        ]
        assert len(harvest.extract_jobs(payload)) == 1

    def test_deduplicates_within_one_payload(self):
        # A card and a detail blob for the same posting commonly both appear.
        payload = {
            "cards": [{"jobPostingId": 777888, "title": "SRE", "companyName": "Initech"}],
            "details": [
                {
                    "jobPostingId": 777888,
                    "title": "SRE",
                    "companyName": "Initech",
                    "description": "The long version.",
                }
            ],
        }
        jobs = harvest.extract_jobs(payload)
        assert len(jobs) == 1
        assert jobs[0]["description"] == "The long version.", "keep the richer copy"

    def test_ignores_objects_that_are_not_jobs(self):
        payload = {
            "title": "Recommended for you",
            "profile": {"name": "Someone", "title": "Engineer at Acme"},
            "footer": {"companyName": "LinkedIn"},
        }
        assert harvest.extract_jobs(payload) == []

    def test_requires_an_identifier(self):
        # Title and company but nothing to point at is a heading, not a job.
        assert harvest.extract_jobs({"title": "Engineer", "companyName": "Acme"}) == []

    def test_non_json_input_is_not_an_error(self):
        assert harvest.extract_jobs("a string") == []
        assert harvest.extract_jobs(None) == []
        assert harvest.extract_jobs(42) == []

    def test_an_empty_payload_yields_nothing(self):
        assert harvest.extract_jobs({}) == []

    def test_deep_nesting_terminates(self):
        # A pathological or cyclic-looking structure must not walk forever.
        node = {"title": "Engineer", "companyName": "Acme", "jobPostingId": 111222}
        for _ in range(60):
            node = {"wrap": node}
        harvest.extract_jobs(node)  # must return, depth-capped


class TestSave:
    def test_stores_a_new_job(self, db):
        counts = harvest.save_harvested_jobs(db, harvest.extract_jobs(VOYAGER))
        assert counts["inserted"] == 1
        stored = db.query(Job).one()
        assert stored.source == harvest.HARVEST_SOURCE
        assert stored.company == "Acme Corp"

    def test_enriches_a_job_already_known_with_a_thinner_description(self, db):
        # The reason this path is worth having: the guest API returns ten cards
        # a page with no description, and Voyager returns the description
        # inline. Meeting the same posting again should improve it.
        make_job(db, description="short")
        counts = harvest.save_harvested_jobs(db, harvest.extract_jobs(VOYAGER))
        assert counts["merged"] == 1
        assert "Postgres" in db.query(Job).one().description

    def test_does_not_replace_a_fuller_description_with_a_thinner_one(self, db):
        long_text = "A very long and complete description. " * 20
        make_job(db, description=long_text)
        harvest.save_harvested_jobs(db, harvest.extract_jobs(VOYAGER))
        assert db.query(Job).one().description == long_text

    def test_the_same_payload_twice_inserts_once(self, db):
        jobs = harvest.extract_jobs(VOYAGER)
        harvest.save_harvested_jobs(db, jobs)
        second = harvest.save_harvested_jobs(db, jobs)
        assert second["inserted"] == 0
        assert db.query(Job).count() == 1

    def test_collapses_into_a_job_the_fetcher_already_stored(self, db):
        # Harvested and fetched copies of one posting must not become two rows.
        make_job(
            db,
            source="linkedin",
            url="https://www.linkedin.com/jobs/view/3901234567/",
            description="",
        )
        harvest.save_harvested_jobs(db, harvest.extract_jobs(VOYAGER))
        assert db.query(Job).count() == 1

    def test_incomplete_jobs_are_counted_not_stored(self, db):
        counts = harvest.save_harvested_jobs(
            db, [{"title": "", "company": "Acme", "url": "https://x/1"}]
        )
        assert counts["invalid"] == 1
        assert db.query(Job).count() == 0

    def test_saving_nothing_is_not_an_error(self, db):
        assert harvest.save_harvested_jobs(db, [])["inserted"] == 0


class TestHarvestedSalary:
    """
    Pay the guest API never sends and the browser sees anyway — most of why
    turning the harvest toggle on is worth doing.
    """

    def test_a_nested_pay_band_is_read(self):
        from app.services.harvest import extract_jobs

        payload = {"elements": [{
            "title": "Backend Engineer",
            "companyName": "Acme",
            "jobPostingId": 4012345678,
            "salaryInsights": {
                "compensationBreakdown": [
                    {"minSalary": 150000, "maxSalary": 190000, "currencyCode": "USD"}
                ]
            },
        }]}
        job = extract_jobs(payload)[0]
        assert job["salary_min"] == 150000
        assert job["salary_max"] == 190000
        assert job["salary_currency"] == "USD"

    def test_money_wrapped_as_an_amount_object_is_read(self):
        from app.services.harvest import extract_jobs

        payload = {"elements": [{
            "title": "Backend Engineer",
            "companyName": "Acme",
            "jobPostingId": 4012345679,
            "compensation": {
                "min": {"amount": "120000", "currencyCode": "USD"},
                "max": {"amount": "160000", "currencyCode": "USD"},
            },
        }]}
        job = extract_jobs(payload)[0]
        assert job["salary_min"] == 120000
        assert job["salary_max"] == 160000

    def test_a_card_with_no_pay_reports_none(self):
        from app.services.harvest import extract_jobs

        payload = {"elements": [{
            "title": "Backend Engineer", "companyName": "Acme",
            "jobPostingId": 4012345680,
        }]}
        assert "salary_min" not in extract_jobs(payload)[0]

    def test_a_harvested_band_reaches_the_stored_job(self, db):
        from app.models.job import Job
        from app.services.harvest import save_harvested_jobs

        save_harvested_jobs(db, [{
            "title": "Backend Engineer", "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/900/",
            "source_job_id": "900",
            "salary_min": 150000.0, "salary_max": 190000.0,
            "salary_currency": "USD",
        }])

        job = db.query(Job).filter(Job.source_job_id == "900").one()
        assert job.salary_label == "$150k–$190k"

    def test_a_salary_already_read_from_the_description_is_not_overwritten(self, db):
        """
        The detail extractor read the posting itself with a model told never to
        guess. A harvested card is a summary of the same job — first stated
        figure wins, and a card that says nothing leaves the column alone.
        """
        import uuid
        from datetime import datetime, timezone

        from app.models.job import Job, JobStatus
        from app.services.harvest import save_harvested_jobs

        url = "https://www.linkedin.com/jobs/view/901/"
        db.add(Job(
            source="linkedin_harvest", source_job_id="901", source_urls=[url],
            title="Backend Engineer", company="Acme", location="", url=url,
            status=JobStatus.new, fetched_at=datetime.now(timezone.utc),
            dedupe_hash=uuid.uuid4().hex,
            salary_min=140000.0, salary_max=180000.0, salary_currency="USD",
        ))
        db.commit()

        save_harvested_jobs(db, [{
            "title": "Backend Engineer", "company": "Acme", "url": url,
            "source_job_id": "901",
            "salary_min": 10.0, "salary_max": 20.0, "salary_currency": "USD",
        }])

        job = db.query(Job).filter(Job.source_job_id == "901").one()
        assert job.salary_min == 140000.0


class TestHarvestBeyondLinkedIn:
    """
    The extractor was always host-agnostic — it finds anything shaped like a
    job in any JSON. Only the interceptor's registration limited it to
    LinkedIn. Now that it can be registered per site, each host needs its own
    source name, or Indeed's yield disappears into LinkedIn's number and
    neither can be judged.
    """

    def test_each_host_gets_its_own_source_name(self):
        from app.services.harvest import source_for_url

        assert source_for_url("https://www.linkedin.com/jobs/view/1/") == "linkedin_harvest"
        assert source_for_url("https://www.indeed.com/viewjob?jk=1") == "indeed_harvest"
        assert source_for_url("https://uk.indeed.com/viewjob?jk=1") == "indeed_harvest"
        assert source_for_url("https://www.glassdoor.com/job-listing/1") == "glassdoor_harvest"
        assert source_for_url("https://acme.wd5.myworkdayjobs.com/x") == "workday_harvest"

    def test_an_unknown_host_falls_back_rather_than_inventing_a_source(self):
        # A wrong-but-known bucket is easier to notice than a new source name
        # appearing silently.
        from app.services.harvest import source_for_url

        assert source_for_url("https://example.com/jobs") == "linkedin_harvest"
        assert source_for_url("") == "linkedin_harvest"

    def test_an_indeed_payload_is_read_with_indeed_field_names(self):
        from app.services.harvest import extract_jobs

        payload = {"metaData": {"mosaicProviderJobCardsModel": {"results": [{
            "jobkey": "abc123def456",
            "displayTitle": "Backend Engineer",
            "truncatedCompany": "Acme",
            "formattedLocation": "Austin, TX",
            "snippet": "Build APIs with Python.",
            "jobUrl": "https://www.indeed.com/viewjob?jk=abc123def456",
        }]}}}
        jobs = extract_jobs(payload, source="indeed_harvest")

        assert len(jobs) == 1
        assert jobs[0]["source"] == "indeed_harvest"
        assert jobs[0]["title"] == "Backend Engineer"
        assert jobs[0]["company"] == "Acme"
        assert jobs[0]["url"] == "https://www.indeed.com/viewjob?jk=abc123def456"

    def test_a_glassdoor_payload_is_read_with_glassdoor_field_names(self):
        from app.services.harvest import extract_jobs

        payload = {"data": {"jobListings": [{
            "jobListingId": 1009988776,
            "jobTitleText": "Staff Engineer",
            "employerName": "Globex",
            "locationName": "Remote",
            "jobUrl": "https://www.glassdoor.com/job-listing/1009988776",
        }]}}
        jobs = extract_jobs(payload, source="glassdoor_harvest")

        assert len(jobs) == 1
        assert jobs[0]["source"] == "glassdoor_harvest"
        assert jobs[0]["company"] == "Globex"

    def test_harvested_jobs_are_stored_under_their_own_source(self, db):
        from app.models.job import Job
        from app.services.harvest import save_harvested_jobs

        save_harvested_jobs(db, [{
            "source": "indeed_harvest",
            "title": "Backend Engineer", "company": "Acme",
            "url": "https://www.indeed.com/viewjob?jk=zz1",
            "source_job_id": "zz1",
            "description": "Build things.",
        }])

        job = db.query(Job).filter(Job.source_job_id == "zz1").one()
        assert job.source == "indeed_harvest"

    def test_the_endpoint_files_a_payload_under_the_page_it_came_from(self, db):
        from app.models.job import Job
        from app.routers.agent import _harvest

        payload = {"results": [{
            "jobkey": "a1b2c3d4e5f6a7b8", "displayTitle": "Backend Engineer",
            "truncatedCompany": "Acme",
            "jobUrl": "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f6a7b8",
        }]}
        counts = _harvest(db, payload, "https://www.indeed.com/jobs?q=backend")

        assert counts["source"] == "indeed_harvest"
        assert db.query(Job).filter(
            Job.source_job_id == "a1b2c3d4e5f6a7b8").one().source == \
            "indeed_harvest"

    def test_an_alphanumeric_posting_id_is_read(self):
        """
        Reading only numbers left every harvested Indeed job with no id, which
        drops it to URL-only dedupe — so the same posting re-inserts itself
        whenever the URL picks up a different tracking parameter.
        """
        from app.services.harvest import extract_jobs

        jobs = extract_jobs({"results": [{
            "jobkey": "a1b2c3d4e5f6a7b8",
            "displayTitle": "Backend Engineer",
            "truncatedCompany": "Acme",
            "jobUrl": "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f6a7b8",
        }]}, source="indeed_harvest")
        assert jobs[0]["source_job_id"] == "a1b2c3d4e5f6a7b8"

    def test_an_ordinary_word_under_an_id_key_is_not_an_id(self):
        from app.services.harvest import extract_jobs

        jobs = extract_jobs({"results": [{
            "id": "featured",
            "displayTitle": "Backend Engineer",
            "truncatedCompany": "Acme",
            "jobUrl": "https://www.indeed.com/viewjob?jk=1",
        }]}, source="indeed_harvest")
        assert jobs[0]["source_job_id"] is None
