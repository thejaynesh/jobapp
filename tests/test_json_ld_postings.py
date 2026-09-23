"""
schema.org JobPosting, the format job pages embed for search engines.

Wellfound's pages ship one per posting, and every one was dropped: the company
is an object and there is no URL, because the block describes the page it is
on. A model asked to learn it answered correctly — no url field — and the
recipe was refused for finding no jobs. These are read from Wellfound's own
payload, trimmed.
"""

from app.models.harvest_recipe import HarvestRecipe, HarvestSample
from app.services import harvest_recipes
from app.services.harvest import extract_jobs

PAGE = "https://wellfound.com/jobs/4666110-full-stack-engineer"

POSTING = {
    "@type": "JobPosting",
    "@context": "http://schema.org/",
    "title": "Full-stack Engineer",
    "datePosted": "2026-09-03T06:54:49Z",
    "identifier": {"name": "AIKOCorp", "@type": "PropertyValue", "value": "4666110"},
    "description": "<p>This is a high-ownership startup role on a three-person team.</p>",
    "jobLocation": [{"@type": "Place", "address": {
        "@type": "PostalAddress", "addressRegion": "Capital Region of Denmark",
        "addressCountry": "Denmark", "addressLocality": "Copenhagen"}}],
    "employmentType": "FULL_TIME",
    "jobLocationType": "TELECOMMUTE",
    "hiringOrganization": {"name": "AIKOCorp", "@type": "Organization",
                           "sameAs": "https://aikocorp.ai"},
    "applicantLocationRequirements": [{"name": "Estonia", "@type": "Country"},
                                      {"name": "Spain", "@type": "Country"}],
}

ONSITE_WITH_PAY = {
    "@type": "JobPosting",
    "title": "Full Stack Engineer",
    "identifier": {"name": "Employvision", "value": "4664589"},
    "description": "<p>SaaS startup experience</p>",
    "baseSalary": {"@type": "MonetaryAmount", "currency": "USD", "value": {
        "@type": "QuantitativeValue", "maxValue": 250000, "minValue": 175000,
        "unitText": "YEAR"}},
    "jobLocation": [{"address": {"addressLocality": "New York",
                                 "addressRegion": "New York",
                                 "addressCountry": "United States"}}],
    "hiringOrganization": {"name": "Employvision"},
}


class TestTheBuiltInReader:
    def test_a_posting_page_is_read_with_the_page_as_its_link(self):
        (job,) = extract_jobs(POSTING, source="wellfound_harvest", page_url=PAGE)
        assert job["title"] == "Full-stack Engineer"
        assert job["company"] == "AIKOCorp"
        assert job["url"] == PAGE
        assert job["source_job_id"] == "4666110"
        assert job["is_remote"] and job["location"] == "Remote (Estonia, Spain)"

    def test_without_the_page_the_id_builds_the_link(self):
        (job,) = extract_jobs(POSTING, source="wellfound_harvest")
        assert job["url"] == "https://wellfound.com/jobs/4666110"

    def test_location_and_pay_are_read(self):
        (job,) = extract_jobs(ONSITE_WITH_PAY, source="wellfound_harvest", page_url=PAGE)
        assert job["location"] == "New York, New York, United States"
        assert (job["salary_min"], job["salary_max"], job["salary_currency"]) == \
            (175000, 250000, "USD")
        assert not job["is_remote"]

    def test_a_page_listing_several_does_not_give_them_all_its_address(self):
        other = {**POSTING, "identifier": {"value": "999"}, "title": "Data Engineer"}
        jobs = extract_jobs({"@graph": [POSTING, other]}, source="x_harvest",
                            page_url="https://x.test/search")
        assert jobs == []  # no link of their own and no id template for x


class TestTheRecipeReader:
    def test_a_numeric_path_step_is_an_index(self):
        recipe = {"roots": [""], "fields": {
            "title": ["title"], "company": ["hiringOrganization.name"],
            "location": ["jobLocation.0.address.addressLocality"],
            "id": ["identifier.value"]}}
        (job,) = harvest_recipes.apply_recipe(POSTING, recipe, "w", page_url=PAGE)
        assert job["location"] == "Copenhagen"
        assert job["url"] == PAGE

    def test_the_models_own_answer_now_validates(self):
        recipe = {"roots": [""], "fields": {
            "title": ["title"], "company": ["hiringOrganization.name"],
            "description": ["description"], "url": [], "id": ["identifier.value"]}}
        outcome = harvest_recipes.validate([POSTING, ONSITE_WITH_PAY], recipe,
                                           page_urls=[PAGE, PAGE + "-2"])
        assert outcome["ok"] and outcome["jobs"] == 2


class TestLearnOnAHostTheReaderNowHandles:
    def test_learn_says_no_recipe_is_needed_and_clears_the_host(self, db):
        db.add(HarvestSample(host="wellfound.com", source_url=PAGE,
                             payload=POSTING, bytes=100, found=0))
        db.commit()
        out = harvest_recipes.learn(db, "wellfound.com")
        assert out["ok"] and "already reads" in out["reason"]
        assert db.query(HarvestSample).count() == 0
        assert db.query(HarvestRecipe).count() == 0
