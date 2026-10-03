"""The extension consumes real API enum values and preserves ATS tenant queries."""
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_autofill_browser import browser  # noqa: F401 — shared Chromium fixture
from tests.test_draft_panel_browser import EXTENSION


def open_panel(page, direction="negative"):
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="""
        <label for="first">First Name</label><input id="first">
        <label for="last">Last Name</label><input id="last">
        <label for="previous">Have you worked for us before?</label><input id="previous">
    """))
    page.goto("https://boards.greenhouse.io/embed/job_app?for=acme&token=123")
    page.evaluate("""direction => {
      const attach = Element.prototype.attachShadow;
      Element.prototype.attachShadow = function(init) { return attach.call(this, {...init, mode: 'open'}); };
      window.calls = [];
      window.chrome = {runtime: {id: 'fixture', sendMessage(message, reply) {
        calls.push(message);
        if (!reply) return;
        let data = {};
        if (message.path.startsWith('/api/agent/job-context')) {
          data = {known: true, job: {title: 'Engineer', company: 'Acme', sponsorship_direction: direction}};
        } else if (message.path.startsWith('/api/agent/autofill-fields')) {
          data = {first_name: 'Jane', last_name: 'Doe'};
        } else if (message.path.endsWith('/remember-answers')) { data = {saved: 1}; }
        reply({data});
      }}};
    }""", direction)
    page.add_script_tag(path=str(EXTENSION / "autofill.js"))
    page.add_script_tag(path=str(EXTENSION / "overlay.js"))
    page.locator("button.launcher").click()


@pytest.mark.parametrize("direction,label", [("negative", "no sponsorship"), ("positive", "sponsors visas")])
def test_sponsorship_badge_uses_the_server_values(browser, direction, label):  # noqa: F811
    page = browser.new_page()
    try:
        open_panel(page, direction)
        assert page.get_by_text(label, exact=True).is_visible()
    finally:
        page.close()


def test_greenhouse_employer_query_reaches_both_answer_endpoints(browser):  # noqa: F811
    page = browser.new_page()
    try:
        open_panel(page)
        page.get_by_role("button", name="Fill this form", exact=True).click()
        page.get_by_text("Watching for the next step of this form.", exact=True).wait_for()
        page.locator("#previous").fill("Yes")
        page.get_by_role("button", name="Remember my answers", exact=True).click()
        calls = page.evaluate("window.calls")
        fill = next(call for call in calls if (call.get("path") or "").startswith("/api/agent/autofill-fields"))
        remember = next(call for call in calls if call.get("path") == "/api/agent/remember-answers")
        site = parse_qs(urlsplit(fill["path"]).query)["site"][0]
        assert parse_qs(urlsplit(site).query)["for"] == ["acme"]
        assert remember["body"]["site"] == site
        assert remember["body"]["answers"] == [{"question": "Have you worked for us before?", "answer": "Yes"}]
    finally:
        page.close()
