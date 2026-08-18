"""
Showing a stored instant in the timezone the reader lives in.

Everything here is stored as timezone-aware UTC, which is the only thing worth
storing. Every page then printed it raw, so a fetch at half past four in the
afternoon read as `Aug 18 23:53` and the Runs page needed mental arithmetic.

The tests that matter are the ones about not making the data worse: storage is
untouched, and a page that cannot render one timestamp shows it badly rather
than failing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import timefmt


@pytest.fixture(autouse=True)
def _clear_zone_cache():
    timefmt._cache.clear()
    yield
    timefmt._cache.clear()


# 2026-08-18 23:53 UTC is 16:53 Pacific — the exact case from the Runs page.
UTC_EVENING = datetime(2026, 8, 18, 23, 53, tzinfo=timezone.utc)
# January, so Pacific is on standard time rather than daylight.
UTC_WINTER = datetime(2026, 1, 15, 20, 30, tzinfo=timezone.utc)


class TestConversion:
    def test_an_evening_utc_timestamp_reads_as_afternoon(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.when(UTC_EVENING, "%b %d %H:%M") == "Aug 18 16:53"

    def test_daylight_saving_is_handled_rather_than_assumed(self, monkeypatch):
        # A fixed -8 would be an hour wrong for two thirds of the year, which is
        # the reason this stores a zone name and not an offset.
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.when(UTC_EVENING, "%H:%M") == "16:53"   # PDT, UTC-7
        assert timefmt.when(UTC_WINTER, "%H:%M") == "12:30"    # PST, UTC-8

    def test_the_zone_is_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "Asia/Kolkata")
        assert timefmt.when(UTC_EVENING, "%b %d %H:%M") == "Aug 19 05:23"

    def test_a_naive_timestamp_is_read_as_utc(self, monkeypatch):
        # Every column in this schema is timezone-aware; a naive value came
        # from a hand-built object, and guessing local time would shift it.
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.when(UTC_EVENING.replace(tzinfo=None), "%H:%M") == "16:53"

    def test_the_instant_itself_never_moves(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.local_time(UTC_EVENING) == UTC_EVENING


class TestItNeverBreaksAPage:
    def test_a_missing_timestamp_renders_empty(self, monkeypatch):
        # Rather than "'NoneType' has no attribute 'strftime'", which is what
        # the bare .strftime() calls did and which took the whole page down.
        assert timefmt.when(None) == ""

    def test_a_non_datetime_passes_through(self):
        assert timefmt.when("not a time") == "not a time"
        assert timefmt.local_time("not a time") == "not a time"

    def test_an_unknown_zone_falls_back_to_utc(self, monkeypatch):
        # A typo in a setting should cost a log line and an unconverted time,
        # not every page in the app.
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "Mars/Olympus_Mons")
        assert timefmt.when(UTC_EVENING, "%H:%M") == "23:53"

    def test_an_empty_setting_uses_the_default(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "")
        assert timefmt.when(UTC_EVENING, "%H:%M") == "16:53"

    def test_a_broken_pattern_still_prints_something(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.when(UTC_EVENING, "%Q") not in ("", None)


class TestTheLabel:
    def test_it_names_the_current_abbreviation(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.label() in ("PST", "PDT")

    def test_it_survives_a_broken_zone(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "Mars/Olympus_Mons")
        assert timefmt.label() == "UTC"


class TestOnThePages:
    """The filter has to actually be installed on every environment."""

    def _job(self, db):
        import uuid

        from app.models.job import Job, JobStatus

        job = Job(
            source="greenhouse", source_urls=["https://x/1"],
            title="Backend Engineer", company="Acme", url="https://x/1",
            description="d" * 300, status=JobStatus.filtered_out,
            filter_reason="low_score", fetched_at=UTC_EVENING,
            posted_at=UTC_EVENING, dedupe_hash=uuid.uuid4().hex,
        )
        db.add(job)
        db.commit()
        return job

    def test_the_jobs_page_shows_local_time(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        timefmt._cache.clear()
        self._job(db)

        body = client.get("/jobs").text
        assert "Aug 18, 2026" in body

    @pytest.mark.parametrize("path", ["/jobs", "/apps", "/runs", "/funnel",
                                      "/llm", "/outreach", "/settings",
                                      "/profile"])
    def test_every_page_still_renders(self, client, db, path):
        # The filter is registered per template environment, and there are
        # eleven of them. A router that missed the factory would raise
        # "No filter named 'when'" the moment it rendered a date.
        assert client.get(path).status_code == 200


class TestDatesAreNotInstants:
    """
    Sources report a posting *date*. It arrives as midnight UTC because a
    timestamp is the only column there is — and converting that to Pacific
    renders 1 August as 31 July, which is not a timezone being helpful.
    """

    MIDNIGHT_UTC = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def test_a_stated_date_does_not_shift(self, monkeypatch):
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.on(self.MIDNIGHT_UTC, "%b %d, %Y") == "Aug 01, 2026"

    def test_the_instant_filter_would_have_shifted_it(self, monkeypatch):
        # The behaviour `on` exists to avoid, asserted so the difference between
        # the two filters cannot quietly disappear.
        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        assert timefmt.when(self.MIDNIGHT_UTC, "%b %d, %Y") == "Jul 31, 2026"

    def test_a_missing_date_renders_empty(self):
        assert timefmt.on(None) == ""

    def test_the_jobs_page_shows_the_posted_date_as_stated(self, client, db,
                                                           monkeypatch):
        import uuid

        from app.models.job import Job, JobStatus

        monkeypatch.setattr(settings, "DISPLAY_TIMEZONE", "America/Los_Angeles")
        timefmt._cache.clear()
        db.add(Job(
            source="greenhouse", source_urls=["https://x/posted"],
            title="Backend Engineer", company="Acme", url="https://x/posted",
            description="d" * 300, status=JobStatus.new,
            fetched_at=UTC_EVENING, posted_at=self.MIDNIGHT_UTC,
            dedupe_hash=uuid.uuid4().hex,
        ))
        db.commit()

        assert "Posted: Aug 01, 2026" in client.get("/jobs").text
