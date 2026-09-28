"""Opening /runs must never start its minutes-long backlog scan inline."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest.mock import MagicMock, Mock

import pytest

from app.services import enrichment_history as history
from app.tasks.enrich import refresh_backlog


COUNTS = {"thin": 1234, "waiting": 123, "rescuable": 12}


class Cache:
    def __init__(self):
        self.values = {}
        self.guard = Lock()
        self.execution_lock = Mock()
        self.execution_lock.acquire.return_value = True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        with self.guard:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def lock(self, *args, **kwargs):
        return self.execution_lock


@pytest.fixture
def cache(monkeypatch):
    cache = Cache()
    monkeypatch.setattr(history, "_backlog_client", lambda: cache)
    return cache


@pytest.fixture
def publish(monkeypatch):
    publish = Mock()
    monkeypatch.setattr(refresh_backlog, "apply_async", publish)
    return publish


def test_warm_cache_needs_no_worker(cache, publish):
    cache.values[history.BACKLOG_KEY] = json.dumps(COUNTS)
    assert history.backlog_snapshot() == {**COUNTS, "stale": False}
    publish.assert_not_called()


def test_simultaneous_cold_requests_queue_one_refresh(cache, publish):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: history.backlog_snapshot(), range(20)))

    assert results == [{"unavailable": True}] * 20
    publish.assert_called_once_with(retry=False, expires=history.BACKLOG_TTL_SECONDS)


def test_expired_counts_remain_visible_while_refresh_is_queued(cache, publish):
    cache.values[history.BACKLOG_LAST_KEY] = json.dumps(COUNTS)
    assert history.backlog_snapshot() == {**COUNTS, "stale": True}
    publish.assert_called_once()


def test_successful_count_survives_fresh_cache_expiry(cache, publish, monkeypatch):
    pipeline = MagicMock()
    pipeline.__enter__.return_value = pipeline
    pipeline.setex.side_effect = lambda key, ttl, value: cache.set(key, value, ex=ttl)
    monkeypatch.setattr(cache, "pipeline", lambda: pipeline, raising=False)

    history._store_backlog(COUNTS)
    pipeline.execute.assert_called_once()
    ttls = {call.args[0]: call.args[1] for call in pipeline.setex.call_args_list}
    assert ttls[history.BACKLOG_LAST_KEY] > ttls[history.BACKLOG_KEY]
    assert history.backlog_snapshot() == {**COUNTS, "stale": False}
    publish.assert_not_called()

    del cache.values[history.BACKLOG_KEY]  # The fresh entry's TTL expires first.
    assert history.backlog_snapshot() == {**COUNTS, "stale": True}
    publish.assert_called_once()


def test_publish_failure_preserves_counts_and_does_not_retry_each_page(cache, publish):
    cache.values[history.BACKLOG_LAST_KEY] = json.dumps(COUNTS)
    publish.side_effect = ConnectionError("broker unavailable")
    for _ in range(3):
        assert history.backlog_snapshot() == {**COUNTS, "stale": True}
    publish.assert_called_once()


def test_unavailable_cache_does_not_fall_back_to_counting(publish, monkeypatch):
    count = Mock(side_effect=AssertionError("page queried the backlog"))
    monkeypatch.setattr(history, "backlog", count)
    # The suite's default cache fixture rejects all Redis access.
    assert history.backlog_snapshot() == {"unavailable": True}
    count.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("payload", ["bad json", "[]", '{"thin": 3}', '{"thin": -1}'])
def test_invalid_cached_counts_are_unavailable(cache, publish, payload):
    cache.values[history.BACKLOG_KEY] = payload
    assert history.backlog_snapshot() == {"unavailable": True}


def test_worker_skips_a_duplicate_refresh_after_another_finished(cache, monkeypatch):
    cache.values[history.BACKLOG_KEY] = json.dumps(COUNTS)
    session = Mock(side_effect=AssertionError("unnecessary database connection"))
    monkeypatch.setattr("app.tasks.enrich.SessionLocal", session)
    assert refresh_backlog.run() == COUNTS
    session.assert_not_called()
    cache.execution_lock.release.assert_called_once()


def test_worker_skips_while_another_refresh_is_running(cache, monkeypatch):
    cache.execution_lock.acquire.return_value = False
    session = Mock(side_effect=AssertionError("overlapping scan"))
    monkeypatch.setattr("app.tasks.enrich.SessionLocal", session)
    assert refresh_backlog.run() == {"skipped_reason": "already running"}
    session.assert_not_called()
    cache.execution_lock.release.assert_not_called()


def test_worker_does_not_scan_when_redis_cannot_protect_it(monkeypatch):
    session = Mock(side_effect=AssertionError("unprotected scan"))
    monkeypatch.setattr("app.tasks.enrich.SessionLocal", session)
    assert refresh_backlog.run() == {"skipped_reason": "cache unavailable"}
    session.assert_not_called()


def test_worker_limits_database_work_and_closes_its_session(cache, monkeypatch):
    session = Mock()
    monkeypatch.setattr("app.tasks.enrich.SessionLocal", lambda: session)
    count = Mock(return_value=COUNTS)
    monkeypatch.setattr(history, "backlog", count)
    assert refresh_backlog.run() == COUNTS
    statements = [str(call.args[0]) for call in session.execute.call_args_list]
    assert statements == [
        "SET LOCAL max_parallel_workers_per_gather = 0",
        "SET LOCAL statement_timeout = '180s'",
    ]
    count.assert_called_once_with(session, refresh=True)
    session.close.assert_called_once()
    cache.execution_lock.release.assert_called_once()


def test_failed_worker_releases_resources_and_preserves_last_count(cache, monkeypatch):
    cache.values[history.BACKLOG_LAST_KEY] = json.dumps(COUNTS)
    session = Mock()
    monkeypatch.setattr("app.tasks.enrich.SessionLocal", lambda: session)
    monkeypatch.setattr(history, "backlog", Mock(side_effect=RuntimeError("query failed")))
    with pytest.raises(RuntimeError, match="query failed"):
        refresh_backlog.run()
    session.close.assert_called_once()
    cache.execution_lock.release.assert_called_once()
    assert history._read_backlog(history.BACKLOG_LAST_KEY) == COUNTS


def test_refresh_uses_the_batch_queue():
    route = refresh_backlog.app.amqp.router.route({}, refresh_backlog.name)
    assert route["queue"].name == "batch"


def test_page_renders_when_counts_are_unavailable(client, cache, publish, monkeypatch):
    monkeypatch.setattr(history, "backlog", Mock(side_effect=AssertionError("inline scan")))
    response = client.get("/runs")
    assert response.status_code == 200
    assert "Backlog counts are not available yet" in response.text
    publish.assert_called_once()


def test_page_labels_old_counts(client, cache, publish):
    cache.values[history.BACKLOG_LAST_KEY] = json.dumps(COUNTS)
    response = client.get("/runs")
    assert response.status_code == 200
    assert "Showing the last available backlog counts" in response.text
    assert "1,234" in response.text
