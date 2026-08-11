"""
What a posting says about who may hold the role.

The interesting cases here are almost all negative: the phrases this module
looks for appear constantly in job descriptions that carry no restriction at
all, and firing on those would quietly delete good jobs.
"""

import pytest

from app.services.eligibility import scan


BASE = "We are hiring a Backend Engineer to work on our payments platform. "


class TestCitizenshipRestriction:
    @pytest.mark.parametrize("sentence", [
        "Applicants must be a US citizen.",
        "Candidates must be a U.S. citizen to be considered.",
        "US citizenship is required for this position.",
        "United States citizenship is required.",
        "This role is open only to US citizens.",
        "This position is restricted to U.S. citizens.",
        "US citizens only.",
    ])
    def test_blocks_explicit_citizenship_requirements(self, sentence):
        result = scan(BASE + sentence)
        assert result.blocked
        assert result.restriction_label == "US citizenship required" or "citizens only" in (
            result.restriction_label or "").lower()

    @pytest.mark.parametrize("sentence", [
        "Must hold an active security clearance.",
        "An active TS/SCI clearance is required.",
        "Candidates must possess a Top Secret clearance.",
        "Secret clearance required.",
        "Security clearance is required for this role.",
    ])
    def test_blocks_clearance_requirements(self, sentence):
        result = scan(BASE + sentence)
        assert result.blocked
        assert "clearance" in result.restriction_label.lower()

    @pytest.mark.parametrize("sentence", [
        "This role is subject to ITAR regulations.",
        "Applicants must be a U.S. Person as defined by ITAR.",
        "This position involves export-controlled technical data.",
    ])
    def test_blocks_export_control(self, sentence):
        result = scan(BASE + sentence)
        assert result.blocked

    def test_quotes_the_triggering_sentence(self):
        result = scan(BASE + "US citizenship is required for this position.")
        assert result.restriction_quote == "US citizenship is required for this position."

    def test_finds_restriction_in_a_bullet_list(self):
        description = (
            "Requirements:\n"
            "• 3+ years of Python\n"
            "• Must be a US citizen\n"
            "• Strong communication skills\n"
        )
        result = scan(description)
        assert result.blocked
        assert result.restriction_quote == "Must be a US citizen"

    def test_long_sentences_are_truncated(self):
        filler = "context " * 100
        result = scan(f"{filler}must be a US citizen{filler}")
        assert result.blocked
        assert len(result.restriction_quote) <= 300
        assert result.restriction_quote.endswith("…")


class TestRestrictionFalsePositives:
    """The phrases appear far more often than the restrictions do."""

    def test_ignores_eeo_boilerplate(self):
        # EEO statements enumerate citizenship precisely because the employer is
        # promising not to select on it.
        result = scan(
            BASE + "We are an equal opportunity employer and do not discriminate "
            "on the basis of race, national origin, or citizenship status."
        )
        assert not result.blocked

    def test_ignores_regardless_of_citizenship(self):
        result = scan(
            BASE + "We welcome all applicants regardless of citizenship or "
            "immigration status."
        )
        assert not result.blocked

    @pytest.mark.parametrize("sentence", [
        "No security clearance is required for this role.",
        "A security clearance is not required.",
        "Clearance not required.",
        "An active clearance is nice to have but not required.",
    ])
    def test_ignores_negated_clearance(self, sentence):
        result = scan(BASE + sentence)
        assert not result.blocked

    @pytest.mark.parametrize("description", [
        "You will work with GCP Secret Manager and Vault.",
        "We keep no trade secrets from our engineers.",
        "Our secret sauce is a strong engineering culture.",
        "You will have 5 years to clear your goals.",
        "Experience with search infrastructure is a plus.",
        "You should have a clear understanding of distributed systems.",
    ])
    def test_bare_landmine_words_do_not_fire(self, description):
        # "secret", "ear" and "clear" are all substrings of ordinary job text.
        assert not scan(BASE + description).blocked

    def test_empty_description_is_not_blocked(self):
        assert not scan("").blocked
        assert not scan(None).blocked


class TestSponsorshipNote:
    @pytest.mark.parametrize("sentence", [
        "We will not sponsor visas for this position.",
        "We are unable to sponsor applicants at this time.",
        "This role is not eligible for visa sponsorship.",
        "No visa sponsorship is available.",
        "Applicants must be authorized to work in the US without sponsorship "
        "now or in the future.",
        "Candidates must not require sponsorship.",
    ])
    def test_detects_negative_statements(self, sentence):
        result = scan(BASE + sentence)
        assert result.sponsorship_direction == "negative"
        assert "sponsor" in result.sponsorship_note.lower()

    @pytest.mark.parametrize("sentence", [
        "Visa sponsorship is available for this role.",
        "We offer visa sponsorship to qualified candidates.",
        "We are open to sponsoring exceptional applicants.",
        "Sponsorship provided for the right candidate.",
    ])
    def test_detects_positive_statements(self, sentence):
        result = scan(BASE + sentence)
        assert result.sponsorship_direction == "positive"

    def test_silent_postings_get_no_note(self):
        result = scan(BASE + "You will write Python and review pull requests.")
        assert result.sponsorship_note is None
        assert result.sponsorship_direction is None

    def test_prefers_the_negative_when_a_posting_says_both(self):
        result = scan(
            BASE
            + "Visa sponsorship is available for many of our roles. "
            + "This particular position is not eligible for sponsorship."
        )
        assert result.sponsorship_direction == "negative"
        assert "not eligible" in result.sponsorship_note

    def test_ignores_eeo_boilerplate(self):
        result = scan(
            BASE + "We are an equal opportunity employer; sponsorship status is "
            "never a factor in our hiring decisions."
        )
        assert result.sponsorship_note is None


class TestHardWrappedText:
    """Descriptions arrive wrapped at ~70 columns; sentences span lines."""

    def test_quote_spans_a_wrapped_sentence(self):
        description = (
            "Applicants must be authorized to work in the United States without sponsorship\n"
            "now or in the future.\n"
        )
        result = scan(description)
        assert result.sponsorship_note == (
            "Applicants must be authorized to work in the United States "
            "without sponsorship now or in the future."
        )

    def test_heading_is_not_glued_to_the_sentence_below_it(self):
        description = (
            "Full Stack Engineer — Bright Labs\n"
            "Applicants must be authorized to work in the United States without sponsorship\n"
            "now or in the future.\n"
        )
        result = scan(description)
        assert result.sponsorship_note.startswith("Applicants must be")

    def test_wrapped_restriction_is_still_found(self):
        description = (
            "This position involves access to export-controlled technical data and applicants\n"
            "must be a U.S. Person as defined by ITAR.\n"
        )
        result = scan(description)
        assert result.blocked
        assert result.restriction_quote.endswith("as defined by ITAR.")

    def test_list_items_stay_separate(self):
        description = (
            "Requirements:\n"
            "- Comfortable owning services end to end in a production environment\n"
            "- Willing to participate in an on-call rotation for the payments team\n"
        )
        # Neither line ends in punctuation, but both are list items.
        result = scan(description)
        assert not result.blocked


class TestTiersAreIndependent:
    """A note must never imply a block, and a block never depends on a note."""

    def test_sponsorship_statement_alone_does_not_block(self):
        result = scan(BASE + "We will not sponsor visas for this position.")
        assert result.sponsorship_direction == "negative"
        assert not result.blocked
        assert result.restriction_label is None

    def test_a_posting_can_carry_both(self):
        result = scan(
            BASE
            + "Must be a US citizen. "
            + "We do not provide visa sponsorship."
        )
        assert result.blocked
        assert result.sponsorship_direction == "negative"
