"""
Reading the draft back as the recruiter would.

Document generation is the one step of this pipeline whose output a human
actually reads, and it was the only step with no second look at all: one pass
per piece, straight to PDF. This adds a critique call and a revision pass — and
most of the care here is in the cases where it must NOT change anything.
"""

import json
from unittest.mock import patch

import pytest

from app.services import self_review

BULLETS = [
    {"company": "Acme", "title": "Engineer",
     "bullets": ["Responsible for the payments service.",
                 "Worked on Kafka pipelines."]},
]
SUMMARY = "I build backend services in Python and Go."
COVER = "I am writing to express my interest in the Backend Engineer position."


def _critique(resume=(), cover_letter=()):
    return json.dumps({"resume": list(resume), "cover_letter": list(cover_letter)})


def _run(reply, **kwargs):
    with patch("app.llm.providers.generation_chat", return_value=reply) as call:
        result = self_review.critique(
            kwargs.pop("job_title", "Backend Engineer"),
            kwargs.pop("job_company", "Globex"),
            kwargs.pop("job_description", "We need Kafka and Go experience."),
            kwargs.pop("bullets", BULLETS),
            kwargs.pop("summary", SUMMARY),
            kwargs.pop("cover_body", COVER),
            "key", "url", "model", **kwargs,
        )
    return result, call


class TestWhatComesBack:
    def test_points_are_split_by_document(self, db):
        # The revision calls are separate, and handing the bullet rewriter a
        # note about the cover letter's opening is noise it will act on.
        result, _ = _run(_critique(
            resume=["The bullet 'Responsible for the payments service' names a "
                    "duty, not an outcome; give the throughput figure."],
            cover_letter=["The opening is a statement of interest; lead with "
                          "the Kafka pipeline work instead."],
        ))

        assert len(result["resume"]) == 1
        assert len(result["cover_letter"]) == 1
        assert "duty" in result["resume"][0]

    def test_an_empty_critique_is_a_valid_answer(self, db):
        # A reviewer that must find faults invents them, and the revision then
        # damages a good draft to address them.
        result, _ = _run(_critique())
        assert result == {"resume": [], "cover_letter": []}

    def test_the_prompt_carries_the_whole_application(self, db):
        _, call = _run(_critique())

        sent = call.call_args.kwargs["messages"][1]["content"]
        assert "Responsible for the payments service." in sent
        assert SUMMARY in sent
        assert COVER in sent
        assert "We need Kafka and Go experience." in sent

    def test_vague_points_are_dropped(self, db):
        # "Be more specific" is not actionable, and acting on it costs three
        # calls and a rewrite of text that was fine.
        result, _ = _run(_critique(resume=["Be specific.", "Improve."]))
        assert result["resume"] == []

    def test_duplicate_points_are_collapsed(self, db):
        note = "The payments bullet names a duty rather than an outcome."
        result, _ = _run(_critique(resume=[note, note.upper()]))
        assert len(result["resume"]) == 1

    def test_the_list_is_capped(self, db):
        many = [f"Point number {n} about a bullet that names a duty." for n in range(9)]
        result, _ = _run(_critique(resume=many))
        assert len(result["resume"]) == self_review._MAX_NOTES


class TestItNeverBreaksAGeneration:
    def test_a_failed_call_returns_nothing(self, db):
        with patch("app.llm.providers.generation_chat",
                   side_effect=RuntimeError("provider down")):
            result = self_review.critique(
                "Backend Engineer", "Globex", "jd", BULLETS, SUMMARY, COVER,
                "key", "url", "model",
            )
        assert result == {"resume": [], "cover_letter": []}

    def test_an_unreadable_reply_returns_nothing(self, db):
        result, _ = _run("I think the resume is quite good, actually.")
        assert result == {"resume": [], "cover_letter": []}

    def test_an_empty_draft_costs_no_call(self, db):
        with patch("app.llm.providers.generation_chat") as call:
            result = self_review.critique(
                "Backend Engineer", "Globex", "jd", [], "", "",
                "key", "url", "model",
            )
        call.assert_not_called()
        assert result == {"resume": [], "cover_letter": []}

    def test_a_reply_wrapped_in_thinking_is_still_read(self, db):
        # Reasoning models put the object inside their working.
        result, _ = _run(
            "Let me consider the draft...\n"
            + _critique(resume=["The payments bullet names a duty, not a result."])
            + "\nThat should cover it."
        )
        assert len(result["resume"]) == 1


class TestFeedbackAssembly:
    def test_the_notes_become_an_instruction(self, db):
        text = self_review.as_feedback(["Lead with the Kafka work."])
        assert "Lead with the Kafka work." in text
        assert "add no claim" in text

    def test_the_user_gets_the_last_word(self, db):
        # Where the reviewer and the person disagree, the person wins.
        text = self_review.as_feedback(["Cut the summary."], "Keep the summary.")
        assert text.index("Cut the summary.") < text.index("Keep the summary.")
        assert "The candidate also asked for" in text

    def test_no_notes_leaves_the_user_feedback_alone(self, db):
        assert self_review.as_feedback([], "Keep the summary.") == "Keep the summary."
        assert self_review.as_feedback([]) is None


class TestTheSwitch:
    def test_it_can_be_switched_off(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "SELF_REVIEW_ENABLED", False)
        assert self_review.enabled() is False
