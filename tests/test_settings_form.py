"""
The settings form submits.

A number field whose value falls outside its own `min`/`max`, or off its
`step`, makes the browser refuse the whole form, and when that field is in a
collapsed group the browser cannot show why: Save does nothing. Two such
fields shipped (a LinkedIn page cap whose default was over its maximum, and a
0.02 rate on a field that only accepted tenths), and every change on the page
was lost to them.
"""

import re
from pathlib import Path

import pytest

from app.services import tunables


@pytest.mark.parametrize("spec", [t for t in tunables.TUNABLES if t.kind in ("int", "float")],
                         ids=lambda t: t.key)
def test_every_default_fits_its_own_field(spec):
    value = tunables.default(spec)
    if value is None:
        return
    if spec.minimum is not None:
        assert value >= spec.minimum, f"{spec.env}={value} is under the field's minimum"
    if spec.maximum is not None:
        assert value <= spec.maximum, f"{spec.env}={value} is over the field's maximum"
    if spec.kind == "int":
        assert float(value).is_integer()


def test_decimal_fields_take_any_precision(client):
    page = client.get("/settings").text
    floats = re.findall(r'<input type="number"[^>]*data-kind="float"[^>]*>', page, re.S)
    assert floats and all('step="any"' in f for f in floats)


sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        launched = None
        for path in [None] + [p for p in ["/opt/pw-browsers/chromium"] if Path(p).exists()]:
            try:
                launched = playwright.chromium.launch(executable_path=path)
                break
            except Exception:  # pragma: no cover — depends on the machine
                continue
        if launched is None:  # pragma: no cover
            pytest.skip("no Chromium here")
        yield launched
        launched.close()


@pytest.fixture
def settings_page(browser, client, db):
    """The page as the server renders it, at a fake origin; posts are caught."""
    html = client.get("/settings").text
    page = browser.new_page()
    posted = []

    def serve(route):
        request = route.request
        if request.method == "POST":
            posted.append(request.post_data or "")
            route.fulfill(status=200, body="<p>saved</p>", content_type="text/html")
        elif request.url.rstrip("/").endswith("/settings"):
            route.fulfill(status=200, body=html, content_type="text/html")
        else:
            route.fulfill(status=200, body="")

    page.route("http://app.test/**", serve)
    page.goto("http://app.test/settings")
    yield page, posted
    page.close()


class TestInTheBrowser:
    def test_nothing_on_the_form_starts_out_invalid(self, settings_page):
        page, _ = settings_page
        invalid = page.evaluate("""() => Array.from(
            document.querySelectorAll('form[action="/settings"] input, form[action="/settings"] select'))
            .filter(e => !e.checkValidity()).map(e => e.name + ': ' + e.validationMessage)""")
        assert invalid == []

    def test_save_submits_a_changed_model(self, settings_page):
        page, posted = settings_page
        select = page.locator('select[name="nvidia_nim_model"]')
        # Only the first group starts open; open this one as a person would.
        select.locator("xpath=ancestor::details[1]").evaluate("d => d.open = true")
        options = select.locator("option").all_inner_texts()
        select.select_option(options[-1])
        page.get_by_role("button", name="Save settings").click()
        page.wait_for_url("**/settings")
        page.wait_for_function("() => document.body.innerText.includes('saved')")
        assert posted and "nvidia_nim_model=" in posted[0]

    def test_a_value_it_will_not_take_is_shown_not_swallowed(self, settings_page):
        page, posted = settings_page
        field = page.locator('input[name="linkedin_max_pages"]')
        group = field.locator("xpath=ancestor::details[1]")
        page.evaluate("() => document.querySelectorAll('details.tunable-group')"
                      ".forEach(d => d.open = false)")
        field.evaluate("e => e.value = '999'")
        page.get_by_role("button", name="Save settings").click()
        page.wait_for_timeout(300)
        assert posted == []
        assert group.evaluate("d => d.open") is True
        assert page.evaluate("() => document.activeElement && document.activeElement.name") \
            == "linkedin_max_pages"
