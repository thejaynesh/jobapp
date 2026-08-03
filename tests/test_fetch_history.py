from datetime import datetime, timedelta, timezone

from app.models.fetch_run import FetchRun, FetchSourceRun
from app.services.fetch_history import (
    prune,
    recent_runs,
    record_run,
    source_totals,
)

_NOW = datetime.now(timezone.utc)


def _counts(fetched=10, inserted=3, merged=1, skipped=6, stale=0):
    return {"fetched": fetched, "inserted": inserted, "merged": merged,
            "skipped": skipped, "stale": stale}


def _sources(**overrides):
    base = {
        "linkedin": {"count": 10, "errors": [], "enabled": True},
        "indeed": {"count": 0, "errors": ["403 Forbidden"], "enabled": True},
        "handshake": {"count": 0, "errors": [], "enabled": False},
    }
    base.update(overrides)
    return base


class TestRecordRun:
    def test_stores_the_run_and_a_row_per_source(self, db):
        run = record_run(db, _NOW, _counts(), _sources())
        db.flush()
        assert run.fetched == 10
        assert run.inserted == 3
        assert {s.source for s in run.sources} == {"linkedin", "indeed", "handshake"}

    def test_computes_duration(self, db):
        run = record_run(db, _NOW - timedelta(seconds=42), _counts(), _sources())
        assert run.duration_seconds >= 42

    def test_a_blocked_source_makes_the_run_partial(self, db):
        run = record_run(db, _NOW, _counts(), _sources())
        assert run.status == "partial"

    def test_all_healthy_is_ok(self, db):
        run = record_run(db, _NOW, _counts(),
                         {"linkedin": {"count": 5, "errors": [], "enabled": True}})
        assert run.status == "ok"

    def test_a_disabled_source_does_not_make_the_run_partial(self, db):
        run = record_run(db, _NOW, _counts(), {
            "linkedin": {"count": 5, "errors": [], "enabled": True},
            "handshake": {"count": 0, "errors": ["no cookie"], "enabled": False},
        })
        assert run.status == "ok"

    def test_a_cycle_level_error_is_recorded_as_failed(self, db):
        run = record_run(db, _NOW, _counts(), {}, error="adapters exploded")
        assert run.status == "failed"
        assert run.error == "adapters exploded"

    def test_per_source_status_is_classified(self, db):
        run = record_run(db, _NOW, _counts(), _sources())
        db.flush()
        by_source = {s.source: s.status for s in run.sources}
        assert by_source["linkedin"] == "ok"
        assert by_source["indeed"] == "failed"
        assert by_source["handshake"] == "disabled"

    def test_records_what_each_source_actually_contributed(self, db):
        """Fetched vs new is the whole point of tracking this per source."""
        run = record_run(
            db, _NOW, _counts(), _sources(),
            per_source_outcome={"linkedin": {"inserted": 2, "merged": 1,
                                            "skipped": 7, "stale": 0}},
        )
        db.flush()
        linkedin = next(s for s in run.sources if s.source == "linkedin")
        assert linkedin.fetched == 10
        assert linkedin.inserted == 2
        assert linkedin.skipped == 7

    def test_stores_queries_locations_and_link_stats(self, db):
        run = record_run(
            db, _NOW, _counts(), _sources(),
            queries=["swe", "backend"], locations=["NYC"],
            resolve_stats={"attempted": 9, "resolved": 7, "failed": 2},
        )
        assert run.queries == ["swe", "backend"]
        assert run.locations == ["NYC"]
        assert (run.links_attempted, run.links_resolved, run.links_failed) == (9, 7, 2)

    def test_stores_board_activity(self, db):
        run = record_run(
            db, _NOW, _counts(), _sources(),
            board_stats={"discovered": 4, "sniffed": 2,
                         "registry": {"greenhouse": {"active": 30},
                                      "lever": {"active": 12}}},
        )
        assert run.boards_discovered == 4
        assert run.boards_sniffed == 2
        assert run.boards_polled == 42

    def test_a_source_missing_from_stats_still_gets_attribution(self, db):
        """Jobs we processed must never end up unattributed to their source."""
        run = record_run(
            db, _NOW, _counts(), {},
            per_source_outcome={"remotive": {"inserted": 3, "merged": 0,
                                             "skipped": 1, "stale": 0}},
        )
        db.flush()
        remotive = next(s for s in run.sources if s.source == "remotive")
        assert remotive.inserted == 3
        assert remotive.skipped == 1

    def test_caps_stored_error_messages(self, db):
        run = record_run(db, _NOW, _counts(), {
            "dice": {"count": 0, "errors": [f"err {i}" for i in range(20)],
                     "enabled": True},
        })
        db.flush()
        assert len(run.sources[0].errors) == 5


class TestRecentRuns:
    def test_returns_newest_first(self, db):
        for i in range(3):
            record_run(db, _NOW - timedelta(hours=i), _counts(), _sources())
        db.flush()
        runs = recent_runs(db, 10)
        assert runs[0].started_at > runs[-1].started_at

    def test_respects_the_limit(self, db):
        for i in range(5):
            record_run(db, _NOW - timedelta(hours=i), _counts(), _sources())
        db.flush()
        assert len(recent_runs(db, 2)) == 2

    def test_empty_history(self, db):
        assert recent_runs(db) == []


class TestPrune:
    def test_keeps_only_the_retention_window(self, db):
        for i in range(8):
            record_run(db, _NOW - timedelta(hours=i), _counts(), _sources(),
                       retention=1000)
        db.flush()
        prune(db, retention=3)
        db.flush()
        assert db.query(FetchRun).count() == 3

    def test_pruning_removes_the_source_rows_too(self, db):
        for i in range(4):
            record_run(db, _NOW - timedelta(hours=i), _counts(), _sources(),
                       retention=1000)
        db.flush()
        prune(db, retention=1)
        db.flush()
        assert db.query(FetchSourceRun).count() == 3  # one run × three sources

    def test_keeps_the_newest_runs(self, db):
        for i in range(5):
            record_run(db, _NOW - timedelta(hours=i), _counts(fetched=100 - i),
                       _sources(), retention=1000)
        db.flush()
        prune(db, retention=2)
        db.flush()
        assert {r.fetched for r in db.query(FetchRun).all()} == {100, 99}

    def test_nothing_to_prune_is_a_no_op(self, db):
        record_run(db, _NOW, _counts(), _sources())
        db.flush()
        assert prune(db, retention=50) == 0

    def test_retention_of_zero_is_ignored_rather_than_wiping_history(self, db):
        record_run(db, _NOW, _counts(), _sources())
        db.flush()
        assert prune(db, retention=0) == 0
        assert db.query(FetchRun).count() == 1


class TestSourceTotals:
    def test_rolls_up_across_runs(self, db):
        for i in range(3):
            record_run(
                db, _NOW - timedelta(hours=i), _counts(), _sources(),
                per_source_outcome={"linkedin": {"inserted": 2, "merged": 0,
                                                 "skipped": 8, "stale": 0}},
            )
        db.flush()
        totals = {t["source"]: t for t in source_totals(db)}
        assert totals["linkedin"]["runs"] == 3
        assert totals["linkedin"]["fetched"] == 30
        assert totals["linkedin"]["inserted"] == 6

    def test_counts_failed_runs_per_source(self, db):
        for i in range(2):
            record_run(db, _NOW - timedelta(hours=i), _counts(), _sources())
        db.flush()
        totals = {t["source"]: t for t in source_totals(db)}
        assert totals["indeed"]["failed_runs"] == 2
        assert totals["linkedin"]["failed_runs"] == 0

    def test_best_contributors_come_first(self, db):
        record_run(
            db, _NOW, _counts(), _sources(),
            per_source_outcome={"linkedin": {"inserted": 5, "merged": 0,
                                             "skipped": 0, "stale": 0}},
        )
        db.flush()
        assert source_totals(db)[0]["source"] == "linkedin"

    def test_empty_history(self, db):
        assert source_totals(db) == []
