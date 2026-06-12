import pytest
from unittest.mock import patch
from app.services.profile_service import (
    get_or_create_profile,
    save_section,
    add_list_item,
    remove_list_item,
    DEFAULT_PROFILE,
    save_narrative_answer,
    generate_questions,
    generate_summary,
)


def test_get_or_create_creates_profile(db):
    profile = get_or_create_profile(db)
    assert profile.id is not None
    assert profile.data["personal"]["name"] == ""
    assert profile.data["experience"] == []
    assert profile.data["narrative"]["answers"] == []


def test_get_or_create_returns_existing(db):
    p1 = get_or_create_profile(db)
    db.flush()
    p2 = get_or_create_profile(db)
    assert p1.id == p2.id


def test_save_section_updates_personal(db):
    get_or_create_profile(db)
    db.flush()
    updated = save_section(db, "personal", {"name": "Jay", "email": "jay@example.com"})
    assert updated.data["personal"]["name"] == "Jay"
    assert updated.data["personal"]["email"] == "jay@example.com"
    assert updated.data["experience"] == []


def test_save_section_updates_skills(db):
    get_or_create_profile(db)
    db.flush()
    updated = save_section(db, "skills", {
        "languages": ["Python", "Go"],
        "frameworks": ["FastAPI"],
        "tools": [],
        "clouds": ["AWS"],
    })
    assert updated.data["skills"]["languages"] == ["Python", "Go"]


def test_add_list_item_experience(db):
    get_or_create_profile(db)
    db.flush()
    item = {
        "company": "Stripe", "role": "SWE",
        "start_date": "2023-01", "end_date": "Present",
        "bullets": ["Built payment APIs"], "tech": ["Python", "Go"],
    }
    updated = add_list_item(db, "experience", item)
    assert len(updated.data["experience"]) == 1
    assert updated.data["experience"][0]["company"] == "Stripe"
    assert "id" in updated.data["experience"][0]


def test_remove_list_item(db):
    get_or_create_profile(db)
    db.flush()
    updated = add_list_item(db, "experience", {"company": "A", "role": "SWE"})
    item_id = updated.data["experience"][0]["id"]
    updated = remove_list_item(db, "experience", item_id)
    assert updated.data["experience"] == []


def test_remove_nonexistent_item_is_noop(db):
    get_or_create_profile(db)
    db.flush()
    updated = remove_list_item(db, "experience", "nonexistent-id")
    assert updated.data["experience"] == []


def test_save_narrative_answer(db):
    get_or_create_profile(db)
    save_section(db, "narrative", {
        "answers": [
            {"question": "How do you solve problems?", "answer": ""},
            {"question": "What energizes you?", "answer": ""},
        ],
        "summary": "",
    })
    db.flush()
    updated = save_narrative_answer(db, index=0, answer="People come to me when stuck.")
    assert updated.data["narrative"]["answers"][0]["answer"] == "People come to me when stuck."
    assert updated.data["narrative"]["answers"][1]["answer"] == ""


def test_generate_questions_calls_llm(db):
    mock_content = """1. How do colleagues describe your problem-solving style?
2. What kinds of problems energize you most?
3. How do you handle ambiguity?"""
    with patch("app.services.profile_service.chat_completion", return_value=mock_content):
        profile = generate_questions(db, api_key="k", base_url="https://api", model="test/model")
    answers = profile.data["narrative"]["answers"]
    assert len(answers) == 3
    assert answers[0]["question"] == "How do colleagues describe your problem-solving style?"
    assert answers[0]["answer"] == ""


def test_generate_summary_calls_llm(db):
    get_or_create_profile(db)
    save_section(db, "narrative", {
        "answers": [
            {"question": "Q1", "answer": "I'm a fast learner."},
            {"question": "Q2", "answer": "I love solving hard problems."},
        ],
        "summary": "",
    })
    db.flush()
    with patch("app.services.profile_service.chat_completion", return_value="Jay is a fast learner who loves hard problems."):
        updated = generate_summary(db, api_key="k", base_url="https://api", model="test/model")
    assert updated.data["narrative"]["summary"] == "Jay is a fast learner who loves hard problems."
