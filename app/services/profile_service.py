import copy
import re
import uuid

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

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

PROFILE_SEED: dict = {
    "experience": [
        {
            "id": "exp-001-neu-ta",
            "company": "Northeastern University",
            "role": "Teaching Assistant",
            "start_date": "September 2024",
            "end_date": "April 2025",
            "location": "Boston, MA",
            "bullets": [
                "Served as Teaching Assistant for Computer Science courses across two consecutive semesters, supporting students in mastering data structures, algorithms, and software engineering.",
                "Held office hours and developed supplementary materials to help students debug code and understand complex technical paradigms.",
            ],
            "tech": ["Java", "Python", "Data Structures", "Algorithms"],
        },
        {
            "id": "exp-002-tcs",
            "company": "Tata Consultancy Services",
            "role": "Assistant System Engineer",
            "start_date": "June 2022",
            "end_date": "January 2024",
            "location": "Mumbai, India",
            "bullets": [
                "Improved API response times by 20% (500ms to 400ms) for a telecom client's backend handling 5,000+ daily requests by optimizing Java/Spring Boot microservices with query caching and load balancing.",
                "Reduced release cycle from 4 weeks to 3 weeks across 3 microservices by automating build and deployment workflows using Docker and Jenkins.",
                "Improved database query execution time by 30% (1,000ms to 700ms) for enterprise client workloads by restructuring queries and adding targeted indexes on internal simulation tools.",
            ],
            "tech": ["Java", "Spring Boot", "Docker", "Jenkins", "RESTful APIs", "SQL"],
        },
        {
            "id": "exp-003-rawat",
            "company": "Rawat Soaps and Chemicals",
            "role": "Freelance Software Engineer",
            "start_date": "January 2022",
            "end_date": "April 2022",
            "location": "Indore, India",
            "bullets": [
                "Reduced processing errors by 50% across 80+ workflows by building a cross-platform inventory management app using Flutter and Firebase that digitized all manual operations.",
                "Saved an estimated $15,000 annually in material wastage (42% reduction) by implementing real-time raw materials tracking with Firestore data sync.",
            ],
            "tech": ["Flutter", "Firebase", "Dart", "Android", "iOS", "Firestore"],
        },
        {
            "id": "exp-004-aiesec",
            "company": "AIESEC in India",
            "role": "Marketing Team Member",
            "start_date": "August 2018",
            "end_date": "February 2019",
            "location": "Indore, India",
            "bullets": [
                "Built the official website and landing pages for AIESEC in Indore to help visitors learn about the organization's programs.",
                "Developed a digital contact portal making it easier for students and partners to reach out with questions.",
                "Designed a clean, easy-to-navigate layout to ensure users could find information quickly on both mobile and desktop.",
            ],
            "tech": ["HTML", "CSS", "JavaScript", "Web Development"],
        },
    ],
    "skills": {
        "languages": ["Java", "Python", "Dart", "SQL", "JavaScript", "HTML/CSS"],
        "frameworks": ["Spring Boot", "Flutter", "Firebase", "RESTful APIs"],
        "tools": ["Docker", "Jenkins", "Git"],
        "clouds": ["Google Cloud Platform (GCP)"],
    },
    "education": [
        {
            "id": "edu-001-neu",
            "school": "Northeastern University",
            "degree": "Master of Science",
            "field": "Computer Science",
            "start_date": "January 2024",
            "end_date": "December 2025",
            "gpa": "",
        },
        {
            "id": "edu-002-medicaps",
            "school": "Medi-Caps University",
            "degree": "Bachelor of Technology",
            "field": "Computer Science",
            "start_date": "August 2018",
            "end_date": "May 2022",
            "gpa": "",
        },
    ],
    "target_roles": [
        "Software Engineer",
        "Software Development Engineer",
        "Full Stack Developer",
        "Backend Engineer",
    ],
    "target_locations": ["San Francisco Bay Area", "Remote"],
    "min_match_score": 65,
    "narrative": {
        "answers": [],
        "summary": (
            "I'm a software engineer passionate about building impactful, scalable solutions—"
            "from optimizing Java/Spring Boot microservices at Tata Consultancy Services to "
            "winning hackathons at Northeastern's Roux Institute. Currently pursuing my MS in "
            "Computer Science at Northeastern, I bring back-end engineering depth and mobile "
            "development experience with a track record of measurable wins: 20% faster API "
            "response times, 30% query optimization, and $15K in annual client savings. "
            "I thrive where I can dig deep into technical problems and collaborate with "
            "cross-functional teams to ship meaningful products."
        ),
    },
}


def apply_seed(db: Session) -> Profile:
    """Merge PROFILE_SEED into the existing profile, overwriting those keys."""
    profile = get_or_create_profile(db)
    updated = copy.deepcopy(profile.data or {})
    updated.update(PROFILE_SEED)
    profile.data = updated
    flag_modified(profile, "data")
    db.commit()
    return profile


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
