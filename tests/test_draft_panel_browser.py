"""
The overlay's drafted answers, end to end in a real browser.

`extension/autofill.js` and `extension/overlay.js` are loaded into a fixture
form the way Chrome injects them, with `chrome.runtime` stood in for: the
background worker's replies are canned per API path, and every message the
panel sends is kept for the test to read. The panel draws into a closed shadow
root; here `attachShadow` is made to open it, which is the only change to the
page and the only way a test can press its buttons.
"""

import json
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

EXTENSION = Path(__file__).resolve().parent.parent / "extension"

FORM = """
<form>
  <h1>Platform Engineer</h1>
  <label for="why">Why do you want to work at Globex?</label>
  <textarea id="why" maxlength="600"></textarea>
  <label for="record">If you have ever been convicted of a crime, please explain</label>
  <textarea id="record"></textarea>
  <label for="auth">Please describe your work authorization status</label>
  <textarea id="auth"></textarea>
</form>
"""

REPLIES = {
    "/api/agent/job-context": {"data": {"known": False}},
    "draft": {"data": {"ok": True, "answer": "I would bring my Kafka work at Initech.",
                       "words": 8, "unsupported_figures": ["7"]}},
    "declaration": {"data": {"ok": False, "declaration": True,
                             "detail": "This asks you to state a fact about yourself."}},
}

STAND_IN = """
(replies) => {
  const open = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    return open.call(this, { ...init, mode: "open" });
  };
  window.__sent = [];
  window.chrome = { runtime: {
    id: "test-extension",
    lastError: undefined,
    sendMessage(message, reply) {
      window.__sent.push(message);
      if (!reply) return;
      let answer = { error: "unexpected " + message.path };
      if (message.path && message.path.startsWith("/api/agent/job-context")) {
        answer = replies["/api/agent/job-context"];
      } else if (message.path === "/api/agent/draft-answer") {
        answer = /convict/i.test(message.body.question)
          ? replies.declaration : replies.draft;
      }
      setTimeout(() => reply(answer), 10);
    },
  } };
}
"""

_FALLBACKS = ["/opt/pw-browsers/chromium"]


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        launched, error = None, None
        for path in [None] + [p for p in _FALLBACKS if Path(p).exists()]:
            try:
                launched = playwright.chromium.launch(executable_path=path)
                break
            except Exception as exc:  # pragma: no cover — depends on the machine
                error = exc
        if launched is None:  # pragma: no cover
            pytest.skip(f"no Chromium to run the extension in: {error}")
        yield launched
        launched.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.set_content(f"<!doctype html><html><body>{FORM}</body></html>")
    # Before the extension's scripts load, as Chrome's own objects would be.
    page.evaluate(f"({STAND_IN})({json.dumps(REPLIES)})")
    page.add_script_tag(path=str(EXTENSION / "autofill.js"))
    page.add_script_tag(path=str(EXTENSION / "overlay.js"))
    page.locator("button.launcher").click()
    yield page
    page.close()


def draft_all(page):
    page.get_by_role("button", name="Draft answers to 2 long questions").click()
    page.get_by_text("Left for you:").wait_for()
    page.locator(".draft textarea").wait_for()


def sent(page, kind):
    return [m for m in page.evaluate("() => window.__sent") if m.get("type") == kind]


class TestDraftingFromThePanel:
    def test_the_panel_offers_the_long_questions(self, page):
        page.get_by_role("button", name="Draft answers to 2 long questions").wait_for()

    def test_each_question_is_asked_for_on_its_own_with_its_limit(self, page):
        draft_all(page)
        asks = [m for m in sent(page, "overlay-api") if m["path"] == "/api/agent/draft-answer"]
        # The work-authorization box is a profile field autofill answers, so
        # it is not offered for drafting at all.
        assert [a["body"]["question"] for a in asks] == [
            "Why do you want to work at Globex?",
            "If you have ever been convicted of a crime, please explain",
        ]
        assert asks[0]["body"]["max_chars"] == 600 and asks[1]["body"]["max_chars"] is None
        assert asks[0]["timeoutMs"] == 90000
        assert asks[0]["body"]["posting"]["title"] == "Platform Engineer"

    def test_nothing_reaches_the_form_until_it_is_put_there(self, page):
        draft_all(page)
        assert page.eval_on_selector("#why", "el => el.value") == ""

    def test_an_edited_draft_is_what_goes_in(self, page):
        draft_all(page)
        page.locator(".draft textarea").fill("My own edit of the draft.")
        page.get_by_role("button", name="Put in form").click()
        assert page.eval_on_selector("#why", "el => el.value") == "My own edit of the draft."
        events = [m for m in sent(page, "overlay-event") if m["kind"] == "draft_answer"]
        assert events[-1]["summary"] == {"drafted": True, "put": True, "edited": True}

    def test_a_declaration_is_left_for_the_user(self, page):
        draft_all(page)
        assert page.get_by_text("Left for you: This asks you to state a fact").is_visible()
        assert page.eval_on_selector("#record", "el => el.value") == ""

    def test_an_unsupported_figure_is_pointed_out(self, page):
        draft_all(page)
        assert page.get_by_text("Check these figures").is_visible()

    def test_the_users_own_typing_is_never_overwritten(self, page):
        draft_all(page)
        page.fill("#why", "I typed this myself.")
        page.get_by_role("button", name="Put in form").click()
        assert page.eval_on_selector("#why", "el => el.value") == "I typed this myself."
        assert page.get_by_text("was left alone").is_visible()

    def test_a_draft_over_the_form_limit_is_held_back(self, page):
        draft_all(page)
        page.locator(".draft textarea").fill("x" * 601)
        page.get_by_role("button", name="Put in form").click()
        assert page.eval_on_selector("#why", "el => el.value") == ""
        assert page.get_by_text("Over the form's 600-character limit").is_visible()

    def test_discard_puts_nothing_in(self, page):
        draft_all(page)
        page.get_by_role("button", name="Discard").click()
        assert page.locator(".draft textarea").count() == 0
        assert page.eval_on_selector("#why", "el => el.value") == ""
