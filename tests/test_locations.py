from app.services.locations import (
    adzuna_countries,
    describe_prefs,
    jobicy_geos,
    location_allowed,
    normalize_prefs,
    search_locations,
)


def _prefs(regions=None, remote_ok=True, custom=None):
    return {"regions": regions or [], "remote_ok": remote_ok, "custom": custom or []}


class TestNormalizePrefs:
    def test_structured_prefs_win(self):
        data = {"location_preferences": {"regions": ["usa", "bogus"], "remote_ok": False,
                                         "custom": ["Dubai"]},
                "target_locations": ["Remote"]}
        prefs = normalize_prefs(data)
        assert prefs == {"regions": ["usa"], "remote_ok": False, "custom": ["Dubai"]}

    def test_legacy_text_parsed(self):
        prefs = normalize_prefs({"target_locations": ["United States", "London", "Remote", "Dubai"]})
        assert prefs["regions"] == ["usa", "uk"]
        assert prefs["remote_ok"] is True
        assert prefs["custom"] == ["Dubai"]

    def test_legacy_filler_means_no_restriction(self):
        prefs = normalize_prefs({"target_locations": ["Open to all locations", "Relocation OK"]})
        assert prefs["regions"] == []
        assert prefs["custom"] == []
        assert prefs["remote_ok"] is True

    def test_empty_profile(self):
        prefs = normalize_prefs({})
        assert prefs["regions"] == []


class TestSearchLocations:
    def test_regions_produce_search_strings(self):
        locs = search_locations(_prefs(regions=["usa", "canada", "uk"]))
        assert "United States" in locs
        assert "Canada" in locs
        assert "London, United Kingdom" in locs
        assert "Remote" in locs

    def test_custom_included_and_capped(self):
        locs = search_locations(_prefs(regions=["usa", "uk", "europe"],
                                       custom=["Dubai", "Singapore", "Tokyo"]))
        assert len(locs) <= 8

    def test_every_region_gets_a_primary_search_even_at_cap(self):
        regions = ["usa", "canada", "uk", "europe", "india", "australia", "new_zealand"]
        locs = search_locations(_prefs(regions=regions))
        from app.services.locations import REGIONS
        for r in regions:
            assert REGIONS[r]["search"][0] in locs, r

    def test_fallback_when_empty(self):
        assert search_locations(_prefs(remote_ok=False)) == ["Remote", "United States"]


class TestAdapterTargeting:
    def test_adzuna_countries(self):
        assert adzuna_countries(_prefs(regions=["usa", "canada", "uk"])) == ["us", "ca", "gb"]
        assert adzuna_countries(_prefs()) == ["us"]

    def test_adzuna_europe_expands_to_multiple_countries(self):
        countries = adzuna_countries(_prefs(regions=["europe", "new_zealand"]))
        assert "de" in countries and "nl" in countries and "nz" in countries

    def test_jobicy_geos(self):
        assert jobicy_geos(_prefs(regions=["usa", "uk"])) == ["usa", "uk"]
        assert jobicy_geos(_prefs(regions=["new_zealand"])) == ["new-zealand"]
        assert jobicy_geos(_prefs()) == [None]


class TestLocationAllowed:
    def test_no_restriction_returns_none(self):
        assert location_allowed("Berlin, Germany", False, _prefs()) is None

    def test_remote_job_allowed_when_remote_ok(self):
        assert location_allowed("", True, _prefs(regions=["usa"])) is True

    def test_us_state_abbrev_allowed(self):
        assert location_allowed("Austin, TX", False, _prefs(regions=["usa"])) is True

    def test_canada_city_allowed(self):
        assert location_allowed("Toronto, Ontario", False, _prefs(regions=["canada"])) is True

    def test_london_allowed_for_uk(self):
        assert location_allowed("London", False, _prefs(regions=["uk"])) is True

    def test_other_region_rejected(self):
        assert location_allowed("Bengaluru, India", False, _prefs(regions=["usa", "canada", "uk"])) is False
        assert location_allowed("Berlin, Germany", False, _prefs(regions=["usa"])) is False

    def test_unknown_location_undecided(self):
        assert location_allowed("Springfield", False, _prefs(regions=["usa"])) is None

    def test_custom_location_allowed(self):
        assert location_allowed("Dubai, UAE", False, _prefs(regions=["usa"], custom=["Dubai"])) is True

    def test_remote_text_allowed(self):
        assert location_allowed("Remote (Worldwide)", False, _prefs(regions=["usa"])) is True

    def test_ca_abbrev_does_not_match_canada_word(self):
        # "CA" the state code must not fire inside lowercase words
        assert location_allowed("Casablanca, Morocco", False, _prefs(regions=["usa"])) is None

    def test_empty_location_undecided(self):
        assert location_allowed("", False, _prefs(regions=["usa"])) is None


class TestDescribePrefs:
    def test_readable_summary(self):
        text = describe_prefs(_prefs(regions=["usa", "uk"], custom=["Dubai"]))
        assert "United States" in text and "United Kingdom" in text and "Dubai" in text and "Remote" in text

    def test_no_restriction(self):
        assert describe_prefs(_prefs(remote_ok=False)) == "No restriction"


class TestNewRegions:
    def test_nz_city_allowed(self):
        assert location_allowed("Auckland", False, _prefs(regions=["new_zealand"])) is True

    def test_nz_abbrev_allowed(self):
        assert location_allowed("Wellington, NZ", False, _prefs(regions=["new_zealand"])) is True

    def test_european_cities_allowed(self):
        for loc in ("Berlin, Germany", "Milan, Italy", "Prague", "Helsinki, Finland"):
            assert location_allowed(loc, False, _prefs(regions=["europe"])) is True, loc

    def test_nz_rejected_when_not_selected(self):
        assert location_allowed("Auckland, New Zealand", False, _prefs(regions=["usa"])) is False
