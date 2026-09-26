"""
Glassdoor through the browser.

It answers a server with a bot check, so it is read the way Indeed and
ZipRecruiter are: the extension opens its search, and the reader takes the
results out of the page's own API responses. Two things were missing — the
"Show more jobs" button (scrolling alone stopped at the first thirty) and the
names its search cards use for the employer and the pay.

The card below is the shape of Glassdoor's search results (`jobListings` ->
`jobview.header`), trimmed.
"""

from app.services import browse_plan
from app.services.harvest import extract_jobs

CARD = {"data": {"jobListings": {"jobListings": [{"jobview": {
    "header": {
        "employerNameFromSearch": "Acme Robotics",
        "employer": {"id": 123, "name": "Acme Robotics Inc", "shortName": "Acme"},
        "jobTitleText": "Senior Software Engineer",
        "locationName": "San Jose, CA",
        "seoJobLink": "/job-listing/senior-software-engineer-acme-JV_IC1147436_KO0,24_KE25,29.htm?jl=1009123456789",
        "payPeriod": "ANNUAL",
        "payCurrency": "USD",
        "payPeriodAdjustedPay": {"p10": 165000, "p50": 190000, "p90": 215000},
        "ageInDays": 2,
    },
    "job": {"listingId": 1009123456789, "jobTitleText": "Senior Software Engineer",
            "descriptionFragmentsText": ["Build robots.", "Python and C++."]},
}}, {"jobview": {
    "header": {
        "employerNameFromSearch": "Confidential",
        "employer": None,
        "jobTitleText": "Backend Engineer",
        "locationName": "Remote",
        "seoJobLink": "/job-listing/backend-engineer-JV_KO0,16.htm?jl=1009987654321",
        "payPeriod": "HOURLY",
        "payPeriodAdjustedPay": {"p10": 60, "p50": 70, "p90": 80},
    },
}}]}}}


class TestReadingItsSearch:
    def test_cards_become_jobs_with_absolute_links(self):
        jobs = {j["title"]: j for j in extract_jobs(CARD, source="glassdoor_harvest")}
        senior = jobs["Senior Software Engineer"]
        assert senior["company"] == "Acme Robotics"
        assert senior["location"] == "San Jose, CA"
        assert senior["url"].startswith("https://www.glassdoor.com/job-listing/")

    def test_the_annual_band_is_kept_and_an_hourly_one_is_not(self):
        jobs = {j["title"]: j for j in extract_jobs(CARD, source="glassdoor_harvest")}
        senior = jobs["Senior Software Engineer"]
        assert (senior["salary_min"], senior["salary_max"], senior["salary_currency"]) == \
            (165000, 215000, "USD")
        assert "salary_min" not in jobs["Backend Engineer"]

    def test_a_confidential_employer_still_has_a_name(self):
        jobs = {j["title"]: j for j in extract_jobs(CARD, source="glassdoor_harvest")}
        assert jobs["Backend Engineer"]["company"] == "Confidential"


class TestCrawlingIt:
    URL = "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=python&locKeyword=Boston"

    def test_the_crawl_presses_show_more(self):
        assert browse_plan._max_pages(self.URL) == 10
        assert browse_plan._click_selector(self.URL) == "button[data-test='load-more']"

    def test_a_learned_recipe_still_wins(self, db):
        from app.services import crawl_recipes

        crawl_recipes.save(db, "www.glassdoor.com",
                           {"mode": "click", "selector": "button.more", "max_pages": 4},
                           {"ok": True})
        assert browse_plan._click_selector(self.URL, db) == "button.more"
        assert browse_plan._max_pages(self.URL, db) == 4
