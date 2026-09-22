from app.celery_app import celery_app


def test_celery_app_configured():
    assert celery_app.conf.broker_url is not None
    assert "redis" in celery_app.conf.broker_url
    assert celery_app.conf.result_backend is not None


def test_ping_task():
    from app.celery_app import ping
    result = ping.apply()
    assert result.result == "pong"


class TestNoTaskIsRedeliveredWhileItRuns:
    """
    With late acks, Redis hands a task to another worker once it has gone
    `visibility_timeout` without an ack. The default was an hour and board
    fetches run for about four, so every long fetch was re-delivered mid-run.
    Every late-acked task has to finish inside the timeout; anything that can
    run longer has to opt out of late acks.
    """

    # Late-acked with no declared limit, and known to take seconds.
    _QUICK = {
        "app.tasks.generate.sweep_generations",
        "app.tasks.generate.refresh_stale_docs",
        "app.tasks.providers.prune_llm_log",
        "app.tasks.providers.prune_agent_history",
    }

    def _tasks(self):
        import app.tasks  # noqa: F401 — registers every task module
        from app.celery_app import celery_app

        celery_app.loader.import_default_modules()
        return {name: task for name, task in celery_app.tasks.items()
                if name.startswith("app.tasks.")}

    def test_every_late_acked_task_finishes_inside_the_timeout(self):
        from app.celery_app import VISIBILITY_TIMEOUT_SECONDS

        for name, task in self._tasks().items():
            if not task.acks_late:
                continue
            limit = task.time_limit or task.soft_time_limit
            if limit is None:
                assert name in self._QUICK, f"{name} is late-acked with no time limit"
                continue
            assert limit < VISIBILITY_TIMEOUT_SECONDS, name

    def test_the_long_runners_opt_out(self):
        tasks = self._tasks()
        for name in ("app.tasks.fetch.fetch_jobs", "app.tasks.fetch.fetch_api_sources",
                     "app.tasks.fetch.fetch_ats_boards", "app.tasks.fetch.fetch_browser_tier",
                     "app.tasks.fetch.sweep_linked_boards",
                     "app.tasks.backfill.backfill_boards",
                     "app.tasks.compare_models.run_comparison"):
            assert tasks[name].acks_late is False, name

    def test_the_broker_is_told(self):
        from app.celery_app import VISIBILITY_TIMEOUT_SECONDS, celery_app

        options = celery_app.conf.broker_transport_options
        assert options["visibility_timeout"] == VISIBILITY_TIMEOUT_SECONDS
