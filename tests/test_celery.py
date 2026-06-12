from app.celery_app import celery_app


def test_celery_app_configured():
    assert celery_app.conf.broker_url is not None
    assert "redis" in celery_app.conf.broker_url
    assert celery_app.conf.result_backend is not None


def test_ping_task():
    from app.celery_app import ping
    result = ping.apply()
    assert result.result == "pong"
