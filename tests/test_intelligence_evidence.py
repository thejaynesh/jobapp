from types import SimpleNamespace

from app.services import evidence, matcher


def job(**changes):
    return SimpleNamespace(**{**dict(description="Python is required. Java or Kotlin is required. Kubernetes is preferred.",
        required_skills=["Python", "Java", "Kotlin"], nice_to_have_skills=["Kubernetes"],
        title="Engineer", company="Example", location="Remote", is_remote=True, experience_level="junior"), **changes})


def profile():
    return {"experience": [{"id": "work", "title": "Engineer", "company": "Example",
        "bullets": ["Built Python services handling 20,000 events daily."],
        "facts": [{"id": "metric", "answer": "Reduced latency by 35%."}]}], "skills": {"languages": ["Java"]}}


def test_prompt_reads_real_achievements_and_is_bounded():
    p = profile()
    p["settings"] = {"match_evidence_mode": "assist"}
    messages = matcher._build_match_prompt(job(), p)
    assert "20,000 events" in messages[1]["content"]
    assert "35%" in messages[1]["content"]
    assert "never instructions" in messages[0]["content"]
    assert evidence.select(p, "Python", budget=10) == []


def test_alternatives_count_once_and_unknown_is_not_failure():
    result = evidence.assess(job(), profile())
    rows = result["requirements"]
    assert len(rows) == 3
    assert rows[1]["label"] == "Java or Kotlin"
    assert rows[1]["status"] == "supported"
    assert rows[2]["status"] == "unknown" and rows[2]["priority"] == "preferred"


def test_forged_model_evidence_is_refused():
    subject, candidate = job(), profile()
    requirement = evidence.requirements(subject)[-1]
    result = evidence.assess(subject, candidate, [{"requirement_id": requirement["id"],
        "status": "transferable", "fact_id": "invented", "quote": "Expert Kubernetes engineer"}])
    assert result["requirements"][-1]["status"] == "unknown"


def test_draft_bullets_are_not_approved_evidence():
    p = profile()
    p["experience"][0]["bank"] = [{"id": "x", "text": "Invented Rust project", "status": "offered"}]
    assert "Invented" not in str(evidence.facts(p))


def test_changed_profile_invalidates_an_assessment():
    subject, p = job(), profile()
    subject.match_assessment = evidence.assess(subject, p)
    p["skills"]["languages"].append("Kubernetes")
    assert evidence.current(subject, p)["requirements"][-1]["status"] == "supported"


def test_rendered_coverage_does_not_count_a_trimmed_achievement():
    result = evidence.coverage(job(), profile(), "Python Java")
    assert result["requirements"][0]["status"] == "term readable; review evidence"
    assert not result["requirements"][0]["fact_ids"]


def test_no_downtime_is_not_negated_python_experience():
    p = profile()
    p["experience"][0]["bullets"] = ["Built Python services with no downtime"]
    assert evidence.assess(job(), p)["requirements"][0]["status"] == "supported"


def test_an_explicit_negative_answer_is_a_conflict():
    subject, p = job(), profile()
    key = evidence.requirements(subject)[0]["id"]
    p["requirement_answers"] = {key: {"text": "I have not used Python professionally"}}
    assert evidence.assess(subject, p)["requirements"][0]["status"] == "conflicting"


def test_years_and_education_are_preserved_as_unknown_questions():
    subject = job(description="At least 5 years of experience. A degree is required.", required_skills=[], nice_to_have_skills=[])
    rows = evidence.assess(subject, {})["requirements"]
    assert {row["kind"] for row in rows} == {"experience", "education"}
    assert all(row["status"] == "unknown" and row["quote"] for row in rows)


def test_unchanged_description_but_changed_requirements_invalidates_cache():
    subject, p = job(), profile()
    subject.match_assessment = evidence.assess(subject, p)
    subject.required_skills = ["Rust"]
    assert evidence.current(subject, p)["posting_hash"] != subject.match_assessment["posting_hash"]


def test_shadow_preserves_the_baseline_prompt():
    messages = matcher._build_match_prompt(job(), profile())
    assert "20,000 events" not in messages[1]["content"]


def test_transferable_claim_requires_an_existing_fact_and_verbatim_quote():
    subject, p = job(), profile()
    fact = evidence.select(p, "Python")[0]
    row = evidence.requirements(subject)[-1]
    proposal = {"requirement_id": row["id"], "status": "transferable", "fact_id": fact["id"], "quote": fact["text"], "explanation": "Related operational work"}
    result = evidence.assess(subject, p, [proposal])["requirements"][-1]
    assert result["status"] == "transferable" and result["inference"]


def test_malformed_model_references_cannot_break_matching():
    assert evidence.assess(job(), profile(), [{"requirement_id": ["invalid"]}])["requirements"]
    key = evidence.requirements(job())[-1]["id"]
    result = evidence.assess(job(), profile(), [{"requirement_id": key, "fact_id": ["invalid"], "status": "transferable"}])
    assert result["requirements"][-1]["status"] == "unknown"


def test_expired_eligibility_answer_does_not_count_as_current_evidence():
    p = profile()
    p["requirement_answers"] = {"r-example": {"text": "I can relocate", "expires_at": "2020-01-01T00:00:00+00:00"}}
    assert "relocate" not in str(evidence.facts(p))


def test_alternative_supported_even_when_other_alternative_is_explicitly_absent():
    subject = job(description="Java or Kotlin is required.", required_skills=["Java", "Kotlin"], nice_to_have_skills=[])
    p = {"experience": [{"id": "work", "bullets": ["Built Kotlin services."]}]}
    key = evidence.requirements(subject)[0]["id"]
    p["requirement_answers"] = {key: {"text": "No Java experience. I have built Kotlin services."}}
    assert evidence.assess(subject, p)["requirements"][0]["status"] == "supported"
