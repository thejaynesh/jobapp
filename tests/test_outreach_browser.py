"""Draft actions against the actual templates and HTMX, with all HTTP intercepted."""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader

from tests.test_autofill_browser import browser  # noqa: F401 — shared Chromium fixture

ROOT = Path(__file__).resolve().parents[1]


def open_message(page, status="draft"):
    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    env.filters["when"] = lambda value, fmt: ""
    message = dict(id="example", channel="email", kind="initial", sequence_step=1,
        status=status, sent_at=None, follow_up_due_at=None, edited=False, char_count=18,
        send_error=None, subject="Original subject", body="Original AI draft.",
        contact=dict(email="recruiter@example.invalid", email_status="verified"),
        delivery_uncertain=False, send_in_progress=False, generated_by=None)
    html = env.from_string('{% extends "base.html" %}{% block content %}'
        '<div id="outreach-panel">{% include "outreach/partials/message.html" %}</div>'
        '{% endblock %}').render(msg=message, send_blocked=False,
            request=SimpleNamespace(url=SimpleNamespace(path="/outreach"), scope={}, query_params={}))
    requests = []

    def respond(route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == "POST":
            requests.append((path, parse_qs(request.post_data or "", keep_blank_values=True)))
            body = '<span>Saved</span>' if path.endswith("/save") else '<div id="outreach-panel">Sent</div>'
            route.fulfill(status=200, content_type="text/html", body=body)
        elif path == "/static/js/htmx.min.js":
            route.fulfill(status=200, content_type="application/javascript",
                          body=(ROOT / "app/static/js/htmx.min.js").read_text())
        elif path == "/":
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            route.fulfill(status=200, body="")

    page.route("**/*", respond)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto("https://fixture.invalid/")
    page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {value: {
        writeText(text) { window.copied = text; return Promise.resolve(); }
    }})""")
    return requests


def test_immediate_send_carries_the_unsaved_editor_values(browser):  # noqa: F811
    page = browser.new_page()
    try:
        requests = open_message(page)
        page.locator('input[name="subject"]').fill("Reviewed subject")
        page.locator('textarea[name="body"]').fill("My approved correction.")
        page.get_by_role("button", name="Send", exact=True).click()
        page.get_by_text("Sent", exact=True).wait_for()
        sent = next(values for path, values in requests if path.endswith("/send"))
        assert sent["subject"] == ["Reviewed subject"]
        assert sent["body"] == ["My approved correction."]
        assert sent["save_draft"] == ["true"]
    finally:
        page.close()


def test_manual_send_carries_the_copied_editor_before_autosave(browser):  # noqa: F811
    page = browser.new_page()
    try:
        requests = open_message(page)
        page.locator('input[name="subject"]').fill("Reviewed subject")
        page.locator('textarea[name="body"]').fill("My approved correction.")
        page.get_by_role("button", name="Copy", exact=True).click()
        page.wait_for_function("window.copied !== undefined")
        page.get_by_role("button", name="I sent this", exact=True).click()
        page.get_by_text("Sent", exact=True).wait_for()
        sent = next(values for path, values in requests if path.endswith("/status"))
        assert sent["subject"] == ["Reviewed subject"]
        assert sent["body"] == [page.evaluate("window.copied")]
        assert sent["save_draft"] == ["true"] and sent["status"] == ["sent"]
    finally:
        page.close()


def test_copy_and_mail_app_use_the_editor_before_and_after_autosave(browser):  # noqa: F811
    page = browser.new_page()
    try:
        open_message(page)
        body = "My approved correction & next steps."
        page.locator('input[name="subject"]').fill("Reviewed & ready")
        page.locator('textarea[name="body"]').fill(body)
        page.get_by_role("button", name="Copy", exact=True).click()
        page.wait_for_function("window.copied !== undefined")
        assert page.evaluate("window.copied") == body
        page.get_by_text("Saved", exact=True).wait_for()
        assert page.locator("#msg-text-example").evaluate("el => el.content.textContent") != body
        page.evaluate("window.copied = undefined")
        page.locator('button[data-message-id="example"]').click()
        page.wait_for_function("window.copied !== undefined")
        assert page.evaluate("window.copied") == body
        link = page.get_by_role("link", name="Open in mail app")
        link.evaluate("el => el.addEventListener('click', event => event.preventDefault())")
        link.click()
        query = parse_qs(urlsplit(link.get_attribute("href")).query)
        assert query == {"subject": ["Reviewed & ready"], "body": [body]}
    finally:
        page.close()


def test_copy_of_a_sent_message_keeps_the_stored_body(browser):  # noqa: F811
    page = browser.new_page()
    try:
        open_message(page, status="sent")
        page.get_by_role("button", name="Copy", exact=True).click()
        page.wait_for_function("window.copied !== undefined")
        assert page.evaluate("window.copied") == "Original AI draft."
    finally:
        page.close()
