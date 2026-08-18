"""
The five questions every application form asks, answered once.

Name, email and phone were already autofilled. What was left was the part that
actually takes the time: are you authorised to work here, will you need
sponsorship, when can you start, what are you looking for, how did you hear
about us. Five questions, asked by every ATS, in a different order and with
different wording each time — and typed out by hand, every time, for years.

Two things about how they are stored.

**They are free text, not a fixed vocabulary.** "Authorized to work in the US
without sponsorship" is a sentence one form wants in a textarea and another
wants matched against a dropdown option, and a schema that only allowed
yes/no could serve neither. The answer the user writes is the answer that
gets typed.

**Nothing here is guessed.** An unset answer is left blank rather than
defaulted, because these are legal declarations on a document going to an
employer. A wrong "no sponsorship required" is worse than an empty box: the
empty box gets noticed and filled, and the wrong one gets submitted.
"""

# key, label, help text, and the placeholder the profile form shows.
#
# The order is the order the profile form renders them in, which is roughly
# the order forms ask them in.
FIELDS = [
    (
        "work_authorization",
        "Work authorization",
        "Asked as “Are you legally authorized to work in [country]?” — answer "
        "as you would on the form, e.g. “Yes”.",
        "Yes",
    ),
    (
        "sponsorship_required",
        "Will you need sponsorship?",
        "Asked as “Will you now or in the future require sponsorship?”. Note "
        "that forms phrase this both ways round, so the autofill only uses "
        "this on fields it is confident about.",
        "Yes — I will require sponsorship (F-1 OPT, seeking H-1B)",
    ),
    (
        "start_date",
        "Earliest start date",
        "Asked as “When can you start?”.",
        "Two weeks from an offer",
    ),
    (
        "salary_expectation",
        "Salary expectation",
        "Asked as “Desired compensation”. Some forms take a number only, so a "
        "plain figure fills more boxes than a sentence does.",
        "120000",
    ),
    (
        "referral_source",
        "How did you hear about us?",
        "Asked on almost every form and worth nothing to either side, which is "
        "exactly why it should not be typed out by hand again.",
        "Company careers page",
    ),
]

KEYS = [key for key, _, _, _ in FIELDS]


def answers(profile_data: dict) -> dict:
    """
    The stored answers, as a complete dict of strings.

    Always every key, so a caller never has to distinguish "not set" from "not
    in the profile yet" — both are the empty string, and the autofill skips
    empty values.
    """
    stored = (profile_data or {}).get("screening_answers") or {}
    out = {}
    for key in KEYS:
        value = stored.get(key)
        out[key] = value.strip() if isinstance(value, str) else ""
    return out


def clean(submitted: dict) -> dict:
    """What to store from a submitted form. Unknown keys are dropped."""
    out = {}
    for key in KEYS:
        value = (submitted or {}).get(key)
        out[key] = " ".join(str(value).split()) if value is not None else ""
    return out
