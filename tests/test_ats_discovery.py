from app.services.ats_discovery import (
    MAX_SLUGS_PER_ATS,
    discover_ats_slugs,
    merged_slugs,
)


def _job(url="", description="", source="jsearch"):
    return {"url": url, "description": description, "source": source}


class TestDiscoverAtsSlugs:
    def test_discovers_greenhouse_from_url(self):
        jobs = [_job(url="https://boards.greenhouse.io/stripe/jobs/123")]
        assert discover_ats_slugs(jobs)["greenhouse"] == ["stripe"]

    def test_discovers_job_boards_greenhouse_variant(self):
        jobs = [_job(url="https://job-boards.greenhouse.io/airbnb/jobs/456")]
        assert discover_ats_slugs(jobs)["greenhouse"] == ["airbnb"]

    def test_discovers_from_description(self):
        jobs = [_job(description='Apply at <a href="https://jobs.lever.co/netflix/abc">here</a>')]
        assert discover_ats_slugs(jobs)["lever"] == ["netflix"]

    def test_discovers_all_ats_kinds(self):
        jobs = [_job(description="""
            https://jobs.ashbyhq.com/linear/x
            https://jobs.smartrecruiters.com/Databricks/1234
            https://apply.workable.com/acme-co/j/ABC/
            https://widgetcorp.recruitee.com/o/backend-dev
        """)]
        result = discover_ats_slugs(jobs)
        assert result["ashby"] == ["linear"]
        assert result["smartrecruiters"] == ["databricks"]
        assert result["workable"] == ["acme-co"]
        assert result["recruitee"] == ["widgetcorp"]

    def test_discovers_greenhouse_from_the_embed_widget(self):
        """The form careers pages actually use: an iframe with ?for=<slug>."""
        jobs = [_job(description=(
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmecorp">'
        ))]
        assert discover_ats_slugs(jobs)["greenhouse"] == ["acmecorp"]

    def test_discovers_from_ats_api_endpoints(self):
        jobs = [_job(description="""
            https://api.lever.co/v0/postings/netflix?mode=json
            https://api.ashbyhq.com/posting-api/job-board/openai
            https://api.smartrecruiters.com/v1/companies/Bosch/postings
        """)]
        result = discover_ats_slugs(jobs)
        assert result["lever"] == ["netflix"]
        assert result["ashby"] == ["openai"]
        assert result["smartrecruiters"] == ["bosch"]

    def test_discovers_from_a_resolved_apply_url(self):
        """Aggregator listings only reveal the ATS once the redirect is followed."""
        jobs = [{
            "source": "adzuna",
            "url": "https://www.adzuna.com/land/ad/123",
            "apply_url": "https://jobs.lever.co/acme/abc",
            "description": "",
        }]
        assert discover_ats_slugs(jobs)["lever"] == ["acme"]

    def test_blocklist_filters_non_company_segments(self):
        jobs = [_job(description="https://boards.greenhouse.io/embed/job_board?for=acme "
                                 "https://apply.workable.com/api/v1/widget")]
        result = discover_ats_slugs(jobs)
        assert "embed" not in result.get("greenhouse", [])
        assert "api" not in result.get("workable", [])

    def test_jobs_from_ats_sources_do_not_rediscover(self):
        jobs = [_job(url="https://boards.greenhouse.io/stripe/jobs/1", source="greenhouse")]
        assert discover_ats_slugs(jobs) == {}

    def test_merges_with_existing_without_duplicates(self):
        existing = {"greenhouse": ["stripe"]}
        jobs = [_job(url="https://boards.greenhouse.io/stripe/jobs/1"),
                _job(url="https://boards.greenhouse.io/airbnb/jobs/2")]
        result = discover_ats_slugs(jobs, existing)
        assert result["greenhouse"] == ["stripe", "airbnb"]

    def test_respects_per_ats_cap(self):
        existing = {"lever": [f"co{i}" for i in range(MAX_SLUGS_PER_ATS)]}
        jobs = [_job(url="https://jobs.lever.co/onemore/x")]
        result = discover_ats_slugs(jobs, existing)
        assert len(result["lever"]) == MAX_SLUGS_PER_ATS
        assert "onemore" not in result["lever"]

    def test_ignores_unknown_keys_in_existing(self):
        result = discover_ats_slugs([], {"bogus": ["x"], "lever": ["a"]})
        assert result == {"lever": ["a"]}


class TestMergedSlugs:
    def test_configured_first_then_discovered(self):
        merged = merged_slugs("stripe, airbnb", {"greenhouse": ["netflix"]}, "greenhouse")
        assert merged == ["stripe", "airbnb", "netflix"]

    def test_dedupes_case_insensitively(self):
        merged = merged_slugs("Stripe", {"greenhouse": ["stripe", "acme"]}, "greenhouse")
        assert merged == ["Stripe", "acme"]

    def test_empty_inputs(self):
        assert merged_slugs("", None, "greenhouse") == []
        assert merged_slugs("", {}, "lever") == []


class TestHarvestFromLists:
    def _resp(self, text):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.text = text
        resp.raise_for_status = MagicMock()
        return resp

    def test_harvests_slugs_from_list_document(self):
        from unittest.mock import patch
        from app.services.ats_discovery import harvest_slugs_from_lists
        doc = """
        | Company | Link |
        | Stripe | [Apply](https://boards.greenhouse.io/stripe/jobs/1) |
        | OpenAI | [Apply](https://jobs.ashbyhq.com/openai/x) |
        | Nvidia | [Apply](https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US/SWE_1) |
        """
        with patch("app.services.ats_discovery.httpx.get", return_value=self._resp(doc)):
            result = harvest_slugs_from_lists(["https://example.com/list.md"])
        assert result["greenhouse"] == ["stripe"]
        assert result["ashby"] == ["openai"]
        assert result["workday"] == ["nvidia:wd5:NVIDIAExternalCareerSite"]

    def test_merges_into_existing_and_survives_url_errors(self):
        from unittest.mock import patch
        import httpx as _httpx
        from app.services.ats_discovery import harvest_slugs_from_lists
        doc = "https://jobs.lever.co/netflix/1"
        with patch("app.services.ats_discovery.httpx.get",
                   side_effect=[_httpx.HTTPError("down"), self._resp(doc)]):
            result = harvest_slugs_from_lists(
                ["https://dead.example/a.md", "https://ok.example/b.md"],
                existing={"lever": ["palantir"]},
            )
        assert result["lever"] == ["palantir", "netflix"]

    def test_respects_per_ats_discovery_caps(self):
        from unittest.mock import patch
        from app.services.ats_discovery import harvest_slugs_from_lists, DISCOVERY_CAPS
        doc = "\n".join(
            f"https://t{i}.wd1.myworkdayjobs.com/Site{i}/job/x" for i in range(40)
        )
        with patch("app.services.ats_discovery.httpx.get", return_value=self._resp(doc)):
            result = harvest_slugs_from_lists(["https://example.com/list.md"])
        assert len(result["workday"]) == DISCOVERY_CAPS["workday"]
