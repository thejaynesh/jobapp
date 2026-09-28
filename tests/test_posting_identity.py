"""
One posting, however a source wrote its URL, is stored once.

Measured on 2026-09-28 against the same postings read from SimplifyJobs and
from each board's own API: 90 of 97 Lever and 99 of 110 Ashby postings were
stored twice, because SimplifyJobs links `…/apply` and `…/application` and
rewrites titles. The examples below are those postings.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.archived_job import ArchivedJob
from app.models.job import Job
from app.services import deduplication, job_fetcher, posting_identity
from tests.test_fetch_task import _make_profile_with_targets, _std_job

LEVER = "https://jobs.lever.co/acme-robotics/5cde0d09-ba2d-408d-947e-4a42028cd4f7"
ASHBY = "https://jobs.ashbyhq.com/applied/a837cbd6-9fe4-4d74-a2dc-84f602c40694"
GH_EMBED = "https://boards.greenhouse.io/embed/job_app?token=5215629007"


@pytest.mark.parametrize("url,expected", [
    (LEVER + "/apply", LEVER),
    ("https://jobs.lever.co/ACME-Robotics/5CDE0D09-ba2d-408d-947e-4a42028cd4f7", LEVER),
    ("https://jobs.eu.lever.co/acme/5cde0d09-ba2d-408d-947e-4a42028cd4f7/apply",
     "https://jobs.eu.lever.co/acme/5cde0d09-ba2d-408d-947e-4a42028cd4f7"),
    (ASHBY + "/application?utm_source=Simplify", ASHBY),
    ("https://boards.greenhouse.io/andurilindustries/jobs/5215629007?gh_jid=5215629007", GH_EMBED),
    ("https://job-boards.greenhouse.io/andurilindustries/jobs/5215629007", GH_EMBED),
    ("https://www.anduril.com/open-roles/software-engineer?gh_jid=5215629007", GH_EMBED),
    ("https://boards-api.greenhouse.io/v1/boards/andurilindustries/jobs/5215629007", GH_EMBED),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
     "Software-Engineer_JR1998/apply?source=Simplify",
     "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
     "Software-Engineer_JR1998"),
    ("https://jobs.smartrecruiters.com/Visa/744000012345678-software-engineer",
     "https://jobs.smartrecruiters.com/visa/744000012345678"),
    ("https://apply.workable.com/acme/j/ab12cd34ef/apply", "https://apply.workable.com/acme/j/AB12CD34EF"),
    ("https://jobs.apple.com/en-gb/details/200123456/software-engineer?team=SFTWR",
     "https://jobs.apple.com/en-us/details/200123456"),
    ("https://careers.tiktok.com/position/7412345678901234567/detail",
     "https://lifeattiktok.com/search/7412345678901234567"),
    ("https://careers-acme.icims.com/jobs/12345/software-engineer/job?in_iframe=1",
     "https://careers-acme.icims.com/jobs/12345/job"),
    ("https://koch.avature.net/en_US/careers/JobDetail/Engineer-Process/189050",
     "https://koch.avature.net/careers/JobDetail/189050"),
])
def test_each_ats_has_one_address_per_posting(url, expected):
    assert posting_identity.canonical(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/4012345678",
    "https://acme.com/careers",
    "https://boards.greenhouse.io/acme",
    "",
    None,
])
def test_a_url_it_cannot_place_is_left_as_written(url):
    assert posting_identity.canonical(url) is None


def test_an_apply_link_counts_only_by_its_posting():
    """A careers page every posting shares must not fold them into one."""
    assert posting_identity.urls("https://x.example/1", "https://acme.com/careers") \
        == ["https://x.example/1"]
    assert posting_identity.urls("https://x.example/1", LEVER + "/apply") \
        == ["https://x.example/1", LEVER]


# --- Through the save path ---------------------------------------------------

def cycle(db, jobs):
    def run(*args, **kwargs):
        return list(jobs), {}

    with patch("app.services.query_expansion.expand_search_queries",
               return_value=(["Software Engineer"], None)), \
         patch("app.services.job_fetcher._run_all_adapters", side_effect=run):
        return job_fetcher.fetch_and_save_jobs(db)


def simplify_row(**kw):
    return _std_job(source="simplify", source_job_id="s-1", company="Acme Robotics",
                    title="Software Engineer Intern - Perception/Computer Vision",
                    location="San Jose, CA", url=LEVER + "/apply", description="", **kw)


def lever_posting(**kw):
    fields = {"source": "lever", "source_job_id": "5cde0d09-ba2d-408d-947e-4a42028cd4f7",
              "company": "acme-robotics", "location": "San Jose, CA", "url": LEVER,
              "title": "New Grads 2027 - Software Engineer - Perception/Computer Vision",
              "description": "Build perception systems. " * 20}
    return _std_job(**{**fields, **kw})


@pytest.fixture
def profile(db):
    _make_profile_with_targets(db)


def test_a_list_row_and_the_boards_posting_are_one_job(db, profile):
    cycle(db, [simplify_row()])
    cycle(db, [lever_posting()])
    [job] = db.query(Job).all()
    assert job.source == "simplify" and "Build perception" in job.description
    assert LEVER + "/apply" in job.source_urls and LEVER in job.source_urls


def test_either_order(db, profile):
    cycle(db, [lever_posting()])
    counts = cycle(db, [simplify_row()])
    assert db.query(Job).count() == 1 and counts["inserted"] == 0


def test_the_apply_link_of_an_aggregator_posting_joins_too(db, profile):
    cycle(db, [lever_posting()])
    cycle(db, [_std_job(source="linkedin", source_job_id="li-9", company="Acme Robotics Corp",
                        title="Perception Engineer", location="San Jose",
                        url="https://www.linkedin.com/jobs/view/9", apply_url=LEVER + "/apply")])
    assert db.query(Job).count() == 1


def test_a_row_stored_before_canonical_addresses_catches_up_when_seen_again(db, profile):
    cycle(db, [simplify_row()])
    old = db.query(Job).one()
    old.source_urls = [LEVER + "/apply"]          # as rows were written until now
    db.commit()
    cycle(db, [simplify_row()])                   # its own source lists it again
    assert LEVER in db.query(Job).one().source_urls
    cycle(db, [lever_posting()])
    assert db.query(Job).count() == 1


def test_an_archived_posting_is_recognised_by_its_address(db, profile):
    db.add(ArchivedJob(id=uuid.uuid4(), source="simplify", source_job_id="s-1",
                       source_urls=[LEVER + "/apply", LEVER], url=LEVER + "/apply",
                       dedupe_hash="gone", title="Software Engineer", company="Acme Robotics",
                       fetched_at=datetime.now(timezone.utc)))
    db.commit()
    counts = cycle(db, [lever_posting()])
    assert counts["inserted"] == 0 and db.query(Job).count() == 0


def test_different_postings_stay_apart(db, profile):
    other = LEVER.replace("5cde0d09", "6cde0d09")
    cycle(db, [lever_posting()])
    cycle(db, [lever_posting(source_job_id="6cde0d09-ba2d-408d-947e-4a42028cd4f7", url=other,
                             title="Software Engineer - Planning")])
    assert db.query(Job).count() == 2


def test_the_lookup_is_by_any_address(db):
    db.add(Job(source="lever", source_urls=[LEVER], title="t", company="c", url=LEVER,
               dedupe_hash="h1", fetched_at=datetime.now(timezone.utc)))
    db.commit()
    found = deduplication.find_existing_job(db, "simplify", LEVER + "/apply", "s-1", "h2")
    assert found is not None and found.url == LEVER
