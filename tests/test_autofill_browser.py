"""
The extension's form filling, run in a real browser against fixture forms.

`extension/autofill.js` is loaded into each page on its own and driven the way
the panel drives it. Chromium is needed, and CI does not install one, so the
module skips where it cannot launch.
"""

import os
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

SCRIPT = Path(__file__).resolve().parent.parent / "extension" / "autofill.js"

VALUES = {
    "first_name": "Jaynesh", "last_name": "Bhandari", "full_name": "Jaynesh Bhandari",
    "email": "someone@example.com", "phone": "+1 207 555 0100",
    "linkedin": "https://www.linkedin.com/in/example", "github": "", "website": "",
    "location": "Boston, MA", "school": "Northeastern University", "degree": "", "field_of_study": "",
    "work_authorization": "Yes",
    "sponsorship_required": "Yes — I will require sponsorship (F-1 OPT)",
    "start_date": "", "salary_expectation": "", "referral_source": "",
    "eeo_self_identification": "Decline to self-identify",
    "remembered": {
        "are you open to hybrid work": "Yes, three days a week",
        "do you have a non compete agreement": "No",
    },
}


# A Chromium other than the one this Playwright release pins, when that is what
# the machine has (`CHROMIUM_PATH`, or the path a preinstalled one is linked at).
_FALLBACKS = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        launched, error = None, None
        for path in [None] + [p for p in _FALLBACKS if p and Path(p).exists()]:
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
    yield page
    page.close()


def load(page, body: str):
    page.set_content(f"<!doctype html><html><body>{body}</body></html>")
    page.add_script_tag(path=str(SCRIPT))


def fill(page, values=VALUES):
    return page.evaluate("values => JobAppAutofill.fill(values)", values)


def value(page, selector):
    return page.eval_on_selector(selector, "el => el.value")


def checked(page, name):
    return page.evaluate(
        "name => (document.querySelector(`input[name='${name}']:checked`) || {}).value || null",
        name)


GREENHOUSE = """
<label for="first_name">First Name *</label><input id="first_name" name="job_application[first_name]">
<label for="last_name">Last Name *</label><input id="last_name" name="job_application[last_name]">
<label for="email">Email *</label><input id="email" type="email">
<label for="phone">Phone</label><input id="phone" value="617 000 0000">
<label for="sponsor">Will you now or in the future require sponsorship?</label>
<select id="sponsor"><option value="">Select...</option><option>Yes</option><option>No</option></select>
"""


def test_the_profile_is_typed_and_nothing_already_answered_is_touched(page):
    load(page, GREENHOUSE)
    report = fill(page)
    assert value(page, "#first_name") == "Jaynesh"
    assert value(page, "#last_name") == "Bhandari"
    assert value(page, "#email") == "someone@example.com"
    assert value(page, "#phone") == "617 000 0000"
    assert value(page, "#sponsor") == "Yes"
    assert set(report["filled"]) >= {"first_name", "last_name", "email", "sponsorship_required"}


RADIOS = """
<fieldset><legend>Are you legally authorized to work in the United States?</legend>
  <label><input type="radio" name="auth" value="yes"> Yes</label>
  <label><input type="radio" name="auth" value="no"> No</label></fieldset>
<fieldset><legend>Are you authorized to work without requiring sponsorship?</legend>
  <label><input type="radio" name="both" value="yes"> Yes</label>
  <label><input type="radio" name="both" value="no"> No</label></fieldset>
<fieldset><legend>Will you require visa sponsorship now or in the future?</legend>
  <label><input type="radio" name="sponsor" value="yes"> Yes</label>
  <label><input type="radio" name="sponsor" value="no"> No</label></fieldset>
"""


def test_yes_no_questions_asked_as_radio_buttons(page):
    load(page, RADIOS)
    fill(page)
    assert checked(page, "auth") == "yes"
    assert checked(page, "sponsor") == "yes"
    # Asked both ways at once: either answer could be a false statement.
    assert checked(page, "both") is None


EEO = """
<label for="gender">Gender</label>
<select id="gender"><option value="">Select</option><option>Male</option><option>Female</option>
  <option>I don't wish to answer</option></select>
<fieldset><legend>Veteran Status</legend>
  <label><input type="radio" name="veteran" value="p"> I identify as a protected veteran</label>
  <label><input type="radio" name="veteran" value="n"> I am not a protected veteran</label>
  <label><input type="radio" name="veteran" value="d"> I prefer not to answer</label></fieldset>
<label for="race">Race / Ethnicity</label>
<select id="race"><option value="">Select</option><option>Asian</option><option>White</option></select>
"""


def test_self_identification_is_declined_with_the_forms_own_option(page):
    load(page, EEO)
    report = fill(page)
    assert value(page, "#gender") == "I don't wish to answer"
    assert checked(page, "veteran") == "d"
    # No decline option on offer: left for the user, and said so.
    assert value(page, "#race") == ""
    assert report["declined"] == 2 and "self_identification" in report["skipped"]


def test_anything_but_a_decline_answers_no_self_identification_question(page):
    load(page, EEO)
    fill(page, {**VALUES, "eeo_self_identification": "Male"})
    assert value(page, "#gender") == "" and checked(page, "veteran") is None


REMEMBERED = """
<label for="hybrid">Are you open to hybrid work? *</label><input id="hybrid">
<label for="compete">Do you have a non-compete agreement?</label>
<select id="compete"><option value="">--</option><option>Yes</option><option>No</option></select>
<label for="why">Why do you want to work here?</label><textarea id="why"></textarea>
"""


def test_a_question_answered_before_is_answered_the_same(page):
    load(page, REMEMBERED)
    report = fill(page)
    assert value(page, "#hybrid") == "Yes, three days a week"
    assert value(page, "#compete") == "No"
    assert value(page, "#why") == ""
    assert report["remembered"] == 2


COLLECT = """
<label for="email">Email</label><input id="email">
<label for="go">Years of experience with Go</label><input id="go">
<label for="pw">Create a password</label><input id="pw" type="password">
<fieldset><legend>Willing to relocate?</legend>
  <label><input type="radio" name="relocate" value="Yes"> Yes</label>
  <label><input type="radio" name="relocate" value="No"> No</label></fieldset>
"""


def test_only_the_users_own_answers_are_offered_for_remembering(page):
    load(page, COLLECT)
    fill(page)   # types the email: that is ours, not theirs
    page.fill("#go", "4")
    page.fill("#pw", "hunter2")
    page.check("input[name='relocate'][value='Yes']")
    answers = page.evaluate("JobAppAutofill.collectAnswers()")
    assert sorted((a["question"], a["answer"]) for a in answers) == [
        ("Willing to relocate?", "Yes"), ("Years of experience with Go", "4")]


WORKDAY_LISTBOX = """
<label id="q">Will you now or in the future require sponsorship?</label>
<button id="btn" aria-haspopup="listbox" aria-labelledby="q btn" aria-controls="lb">Select One</button>
<ul id="lb" role="listbox" style="display:none">
  <li role="option">Yes</li><li role="option">No</li></ul>
<script>
  btn.addEventListener("click", () => { lb.style.display = "block"; });
  lb.querySelectorAll("[role=option]").forEach(option => option.addEventListener("click", () => {
    btn.textContent = option.textContent; lb.style.display = "none";
  }));
</script>
"""


def test_a_workday_style_dropdown_is_opened_and_answered(page):
    load(page, WORKDAY_LISTBOX)
    report = fill(page)
    assert page.text_content("#btn") == "Yes"
    assert report["filled"] == ["sponsorship_required"]


def test_the_next_step_of_a_form_is_filled_as_it_appears(page):
    load(page, '<label for="first_name">First name</label><input id="first_name">')
    page.evaluate("values => { window.steps = []; "
                  "JobAppAutofill.fill(values).then(() => "
                  "JobAppAutofill.watch(values, r => window.steps.push(r))); }", VALUES)
    page.wait_for_function("document.querySelector('#first_name').value === 'Jaynesh'")
    page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
        '<label for="email">Email address</label><input id="email">')""")
    page.wait_for_function("document.querySelector('#email').value === 'someone@example.com'",
                           timeout=3000)
    assert page.evaluate("window.steps.length") >= 1


def test_the_question_key_matches_the_servers():
    from app.services.remembered_answers import normalize_question

    assert normalize_question("Do you have a non-compete agreement? *") == \
        "do you have a non compete agreement"
    assert normalize_question("Are you open to hybrid work? (Required)") == \
        "are you open to hybrid work"


LONG_FORM = """
<form>
  <label for="why">Why do you want to work at Globex?</label>
  <textarea id="why" maxlength="600"></textarea>
  <label for="proud">Tell us about a project you are proud of</label>
  <textarea id="proud"></textarea>
  <label for="said">Anything else we should know?</label>
  <textarea id="said">Already answered by hand.</textarea>
  <label for="gender">Gender (optional)</label>
  <textarea id="gender"></textarea>
  <label for="hidden">Hidden question text here</label>
  <textarea id="hidden" style="display:none"></textarea>
</form>
"""


class TestLongQuestions:
    def test_it_finds_the_empty_long_questions(self, page):
        load(page, LONG_FORM)
        found = page.evaluate(
            "() => JobAppAutofill.longQuestions().map(q => [q.question, q.maxChars])")
        assert found == [["Why do you want to work at Globex?", 600],
                         ["Tell us about a project you are proud of", None]]

    def test_put_types_it_the_way_a_framework_believes(self, page):
        load(page, LONG_FORM)
        page.evaluate("""() => {
            window.seen = [];
            document.querySelector('#why').addEventListener('input', e => seen.push(e.target.value));
        }""")
        ok = page.evaluate("""() => {
            const q = JobAppAutofill.longQuestions()[0];
            return JobAppAutofill.put(q.field, 'Because of Kafka.');
        }""")
        assert ok is True
        assert value(page, "#why") == "Because of Kafka."
        assert page.evaluate("() => seen") == ["Because of Kafka."]
        assert "solid" in page.eval_on_selector("#why", "el => el.style.outline")

    def test_put_never_overwrites_what_the_user_typed(self, page):
        load(page, LONG_FORM)
        ok = page.evaluate("""() => {
            const q = JobAppAutofill.longQuestions()[0];
            q.field.value = 'My own words.';
            return JobAppAutofill.put(q.field, 'A draft.');
        }""")
        assert ok is False
        assert value(page, "#why") == "My own words."

    def test_a_drafted_answer_is_not_remembered(self, page):
        # Remembered answers are short, reusable facts; a "why us" is about one
        # company, and one the model wrote is not the user's answer to keep.
        load(page, LONG_FORM)
        page.evaluate("""() => {
            const q = JobAppAutofill.longQuestions()[0];
            JobAppAutofill.put(q.field, 'Because of Kafka.');
        }""")
        answers = page.evaluate("() => JobAppAutofill.collectAnswers()")
        assert all(a["answer"] != "Because of Kafka." for a in answers)
