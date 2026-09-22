import logging

from app.services.source_diagnostics import (
    MAX_MESSAGES_PER_SOURCE,
    SourceLogCapture,
    classify,
    merge_into_stats,
)


def _log(source: str, message: str, level: int = logging.ERROR) -> None:
    logging.getLogger(f"app.services.sources.{source}").log(level, message)


class TestSourceLogCapture:
    def test_files_a_message_under_its_source(self):
        with SourceLogCapture() as capture:
            _log("indeed", "Indeed RSS fetch error: 403 Forbidden")
        assert capture.messages == {
            "indeed": ["Indeed RSS fetch error: 403 Forbidden"]
        }

    def test_captures_warnings_as_well_as_errors(self):
        with SourceLogCapture() as capture:
            _log("wellfound", "no job cards found", level=logging.WARNING)
        assert capture.messages["wellfound"] == ["no job cards found"]

    def test_ignores_chatter_below_warning(self):
        with SourceLogCapture() as capture:
            _log("linkedin", "fetched 10 jobs", level=logging.INFO)
            _log("linkedin", "debugging", level=logging.DEBUG)
        assert capture.messages == {}

    def test_ignores_loggers_outside_the_sources_package(self):
        with SourceLogCapture() as capture:
            logging.getLogger("app.services.matcher").error("unrelated failure")
        assert capture.messages == {}

    def test_ignores_the_shared_helper_logger(self):
        """`base` is a helper, not a source — its bucket would go nowhere."""
        with SourceLogCapture() as capture:
            _log("base", "some helper problem")
        assert capture.messages == {}

    def test_deduplicates_repeated_messages(self):
        with SourceLogCapture() as capture:
            for _ in range(4):
                _log("dice", "Dice: page load failed: timeout")
        assert capture.messages["dice"] == ["Dice: page load failed: timeout"]

    def test_caps_messages_per_source(self):
        with SourceLogCapture() as capture:
            for i in range(MAX_MESSAGES_PER_SOURCE + 5):
                _log("jooble", f"failure {i}")
        assert len(capture.messages["jooble"]) == MAX_MESSAGES_PER_SOURCE

    def test_formats_lazy_log_arguments(self):
        with SourceLogCapture() as capture:
            logging.getLogger("app.services.sources.adzuna").error(
                "Adzuna fetch error (%s p%d): %s", "us", 1, "boom"
            )
        assert capture.messages["adzuna"] == ["Adzuna fetch error (us p1): boom"]

    def test_stops_capturing_once_the_block_exits(self):
        with SourceLogCapture() as capture:
            pass
        _log("indeed", "after the fact")
        assert capture.messages == {}

    def test_removes_its_handler_even_when_the_block_raises(self):
        root = logging.getLogger("app.services.sources")
        before = len(root.handlers)
        try:
            with SourceLogCapture():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert len(root.handlers) == before


class TestMergeIntoStats:
    def test_attaches_the_reason_a_source_returned_nothing(self):
        stats = {"indeed": {"count": 0, "errors": [], "enabled": True}}
        merge_into_stats(stats, {"indeed": ["403 Forbidden"]})
        assert stats["indeed"]["errors"] == ["403 Forbidden"]

    def test_a_warning_on_a_productive_source_is_not_an_error(self):
        """A warning on a source that still delivered isn't worth alarming over."""
        stats = {"linkedin": {"count": 12, "errors": [], "enabled": True}}
        merge_into_stats(stats, {"linkedin": ["one search was throttled"]})
        assert stats["linkedin"]["errors"] == []
        assert stats["linkedin"]["warnings"] == ["one search was throttled"]
        assert classify(stats["linkedin"]) == "ok"

    def test_a_failed_request_on_a_productive_source_makes_it_partial(self):
        """
        One working board used to hide every broken one: the errors were
        dropped whenever the source returned any jobs at all.
        """
        stats = {"greenhouse": {"count": 40, "errors": [], "enabled": True}}
        merge_into_stats(stats, {"greenhouse": ["acme: 500", "throttled"]},
                         {"greenhouse": ["acme: 500"]})
        assert stats["greenhouse"]["errors"] == ["acme: 500"]
        assert stats["greenhouse"]["warnings"] == ["throttled"]
        assert classify(stats["greenhouse"]) == "partial"

    def test_the_capture_separates_errors_from_warnings(self):
        import logging

        with SourceLogCapture() as capture:
            logging.getLogger("app.services.sources.greenhouse").warning("slow")
            logging.getLogger("app.services.sources.greenhouse").error("acme: 500")
        assert capture.messages["greenhouse"] == ["slow", "acme: 500"]
        assert capture.errors["greenhouse"] == ["acme: 500"]

    def test_ignores_sources_absent_from_the_stats(self):
        stats = {}
        merge_into_stats(stats, {"ghost": ["nope"]})
        assert stats == {}

    def test_does_not_duplicate_an_already_recorded_error(self):
        stats = {"dice": {"count": 0, "errors": ["timeout"], "enabled": True}}
        merge_into_stats(stats, {"dice": ["timeout", "blocked"]})
        assert stats["dice"]["errors"] == ["timeout", "blocked"]


class TestClassify:
    def test_a_blocked_source_is_failed_not_ok(self):
        """The bug this all exists for: 0 jobs plus a reason is a failure."""
        assert classify({"count": 0, "errors": ["403"], "enabled": True}) == "failed"

    def test_zero_jobs_with_no_reason_is_merely_empty(self):
        assert classify({"count": 0, "errors": [], "enabled": True}) == "empty"

    def test_jobs_with_no_errors_is_ok(self):
        assert classify({"count": 5, "errors": [], "enabled": True}) == "ok"

    def test_jobs_alongside_errors_is_partial(self):
        assert classify({"count": 5, "errors": ["one failed"], "enabled": True}) == "partial"

    def test_disabled_wins_over_everything(self):
        assert classify({"count": 0, "errors": ["x"], "enabled": False}) == "disabled"
