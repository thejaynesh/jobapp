"""Exercise the source/watch/relationship forms against the actual local app."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from tests.test_autofill_browser import browser  # noqa: F401


def open_app(browser, client, path):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.fixture_responses = []
    def serve(route):
        request = route.request
        url = urlsplit(request.url)
        response = client.request(request.method, url.path + ("?" + url.query if url.query else ""),
            content=request.post_data_buffer,
            headers={key: value for key, value in request.headers.items()
                     if key.lower() in {"content-type", "hx-request", "hx-current-url"}},
            # Chromium does not re-route a redirect chain from route.fulfill;
            # follow it inside TestClient so the browser never leaves the
            # local fixture for fixture.invalid after a successful form save.
            follow_redirects=True)
        page.fixture_responses.append({"method": request.method, "path": url.path,
                                       "status": response.history[0].status_code if response.history else response.status_code,
                                       "body": response.text[:1500]})
        route.fulfill(status=response.status_code, body=response.content,
            headers={key: value for key, value in response.headers.items()
                     if key.lower() in {"content-type", "location"}})
    # Browser requests are served by TestClient; no external sites are visited.
    page.route("**/*", serve)
    page.goto("https://fixture.invalid" + path)
    return page


def screenshot(page, name, selector="main"):
    output = os.environ.get("OPPORTUNITY_UI_CAPTURE_DIR")
    if output:
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        page.locator(selector).screenshot(path=str(folder / (name + ".png")))


def test_employer_watch_can_be_saved_and_paused(browser, client, db, monkeypatch):
    from app.models.company import Company
    monkeypatch.setattr("app.routers.companies._queue", lambda *args: None)
    page = open_app(browser, client, "/companies")
    try:
        page.get_by_label("Employer name", exact=True).fill("Example Robotics")
        page.get_by_label("Confirmed employer website").fill("https://robotics.example")
        page.get_by_label("Careers or ATS URL").fill("https://jobs.lever.co/example-robotics")
        page.get_by_role("button", name="Save and watch employer").click()
        posts = [row for row in page.fixture_responses if row["method"] == "POST" and row["path"] == "/companies"]
        assert posts and posts[-1]["status"] == 303, posts or page.locator("body").inner_text()
        assert db.query(Company).filter_by(name="Example Robotics", watched=True).count() == 1
        page.get_by_role("heading", name="Example Robotics", exact=True).wait_for()
        assert db.query(Company).filter_by(name="Example Robotics", watched=True).count() == 1
        screenshot(page, "employers-desktop")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        screenshot(page, "employers-mobile")
        page.get_by_role("button", name="Pause watch").click()
        page.get_by_role("button", name="Resume watch").wait_for()
        db.expire_all()
        assert not db.query(Company).filter_by(name="Example Robotics").one().watched
    finally:
        page.close()


def test_known_source_registration_updates_visible_status(browser, client, db):
    from app.models.company_board import CompanyBoard
    page = open_app(browser, client, "/runs")
    try:
        page.get_by_label("Job source URL").fill("https://jobs.lever.co/example-new-source")
        page.get_by_role("button", name="Add source", exact=True).click()
        page.get_by_text("Recognized ATS board saved.", exact=False).first.wait_for()
        assert db.query(CompanyBoard).filter_by(ats="lever", slug="example-new-source").count() == 1
        screenshot(page, "source-learning", "#source-learning")
        screenshot(page, "collection-coverage", "section[aria-labelledby='collection-health-title']")
    finally:
        page.close()


def test_relationship_action_saves_and_appears_in_today(browser, client, db):
    from app.models.outreach import OutreachConversation
    from app.services.opportunity_actions import build
    response = client.post("/api/outreach/contacts/capture", json={"name": "Sam Example",
        "company": "Example Robotics", "email": "sam@robotics.example", "relationship_kind": "colleague"})
    assert response.status_code == 201
    page = open_app(browser, client, response.json()["url"])
    try:
        page.get_by_text("Relationship · Researching", exact=False).click()
        page.get_by_label("Conversation stage").select_option("intro_requested")
        page.get_by_label("Record an interaction").fill("Sam offered to introduce me to the platform team.")
        page.get_by_label("Next action", exact=True).fill("Send Sam my project summary")
        page.get_by_label("Action date").fill("2026-01-01")
        page.get_by_role("button", name="Save relationship and next action").click()
        page.get_by_text("Relationship · Intro Requested", exact=False).wait_for()
        db.expire_all()
        assert db.query(OutreachConversation).one().next_action == "Send Sam my project summary"
        assert any(action["title"] == "Send Sam my project summary" for action in build(db, {}))
        page.get_by_text("Relationship · Intro Requested", exact=False).click()
        assert page.get_by_label("Action date").input_value() == "2026-01-01"
        screenshot(page, "relationship-desktop")
    finally:
        page.close()
