import copy
import re
import uuid

from sqlalchemy.orm import Session

from app.models.profile import Profile
from app.llm.client import chat_completion

DEFAULT_PROFILE: dict = {
    "personal": {
        "name": "", "email": "", "phone": "",
        "linkedin": "", "github": "", "location": ""
    },
    "experience": [],
    "projects": [],
    "skills": {
        "languages": [], "frameworks": [], "tools": [], "clouds": []
    },
    "education": [],
    "latex_template": "",
    "cover_letter_template": "",
    "target_roles": [],
    "target_locations": [],
    "excluded_companies": [],
    "min_match_score": 70,
    "narrative": {
        "answers": [],
        "summary": ""
    }
}


def get_or_create_profile(db: Session) -> Profile:
    profile = db.query(Profile).first()
    if not profile:
        profile = Profile(data=copy.deepcopy(DEFAULT_PROFILE))
        db.add(profile)
        db.flush()
    return profile


def save_section(db: Session, section: str, data) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    updated[section] = data
    profile.data = updated
    db.flush()
    return profile


def add_list_item(db: Session, section: str, item: dict) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    item_with_id = {"id": str(uuid.uuid4()), **item}
    updated[section].append(item_with_id)
    profile.data = updated
    db.flush()
    return profile


def remove_list_item(db: Session, section: str, item_id: str) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    updated[section] = [i for i in updated[section] if i.get("id") != item_id]
    profile.data = updated
    db.flush()
    return profile


def update_list_item(db: Session, section: str, item_id: str, data: dict) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    for i, item in enumerate(updated[section]):
        if item.get("id") == item_id:
            updated[section][i] = {"id": item_id, **data}
            break
    profile.data = updated
    db.flush()
    return profile


def save_narrative_answer(db: Session, index: int, answer: str) -> Profile:
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data)
    if 0 <= index < len(updated["narrative"]["answers"]):
        updated["narrative"]["answers"][index]["answer"] = answer
    profile.data = updated
    db.flush()
    return profile


def generate_questions(db: Session, api_key: str, base_url: str, model: str) -> Profile:
    prompt = """Generate exactly 15 thoughtful questions to understand a software engineer's
personality, work style, strengths, and unique value. These answers will be used to write
personalized resumes and cover letters that sound authentically like the person.

Cover: problem-solving style, how colleagues rely on them, what energizes them, handling
ambiguity, proudest technical moment, learning style, leadership/collaboration style,
what makes them different, what they want employers to know that their resume doesn't show.

Format: numbered list only, one question per line, no extra text.
Example format:
1. How do colleagues describe your problem-solving style?
2. What kinds of problems energize you most?"""

    response = chat_completion(
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    questions = []
    for line in response.strip().splitlines():
        line = line.strip()
        cleaned = re.sub(r"^\d+\.\s*", "", line)
        if cleaned:
            questions.append({"question": cleaned, "answer": ""})

    return save_section(db, "narrative", {"answers": questions, "summary": ""})


def generate_summary(db: Session, api_key: str, base_url: str, model: str) -> Profile:
    profile = get_or_create_profile(db)
    answers = profile.data["narrative"]["answers"]

    qa_text = "\n".join(
        f"Q: {item['question']}\nA: {item['answer']}"
        for item in answers
        if item.get("answer", "").strip()
    )

    if not qa_text:
        return profile

    prompt = f"""Based on these Q&A answers from a software engineer, write a 2-3 sentence
first-person narrative summary that captures their personality, work style, and unique value.
It should sound natural and personal — not like a resume summary.

{qa_text}

Write only the summary paragraph. No intro, no labels."""

    summary = chat_completion(
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.8,
        max_tokens=300,
    )

    updated = copy.deepcopy(profile.data)
    updated["narrative"]["summary"] = summary.strip()
    profile.data = updated
    db.flush()
    return profile
