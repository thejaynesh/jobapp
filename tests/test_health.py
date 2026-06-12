def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "db" in data
    assert data["db"] == "ok"


def test_root_redirects_to_apps(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert "/apps" in response.headers["location"]
