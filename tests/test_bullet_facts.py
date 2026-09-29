"""
Asking for the numbers a bullet leaves out, and using the answers.
"""

import json
from unittest.mock import patch

import pytest

from app.models.profile import Profile
from app.services import bullet_facts

PROFILE = {
    "personal": {"name": "Jaynesh Bhandari"},
    "narrative": {"summary": "Backend engineer."},
    "experience": [{
        "id": "exp-1", "company": "Initech", "role": "Engineer", "tech": ["Go"],
        "start_date": "2022-01", "end_date": "2024-01",
        "bullets": ["Reduced ingestion latency with a Go rewrite",
                    "Ran the on-call rota for 6 services",
                    "Led the platform guild"],
    }],
    "projects": [{"id": "proj-1", "name": "Tracker", "description": "job pipeline",
                  "bullets": ["Built a scorer for postings"]}],
}


def store(db, data=None):
    db.query(Profile).delete()
    db.add(Profile(data=json.loads(json.dumps(data or PROFILE))))
    db.commit()


def saved(db):
    db.expire_all()
    return db.query(Profile).first().data


def with_facts(*facts, section="experience"):
    data = json.loads(json.dumps(PROFILE))
    data[section][0]["facts"] = list(facts)
    return data


class TestWhatIsAsked:
    @pytest.mark.parametrize("text", ["Cut costs by 40%", "Mentored three interns",
                                      "Handled dozens of incidents", "Doubled throughput"])
    def test_a_bullet_with_a_figure_is_not_asked_about(self, text):
        assert bullet_facts.has_number(text)

    @pytest.mark.parametrize("bullet, question", [
        ("Reduced ingestion latency with a Go rewrite", "By how much?"),
        ("Improved search relevance", "By how much?"),
        ("Led the platform guild", "How many people"),
        ("Automated the release checklist", "How much time"),
        ("Migrated billing to AWS", "How much was moved"),
        ("Built a scorer for postings", "How many used it"),
        ("Owned the billing domain", "What number shows"),
    ])
    def test_the_question_fits_what_the_bullet_says_it_did(self, bullet, question):
        assert bullet_facts.ask(bullet).startswith(question)

    def test_answered_and_skipped_bullets_are_not_asked_again(self):
        entry = with_facts(
            {"id": "a", "about": "Reduced ingestion latency with a Go rewrite", "answer": "p95 from 800ms to 480ms"},
            {"id": "b", "about": "Led the platform guild", "skipped": True},
        )["experience"][0]
        assert bullet_facts.unanswered(entry) == []
        assert bullet_facts.answers(entry) == ["p95 from 800ms to 480ms"]

    def test_the_count_leaves_out_entries_switched_out_of_resumes(self):
        data = json.loads(json.dumps(PROFILE))
        assert bullet_facts.count_unanswered(data) == 3
        data["projects"][0]["in_resume"] = False
        assert bullet_facts.count_unanswered(data) == 2


class TestAnsweringOnTheProfilePage:
    def test_the_page_asks_under_each_entry(self, client, db):
        store(db)
        page = client.get("/profile?tab=experience").text
        assert "2 bullets with no number" in page
        assert "By how much? A percentage, or before and after." in page

    def test_an_answer_is_kept_and_the_question_goes(self, client, db):
        store(db)
        page = client.post("/profile/experience/exp-1/facts", data={
            "about": "Reduced ingestion latency with a Go rewrite",
            "answer": "  p95 from 800ms   to 480ms "}).text
        facts = saved(db)["experience"][0]["facts"]
        assert [(f["about"], f["answer"]) for f in facts] == [
            ("Reduced ingestion latency with a Go rewrite", "p95 from 800ms to 480ms")]
        assert "1 bullet with no number" in page and "1 fact kept" in page
        # The bullet itself is as the person wrote it.
        assert saved(db)["experience"][0]["bullets"][0] == "Reduced ingestion latency with a Go rewrite"

    def test_a_second_answer_replaces_the_first(self, client, db):
        store(db)
        for answer in ("30% lower", "40% lower p95"):
            client.post("/profile/experience/exp-1/facts", data={
                "about": "Led the platform guild", "answer": answer})
        assert bullet_facts.answers(saved(db)["experience"][0]) == ["40% lower p95"]

    def test_no_number_fits_stops_the_question(self, client, db):
        store(db)
        client.post("/profile/projects/proj-1/facts/skip", data={"about": "Built a scorer for postings"})
        assert bullet_facts.unanswered(saved(db)["projects"][0]) == []
        assert bullet_facts.answers(saved(db)["projects"][0]) == []

    def test_a_fact_can_be_removed(self, client, db):
        store(db, with_facts({"id": "f1", "about": "Led the platform guild", "answer": "9 engineers"}))
        client.post("/profile/experience/exp-1/facts/f1/delete")
        assert saved(db)["experience"][0]["facts"] == []

    def test_an_empty_answer_changes_nothing(self, client, db):
        store(db)
        client.post("/profile/experience/exp-1/facts", data={"about": "Led the platform guild", "answer": " "})
        assert saved(db)["experience"][0].get("facts") == []

    def test_an_unknown_section_or_entry_is_not_found(self, client, db):
        store(db)
        assert client.post("/profile/education/edu-1/facts", data={"answer": "x"}).status_code == 404
        assert client.post("/profile/experience/nope/facts", data={"answer": "x"}).status_code == 404

    def test_editing_the_entry_keeps_its_facts(self, client, db):
        store(db, with_facts({"id": "f1", "about": "Led the platform guild", "answer": "9 engineers"}))
        client.post("/profile/experience/exp-1", data={
            "company": "Initech", "role": "Senior Engineer", "bullets": "Led the platform guild",
            "tech": "Go"})
        entry = saved(db)["experience"][0]
        assert entry["role"] == "Senior Engineer"
        assert bullet_facts.answers(entry) == ["9 engineers"]

    def test_the_profile_check_counts_them(self):
        from app.services.profile_check import report

        result = report(PROFILE)
        assert result["unquantified"] == 3
        assert any("3 bullets have no number" in w for w in result["readiness"]["warnings"])


class TestGenerationUsesThem:
    FACT = {"id": "f1", "about": "Reduced ingestion latency with a Go rewrite",
            "answer": "p95 latency from 800ms to 480ms"}

    def test_the_bullet_rewriter_is_given_them_and_may_use_their_figures(self):
        from app.services.doc_generator import tailor_resume_bullets

        reply = json.dumps([{"company": "Initech", "title": "Engineer", "bullets": [
            "Cut p95 ingestion latency from 800ms to 480ms with a Go rewrite",
            "Ran the on-call rota for 6 services",
            "Led a platform guild of 12 engineers",
        ]}])
        with patch("app.services.doc_generator.chat_completion", return_value=reply) as model:
            result = tailor_resume_bullets(with_facts(self.FACT), "Platform Engineer", "Go.",
                                           "key", "url", "model")
        prompt = model.call_args.kwargs["messages"][1]["content"]
        assert "p95 latency from 800ms to 480ms" in prompt
        assert "the only numbers you may add" in model.call_args.kwargs["messages"][0]["content"]
        # The fact's figures pass the invented-number guard; 12 is from nowhere.
        assert result[0]["bullets"] == [
            "Cut p95 ingestion latency from 800ms to 480ms with a Go rewrite",
            "Ran the on-call rota for 6 services",
            "Led the platform guild",
        ]

    def test_the_letter_and_answer_evidence_lists_them(self):
        from app.services.doc_generator import _evidence_block

        data = with_facts(self.FACT)
        assert "(fact) p95 latency from 800ms to 480ms" in _evidence_block(
            data["experience"], data["projects"])

    def test_an_outreach_message_can_cite_them(self):
        from app.services.outreach import _evidence_lines

        assert "(fact) p95 latency from 800ms to 480ms" in _evidence_lines(with_facts(self.FACT))

    def test_a_drafted_answer_does_not_call_a_fact_figure_unsupported(self, db):
        from app.services import answer_drafts

        store(db, with_facts(self.FACT))
        with patch("app.services.model_roles.call",
                   return_value="I cut p95 latency from 800ms to 480ms."):
            result = answer_drafts.draft(db, "https://example.com/job", "Tell us about your impact.")
        assert result["unsupported_figures"] == []

    def test_the_resume_check_takes_a_fact_as_a_source(self):
        from app.services import content_checks

        data = with_facts(self.FACT)
        ctx = {"experience": [{**data["experience"][0], "title": "Engineer", "bullets": [
            "Cut p95 latency from 800ms to 480ms"]}], "projects": [], "education": []}
        assert content_checks.check_resume(ctx, data) == []
