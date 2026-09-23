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


ZIP_PAGE = "https://www.ziprecruiter.com/jobs-search/4?days=7&search=Software"
ZIP_LIST = {
    "@type": "ItemList", "@context": "https://schema.org", "numberOfItems": 20,
    "itemListElement": [
        {"url": "https://www.ziprecruiter.com/c/Lancesoft-INC/Job/Software-Quality-Engineer-II/-in-Lafayette,CO?jid=4c007d00ffffbc8c",
         "name": "Software Quality Engineer II", "@type": "ListItem", "position": "1"},
        {"url": "https://www.ziprecruiter.com/c/Robert-Half/Job/RPA-Software-Engineer/-in-Auburn-Hills,MI?jid=5ee554d2d1397c49",
         "name": "RPA Software Engineer", "@type": "ListItem", "position": "3"},
    ],
}
ZIP_HOME = {"sections": [
    {"title": "For you", "subtitle": "Our top picks for you",
     "jobs": [{"jobTitle": "Backend Engineer", "companyName": "Acme", "jid": "x1"}]},
    {"title": "Remote jobs", "subtitle": "Jobs that allow you to work remotely", "jobs": []},
]}


class TestZipRecruiterSearchResults:
    """Its search pages list results as name + link; the link names the employer."""

    def test_company_location_and_id_come_from_the_link(self):
        jobs = extract_jobs(ZIP_LIST, source="ziprecruiter_harvest", page_url=ZIP_PAGE)
        first = next(j for j in jobs if j["source_job_id"] == "4c007d00ffffbc8c")
        assert first["company"] == "Lancesoft INC"
        assert first["title"] == "Software Quality Engineer II"
        assert first["location"] == "Lafayette, CO"
        assert first["url"].startswith("https://www.ziprecruiter.com/c/Lancesoft-INC/")
        assert len(jobs) == 2

    def test_other_boards_do_not_guess_from_links(self):
        assert extract_jobs(ZIP_LIST, source="indeed_harvest") == []

    def test_learn_on_the_search_pages_needs_no_model(self, db):
        db.add(HarvestSample(host="www.ziprecruiter.com", source_url=ZIP_PAGE,
                             payload=ZIP_LIST, bytes=100, found=0))
        db.commit()
        out = harvest_recipes.learn(db, "www.ziprecruiter.com")
        assert out["ok"] and "already reads" in out["reason"] and out["jobs"] == 2

    def test_section_headings_are_not_offered_as_job_titles(self):
        found = harvest_recipes.title_candidates([ZIP_HOME])
        assert "For you" not in found and "Our top picks for you" not in found
        assert found == ["Backend Engineer"]


INDEED_GQL = [{"data": {"findRelevantJobs": {"results": [
    {"job": {
        "key": "4c5d7354200380a9",
        "title": "Attorney - Foreclosure & Litigation",
        "benefits": [{"key": "4ZN8U", "label": "Wellness program"}],
        "employer": {"key": "ddce6c3200287688", "dossier": {"images": {}}},
        "location": {"city": "San Juan Capistrano",
                     "formatted": {"long": "San Juan Capistrano, CA 92675",
                                   "short": "San Juan Capistrano, CA"}},
        "tracking": {"jobClick": {"url": "http://www.indeed.com/pagead/clk?mo=r&ad=x"}},
        "__typename": "Job",
        "compensation": {"baseSalary": {"rangeMinor": {"min": "13000000", "__typename": "RangeMinor"},
                                        "unitOfWork": "YEAR"},
                         "currencyCode": "USD", "formattedText": "$130,000 - $150,000 a year"},
        "sourceEmployerName": "Alterra Assessment Recovery",
    }},
    {"job": {
        "key": "6289a951f628a4bd",
        "title": "Broadcom Cluster / Licensing Technician",
        "compensation": {"baseSalary": {"rangeMinor": {"min": "6500", "__typename": "RangeMinor"},
                                        "unitOfWork": "HOUR"}, "currencyCode": "USD"},
        "sourceEmployerName": "EMR CPR LLC",
    }},
]}}}]

HIRING_CAFE = {"props": {"pageProps": {"ssrHits": [{
    "id": "higherme___dunkindonuts___67eea3f7b062e",
    "objectID": "higherme___dunkindonuts___67eea3f7b062e",
    "apply_url": "https://app.higherme.com/jobs/67eea3f7b062e",
    "attributed_org": {"name": "Dunkin'"},
    "job_information": {"title": "Dunkin Assistant Manager", "job_title_raw": "Dunkin Assistant Manager"},
    "enriched_company_data": {"name": "Dunkin'"},
    "v5_processed_job_data": {"company_name": "Dunkin' - Franchisee Of Dunkin Donuts",
                              "workplace_type": "Onsite",
                              "workplace_cities": ["Omaha, Nebraska, US"]},
}]}}}


class TestIndeedGraphQL:
    def test_jobs_are_read_with_their_employer_and_key(self):
        jobs = extract_jobs(INDEED_GQL, source="indeed_harvest")
        by_key = {j["source_job_id"]: j for j in jobs}
        attorney = by_key["4c5d7354200380a9"]
        assert attorney["company"] == "Alterra Assessment Recovery"
        assert attorney["url"] == "https://www.indeed.com/viewjob?jk=4c5d7354200380a9"

    def test_pay_in_cents_is_dollars_and_hourly_is_left_out(self):
        by_key = {j["source_job_id"]: j for j in extract_jobs(INDEED_GQL, source="indeed_harvest")}
        assert by_key["4c5d7354200380a9"]["salary_min"] == 130000
        assert "salary_min" not in by_key["6289a951f628a4bd"]

    def test_its_titles_are_offered(self):
        found = harvest_recipes.title_candidates(INDEED_GQL)
        assert "Attorney - Foreclosure & Litigation" in found


class TestHiringCafe:
    def test_a_hit_is_read(self):
        (job,) = extract_jobs(HIRING_CAFE, source="hiringcafe_harvest")
        assert job["title"] == "Dunkin Assistant Manager"
        assert job["company"] == "Dunkin' - Franchisee Of Dunkin Donuts"
        assert job["url"] == "https://app.higherme.com/jobs/67eea3f7b062e"
        assert job["location"] == "Omaha, Nebraska, US"

    def test_its_title_is_offered(self):
        assert harvest_recipes.title_candidates([HIRING_CAFE]) == ["Dunkin Assistant Manager"]


class TestAHostWithAWorkingRecipeLeavesTheList:
    def test_learn_clears_samples_the_active_recipe_reads(self, db):
        payload = {"results": [{"jobTitle": "Platform Engineer", "org": {"label": "Acme"},
                                "link": "https://board.test/j/1"}]}
        db.add(HarvestSample(host="board.test", source_url="https://board.test/s",
                             payload=payload, bytes=100, found=0))
        harvest_recipes.save(db, "board.test", {
            "roots": ["results"], "fields": {"title": ["jobTitle"], "company": ["org.label"],
                                             "url": ["link"]}}, {"ok": True})
        out = harvest_recipes.learn(db, "board.test")
        assert out["ok"] and "active recipe already reads" in out["reason"]
        assert db.query(HarvestSample).count() == 0
