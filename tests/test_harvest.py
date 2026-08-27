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


# The real shape my.greenhouse.io returns, trimmed to two rows. This is
# Greenhouse's own aggregate board — every job posted through Greenhouse,
# regardless of employer — and it needs a login, so the browser is the only
# thing that can read it.
#
# Two things about it are worth stating, because both were bugs:
#
#   * There is no description field. Not a short one, not a snippet — the
#     search endpoint does not send one at all. The card is a card. That is
#     fine, because `publicUrl` points at the employer's own Greenhouse board,
#     which enrichment already knows how to read by API.
#   * `publicUrl` was the *only* URL on the row, and it wasn't in `_URL_KEYS`.
#     `_normalize` requires a URL, so every job on this board was found,
#     read, judged URL-less, and dropped.
GREENHOUSE_BOARD = {
    "props": {
        "jobPosts": [
            {
                "id": 4956068101,
                "title": "Senior Software Engineer, Platform",
                "viewed": False,
                "logoUrl": "https://example.com/logo.png",
                "workType": "remote",
                "appliedAt": None,
                "locations": ["San Francisco, CA", "New York, NY"],
                "payRanges": "$190,978 - $231,050",
                "publicUrl": "https://job-boards.greenhouse.io/corporatecareers/jobs/4956068101",
                "companyName": "Corporate Careers",
                "viewJobPath": "/jobs/corporatecareers/4956068101",
                "firstPublished": "2026-08-19T12:00:00Z",
            },
            {
                "id": 4123456789,
                "title": "Flutter Developer",
                "workType": "in_person",
                "locations": ["Austin, TX"],
                "payRanges": None,
                "publicUrl": "https://job-boards.greenhouse.io/acme/jobs/4123456789",
                "companyName": "Acme",
                "viewJobPath": "/jobs/acme/4123456789",
            },
            {
                # The common case that costs us something: the employer hosts
                # the posting on their own careers page, so `publicUrl` names
                # the job but not the company slug. `viewJobPath` still does.
                "id": 4777000111,
                "title": "Mobile Engineer",
                "workType": "hybrid",
                "locations": ["Logan, UT"],
                "publicUrl": "https://www.ifit.com/careers?gh_jid=4777000111",
                "companyName": "iFIT",
                "viewJobPath": "/jobs/ifit/4777000111",
            },
        ]
    }
}


class TestTheGreenhouseAggregateBoard:
    """
    Reading Greenhouse's job-seeker board, which is worth more than one more
    site: it lists jobs from every company on Greenhouse, so it is also where
    company slugs come from for the API fetcher that needs them.
    """

    def test_the_walker_reads_it_without_a_learned_recipe(self):
        # A recipe is the fallback for payloads the generic reader cannot see.
        # This one it can, now that `publicUrl` counts as a URL — so the board
        # keeps working even if the recipe is rejected or goes stale.
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        assert len(jobs) == 3
        assert {job["title"] for job in jobs} == {
            "Senior Software Engineer, Platform", "Flutter Developer",
            "Mobile Engineer"}

    def test_public_url_becomes_the_job_url(self):
        """
        Not a nicety. `_normalize` returns None without a URL, and this row has
        no other one, so before `publicUrl` was an alias the whole board
        harvested as zero jobs — which looks exactly like an idle browser.
        """
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        senior = next(j for j in jobs if j["title"].startswith("Senior"))
        assert senior["url"] == \
            "https://job-boards.greenhouse.io/corporatecareers/jobs/4956068101"

    def test_the_url_is_one_enrichment_can_fetch_a_description_from(self):
        """
        The board sends no description, so the description has to come from
        somewhere. `publicUrl` is a Greenhouse board URL, which enrichment
        recognises as an ATS and reads by API — full text, free, no browser.
        This is what makes the missing description a non-problem rather than a
        reason to open every posting in a tab.
        """
        from app.services.enrichment import looks_like_ats

        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        # The address enrichment will actually try — `_target_url` prefers the
        # apply URL, which is where the derived Greenhouse URL lands for the
        # rows whose `publicUrl` points at the employer's own careers page.
        assert all(
            looks_like_ats(job.get("apply_url") or job["url"]) for job in jobs
        )

    def test_the_first_location_is_the_posting_location(self):
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        senior = next(j for j in jobs if j["title"].startswith("Senior"))
        assert senior["location"] == "San Francisco, CA"

    def test_a_pay_band_written_for_a_person_is_read(self):
        """
        There are no min/max keys on this board — pay is a sentence. `_salary`
        alone came back empty on every row, which loses the one field the
        server-side APIs most often cannot supply.
        """
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        senior = next(j for j in jobs if j["title"].startswith("Senior"))
        assert senior["salary_min"] == 190978.0
        assert senior["salary_max"] == 231050.0
        assert senior["salary_currency"] == "USD"

    def test_a_row_with_no_stated_pay_reports_none(self):
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        flutter = next(j for j in jobs if j["title"] == "Flutter Developer")
        # Absent rather than None: `_salary` returns {} so a card that says
        # nothing about pay leaves the column alone instead of blanking it.
        assert flutter.get("salary_min") is None
        assert flutter.get("salary_max") is None

    def test_the_work_type_the_search_filtered_on_is_kept(self):
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        by_title = {job["title"]: job for job in jobs}
        assert by_title["Senior Software Engineer, Platform"]["is_remote"] is True
        assert by_title["Flutter Developer"]["is_remote"] is False

    def test_a_slug_only_named_in_the_view_path_is_still_captured(self):
        """
        The compounding half. `publicUrl` here is `ifit.com/careers?gh_jid=...`
        — it names the job but not the company, so on its own it buys one
        posting. `viewJobPath` names the slug, and a slug is that company's
        whole board on every future fetch cycle, by API, forever.
        """
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        mobile = next(j for j in jobs if j["title"] == "Mobile Engineer")
        assert mobile["url"] == "https://www.ifit.com/careers?gh_jid=4777000111"
        assert mobile["apply_url"] == \
            "https://job-boards.greenhouse.io/ifit/jobs/4777000111"

    def test_the_derived_url_is_one_the_ats_shortcut_can_read(self):
        # Which is the other half of what it buys: a description by free API
        # call rather than a scrape of somebody's bespoke careers page.
        from app.services.enrichment import looks_like_ats

        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        mobile = next(j for j in jobs if j["title"] == "Mobile Engineer")
        assert not looks_like_ats(mobile["url"])
        assert looks_like_ats(mobile["apply_url"])

    def test_no_apply_url_when_it_would_just_repeat_the_listing_url(self):
        # `_target_url` prefers apply_url; setting it to the same address only
        # makes enrichment look like it had a choice it did not have.
        jobs = harvest.extract_jobs(GREENHOUSE_BOARD, source="greenhouse_harvest")
        senior = next(j for j in jobs if j["title"].startswith("Senior"))
        assert "apply_url" not in senior

    def test_a_view_path_that_is_not_a_greenhouse_job_path_is_ignored(self):
        # Guessing a URL is worse than having none: a wrong apply_url is what
        # enrichment would try *first*.
        assert harvest._greenhouse_board_url({"viewJobPath": "/companies/acme"}) == ""
        assert harvest._greenhouse_board_url({"viewJobPath": ""}) == ""
        assert harvest._greenhouse_board_url({}) == ""

    def test_the_derived_url_reaches_the_stored_job(self, db):
        from app.services.harvest import save_harvested_jobs

        save_harvested_jobs(db, harvest.extract_jobs(
            GREENHOUSE_BOARD, source="greenhouse_harvest"))

        job = db.query(Job).filter(Job.source_job_id == "4777000111").one()
        assert job.apply_url == \
            "https://job-boards.greenhouse.io/ifit/jobs/4777000111"

    def test_an_apply_url_we_already_resolved_is_not_replaced(self, db):
        """
        A resolved apply URL is the end of a redirect chain we followed once and
        would rather not follow again. A card's guess at the same thing is not
        an improvement on it.
        """
        from app.services.harvest import save_harvested_jobs

        make_job(
            db,
            source="greenhouse_harvest",
            url="https://www.ifit.com/careers?gh_jid=4777000111",
            title="Mobile Engineer",
            company="iFIT",
            location="Logan, UT",
            source_job_id="4777000111",
            apply_url="https://www.ifit.com/careers/apply/4777000111",
        )
        save_harvested_jobs(db, harvest.extract_jobs(
            GREENHOUSE_BOARD, source="greenhouse_harvest"))

        job = db.query(Job).filter(Job.source_job_id == "4777000111").one()
        assert job.apply_url == "https://www.ifit.com/careers/apply/4777000111"

    def test_the_slug_becomes_a_board_worth_fetching_from(self, db):
        """
        End to end on the reason the user wanted this board: our Greenhouse
        fetcher can only read companies whose slug it knows, and this board
        lists every company on the platform.
        """
        from app.services.harvest import save_harvested_jobs

        counts = save_harvested_jobs(db, harvest.extract_jobs(
            GREENHOUSE_BOARD, source="greenhouse_harvest"))
        assert counts["boards"] >= 1

        from app.models.company_board import CompanyBoard

        slugs = {row.slug for row in db.query(CompanyBoard).all()}
        assert {"ifit", "acme", "corporatecareers"} <= slugs

    def test_the_board_is_harvested_under_its_own_source(self):
        assert harvest.source_for_url(
            "https://my.greenhouse.io/jobs/search?query=software%20engineer"
        ) == "greenhouse_harvest"

    def test_a_stored_job_carries_the_band_and_the_employer_url(self, db):
        from app.services.harvest import save_harvested_jobs

        save_harvested_jobs(db, harvest.extract_jobs(
            GREENHOUSE_BOARD, source="greenhouse_harvest"))

        job = db.query(Job).filter(Job.source_job_id == "4956068101").one()
        assert job.url == \
            "https://job-boards.greenhouse.io/corporatecareers/jobs/4956068101"
        assert job.salary_min == 190978.0
        assert job.source == "greenhouse_harvest"
        # Empty, and expected to be: enrichment fills it from the ATS API.
        assert not (job.description or "")


class TestPayWrittenAsProse:
    """
    Parsing a stated range. Confined to keys that only ever hold pay, so a
    number in a title cannot be mistaken for a salary.
    """

    def test_a_plain_dollar_range(self):
        assert harvest._salary_from_text("$85,000 - $100,000") == {
            "salary_min": 85000.0, "salary_max": 100000.0,
            "salary_currency": "USD"}

    def test_thousands_shorthand(self):
        assert harvest._salary_from_text("£60k – £75k") == {
            "salary_min": 60000.0, "salary_max": 75000.0,
            "salary_currency": "GBP"}

    def test_the_word_to_reads_as_a_range(self):
        found = harvest._salary_from_text("€50,000 to €65,000 per year")
        assert found["salary_min"] == 50000.0
        assert found["salary_max"] == 65000.0
        assert found["salary_currency"] == "EUR"

    def test_a_backwards_range_is_put_the_right_way_round(self):
        found = harvest._salary_from_text("$120,000 - $90,000")
        assert found["salary_min"] == 90000.0
        assert found["salary_max"] == 120000.0

    def test_a_lone_figure_is_the_floor_not_the_ceiling(self):
        # Matching `_salary`'s reading, so a filter on the top of the band does
        # not silently exclude a job that only stated one number.
        assert harvest._salary_from_text("From $150,000") == {
            "salary_min": 150000.0, "salary_max": None,
            "salary_currency": "USD"}

    def test_prose_with_no_money_in_it_yields_nothing(self):
        assert harvest._salary_from_text("Competitive salary") == {}
        assert harvest._salary_from_text("") == {}
        assert harvest._salary_from_text(None) == {}

    def test_numbers_that_are_not_money_are_left_alone(self):
        assert harvest._salary_from_text("Competitive") == {}

    def test_a_range_with_no_money_marker_is_not_a_salary(self):
        """
        A range of anything reads as money without this. "2 to 5 years" parses
        perfectly well as 2–5, and a band of 2 in the salary columns is worse
        than an empty one — it is a number someone might act on.

        The cost is real but small: an unmarked "120,000 - 150,000" is skipped
        too. Every board seen so far states the currency.
        """
        assert harvest._salary_from_text("2 to 5 years experience") == {}
        assert harvest._salary_from_text("3-5 years") == {}
        assert harvest._salary_from_text("Salary: 120,000 - 150,000") == {}

    def test_thousands_shorthand_counts_as_a_money_marker(self):
        # No symbol, but `k` after a five-figure-shaped number is not ambiguous.
        assert harvest._salary_from_text("60k - 75k") == {
            "salary_min": 60000.0, "salary_max": 75000.0,
            "salary_currency": None}

    def test_the_word_to_has_to_be_the_word(self):
        # `[-to]+` as a character class would match any letter out of t/o, so a
        # stray letter between two figures became a range.
        assert harvest._salary_from_text("$50,000t$60,000") == {
            "salary_min": 50000.0, "salary_max": None,
            "salary_currency": "USD"}

    def test_structured_pay_still_wins_over_prose(self):
        """
        A stated min/max is the better reading — it needs no parsing and cannot
        be misread. The prose parser is the fallback, not the first choice.
        """
        found = harvest._salary({
            "compensation": {"minSalary": 100000, "maxSalary": 120000,
                             "currencyCode": "usd"},
            "payRanges": "$1 - $2",
        })
        assert found["salary_min"] == 100000.0
        assert found["salary_max"] == 120000.0
        assert found["salary_currency"] == "USD"


class TestABoardThatRedirectsToAnotherHost:
    """
    Hiring Cafe is queued as `hiring.cafe` and the page that actually loads is
    `hiringcafe.com`. Everything keyed on the host — the reader's registration,
    the source name, the board's own depth — applied to the URL we asked for
    and not the one that rendered, so the board opened, scrolled, and forwarded
    nothing for as long as nobody looked at it.
    """

    def test_both_spellings_are_the_same_source(self):
        assert harvest.source_for_url("https://hiring.cafe/") == "hiringcafe_harvest"
        assert harvest.source_for_url(
            "https://hiringcafe.com/jobs") == "hiringcafe_harvest"

    def test_the_redirect_target_is_not_filed_under_linkedin(self):
        # The fallback is LinkedIn's bucket, which is exactly where an
        # unrecognised host's yield goes to become uncountable.
        assert harvest.source_for_url("https://hiringcafe.com/x") != \
            harvest.HARVEST_SOURCE

    def test_the_board_recognises_the_host_it_lands_on(self):
        from app.services.browse_plan import board_for

        assert board_for("https://hiring.cafe/").key == "hiringcafe"
        assert board_for("https://hiringcafe.com/jobs").key == "hiringcafe"

    def test_the_boards_depth_survives_the_redirect(self):
        # `_max_pages` on the landing URL has to give the same answer as on the
        # queued one, or the click-through stops the moment the page is real.
        from app.services.browse_plan import _max_pages

        assert _max_pages("https://hiringcafe.com/") == _max_pages("https://hiring.cafe/")

    def test_an_unrelated_host_is_still_unrelated(self):
        from app.services.browse_plan import board_for

        assert board_for("https://hiringcafe.example.org/") is None
