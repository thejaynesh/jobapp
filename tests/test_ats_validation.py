from unittest.mock import patch

from app.services.ats_validation import (
    candidate_slugs,
    resolve_slug,
    validate_configured_slugs,
)


class TestCandidateSlugs:
    def test_strips_legal_suffixes_and_spaces(self):
        cands = candidate_slugs("Stripe Inc")
        assert cands[0] == "stripe"

    def test_generates_hyphen_and_joined_variants(self):
        cands = candidate_slugs("Acme Robotics")
        assert "acmerobotics" in cands
        assert "acme-robotics" in cands
        assert "acme" in cands

    def test_no_duplicate_of_original(self):
        assert "stripe" not in candidate_slugs("stripe") or candidate_slugs("stripe") == []


class TestResolveSlug:
    def test_returns_slug_when_already_valid(self):
        with patch("app.services.ats_validation.is_valid_slug", return_value=True) as mock_v:
            assert resolve_slug("greenhouse", "stripe") == "stripe"
        mock_v.assert_called_once()

    def test_fixes_via_candidates(self):
        def fake_valid(ats, slug):
            return slug == "stripe"
        with patch("app.services.ats_validation.is_valid_slug", side_effect=fake_valid):
            assert resolve_slug("greenhouse", "Stripe Inc") == "stripe"

    def test_none_when_unfixable(self):
        with patch("app.services.ats_validation.is_valid_slug", return_value=False):
            assert resolve_slug("greenhouse", "notacompany") is None


class TestValidateConfiguredSlugs:
    def test_valid_fixed_and_invalid_are_reported(self):
        def fake_valid(ats, slug):
            return slug in ("stripe", "airbnb")
        with patch("app.services.ats_validation.is_valid_slug", side_effect=fake_valid):
            valid, cache, report = validate_configured_slugs(
                {"greenhouse": ["stripe", "Airbnb Inc", "totally wrong co"]}, None
            )
        assert valid["greenhouse"] == ["stripe", "airbnb"]
        assert report["greenhouse"]["fixed"] == {"Airbnb Inc": "airbnb"}
        assert report["greenhouse"]["invalid"] == ["totally wrong co"]
        assert cache["greenhouse"]["stripe"] == "stripe"
        assert cache["greenhouse"]["Airbnb Inc"] == "airbnb"
        assert cache["greenhouse"]["totally wrong co"] is None

    def test_cache_prevents_reprobing(self):
        cache = {"greenhouse": {"stripe": "stripe", "badco": None}}
        with patch("app.services.ats_validation.is_valid_slug") as mock_v:
            valid, _, report = validate_configured_slugs(
                {"greenhouse": ["stripe", "badco"]}, cache
            )
        mock_v.assert_not_called()
        assert valid["greenhouse"] == ["stripe"]
        assert report["greenhouse"]["invalid"] == ["badco"]

    def test_probe_network_error_keeps_slug(self):
        # network trouble must not mark a slug invalid
        from app.services.ats_validation import is_valid_slug
        with patch("app.services.ats_validation._probe_greenhouse", side_effect=Exception("timeout")):
            assert is_valid_slug("greenhouse", "stripe") is True


class TestSeedsAndAssembly:
    def test_seed_lists_have_expected_shape(self):
        from app.services.ats_seeds import SEED_ATS_SLUGS
        assert len(SEED_ATS_SLUGS["greenhouse"]) >= 30
        assert len(SEED_ATS_SLUGS["ashby"]) >= 15
        for spec in SEED_ATS_SLUGS["workday"]:
            assert len(spec.split(":")) == 3

    def _cfg(self, **overrides):
        class Cfg:
            GREENHOUSE_COMPANY_SLUGS = ""
            LEVER_COMPANY_SLUGS = ""
            ASHBY_COMPANY_SLUGS = ""
            SMARTRECRUITERS_COMPANY_SLUGS = ""
            WORKABLE_COMPANY_SLUGS = ""
            RECRUITEE_COMPANY_SLUGS = ""
            WORKDAY_TENANTS = ""
            ATS_SEED_COMPANIES = True
        cfg = Cfg()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_build_merges_configured_seeds_discovered(self):
        from app.services.ats_discovery import build_ats_slugs
        cfg = self._cfg(GREENHOUSE_COMPANY_SLUGS="mycompany")
        result = build_ats_slugs(cfg, discovered={"greenhouse": ["foundco"]})
        gh = result["greenhouse"]
        assert gh[0] == "mycompany"       # configured first
        assert "stripe" in gh             # seeds present
        assert "foundco" in gh            # discovered last

    def test_build_respects_seed_toggle(self):
        from app.services.ats_discovery import build_ats_slugs
        result = build_ats_slugs(self._cfg(ATS_SEED_COMPANIES=False))
        assert result["greenhouse"] == []

    def test_build_uses_validated_configured_when_given(self):
        from app.services.ats_discovery import build_ats_slugs
        cfg = self._cfg(GREENHOUSE_COMPANY_SLUGS="Stripe Inc", ATS_SEED_COMPANIES=False)
        result = build_ats_slugs(cfg, validated_configured={"greenhouse": ["stripe"]})
        assert result["greenhouse"] == ["stripe"]

    def test_build_caps_total(self):
        from app.services.ats_discovery import build_ats_slugs, MAX_TOTAL_SLUGS_PER_ATS
        many = ",".join(f"co{i}" for i in range(100))
        result = build_ats_slugs(self._cfg(GREENHOUSE_COMPANY_SLUGS=many))
        assert len(result["greenhouse"]) == MAX_TOTAL_SLUGS_PER_ATS


class TestWorkdayDiscovery:
    def test_extracts_tenant_host_site_from_url(self):
        from app.services.ats_discovery import discover_ats_slugs
        jobs = [{"source": "jsearch", "url": "", "description":
                 "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/SWE_JR1"}]
        result = discover_ats_slugs(jobs)
        assert result["workday"] == ["nvidia:wd5:NVIDIAExternalCareerSite"]

    def test_extracts_without_locale_segment(self):
        from app.services.ats_discovery import discover_ats_slugs
        jobs = [{"source": "hnhiring", "url":
                 "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/x", "description": ""}]
        result = discover_ats_slugs(jobs)
        assert result["workday"] == ["salesforce:wd12:External_Career_Site"]

    def test_skips_non_site_segments(self):
        from app.services.ats_discovery import discover_ats_slugs
        jobs = [{"source": "jsearch", "url":
                 "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme", "description": ""}]
        assert "workday" not in discover_ats_slugs(jobs) or \
            all(not s.endswith(":wday") for s in discover_ats_slugs(jobs).get("workday", []))
