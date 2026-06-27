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
        "linkedin": "", "github": "", "website": "", "location": ""
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
    "personal": {
        "name": "Jaynesh Bhandari",
        "email": "thejaynesh@gmail.com",
        "phone": "+1 (207) 313-7210",
        "location": "San Jose, CA",
        "linkedin": "https://www.linkedin.com/in/thejaynesh",
        "github": "https://github.com/thejaynesh",
        "website": "https://jaynesh.dev",
    },
    "experience": [
        {
            "id": "exp-001-neu-ta",
            "company": "Northeastern University",
            "role": "Graduate Teaching Assistant",
            "start_date": "Sep 2024",
            "end_date": "Apr 2025",
            "location": "Portland, ME",
            "bullets": [
                "Supported 100+ students across two semesters in Programming Design Paradigm (Java full-stack) and Algorithms, by conducting office hours, grading assignments, and providing 1-on-1 debugging guidance.",
            ],
            "tech": ["Java", "Algorithms", "Data Structures"],
        },
        {
            "id": "exp-002-tcs",
            "company": "Tata Consultancy Services",
            "role": "Assistant System Engineer",
            "start_date": "Jun 2022",
            "end_date": "Jan 2024",
            "location": "Mumbai, India",
            "bullets": [
                "Improved API response times by 20% (500ms to 400ms) for a telecom client's backend handling 5,000+ daily requests, by optimizing Java/Spring Boot microservices with query caching and load balancing.",
                "Reduced release cycle from 4 weeks to 3 weeks across 3 microservices, by automating build and deployment workflows using Docker and Jenkins.",
                "Improved database query execution time by 30% (1,000ms to 700ms) for enterprise client workloads, by restructuring queries and adding targeted indexes on internal simulation tools.",
            ],
            "tech": ["Java", "Spring Boot", "Docker", "Jenkins", "Microservices", "PostgreSQL"],
        },
        {
            "id": "exp-003-rawat",
            "company": "Rawat Soap and Chemicals",
            "role": "Freelance Software Developer",
            "start_date": "Jan 2022",
            "end_date": "Apr 2022",
            "location": "Indore, India",
            "bullets": [
                "Reduced processing errors by 50% across 80+ workflows, by building a cross-platform inventory management app using Flutter and Firebase that digitized all manual operations.",
                "Saved an estimated $15,000 annually in material wastage (42% reduction), by implementing real-time raw materials tracking with Firestore data sync.",
            ],
            "tech": ["Flutter", "Firebase", "Dart", "Firestore", "Android", "iOS"],
        },
    ],
    "projects": [
        {
            "id": "proj-001-snapagent",
            "name": "SnapAgent",
            "description": "Native Android AI Photo Assistant",
            "url": "",
            "bullets": [
                "Enabled natural language photo queries (e.g., 'find all receipts from March and total them') with sub-500ms response for on-device tool execution, by building an agentic AI pipeline where Gemini orchestrates 5+ custom tools via multi-step tool-calling.",
                "Indexed 1,000+ photos with zero manual tagging and sub-100ms search, by fusing ML Kit (labels, OCR, face clustering) with TFLite image embeddings into a Room-backed vector store queried via cosine similarity.",
                "Made the app fully navigable for visually impaired users, by integrating Android AccessibilityService with Gemini Vision to auto-generate descriptive alt-text for every photo.",
            ],
            "tech": ["Android", "Kotlin", "Gemini API", "ML Kit", "TFLite", "LLM", "RAG", "Vector Embeddings", "Room"],
        },
        {
            "id": "proj-002-supertips",
            "name": "SuperTips",
            "description": "YouTube Creator SaaS Platform",
            "url": "https://supertips.store",
            "bullets": [
                "Onboarded 50 creators and 900 supporters, processing ~450,000 Rs in revenue over 3 months, by designing and shipping a full-stack SaaS on Next.js with App Router and TypeScript.",
                "Achieved zero failed payments across multiple gateways, by building a gateway-agnostic payment system with webhook signature verification and idempotent transaction reconciliation.",
                "Cut p95 API latency and reduced YouTube API quota usage, by building a TTL cache layer over aggregated Firestore metrics powering real-time OBS alerts and traffic dashboards.",
            ],
            "tech": ["Next.js", "TypeScript", "Firebase", "Firestore", "Payments", "REST APIs", "Full Stack"],
        },
        {
            "id": "proj-003-algoview",
            "name": "AlgoView",
            "description": "Interactive Algorithm Visualizer",
            "url": "",
            "bullets": [
                "Enabled real-time playback with step-by-step backtracking across 12+ algorithms, by engineering a visualization engine using Dart generator functions and custom history stacks.",
                "Created an interactive, paintable grid-based graph canvas for pathfinding visualizations (BFS, DFS, Dijkstra, A*) and achieved high reliability by implementing a comprehensive suite of unit and widget tests in Dart.",
            ],
            "tech": ["Flutter", "Dart", "Algorithms", "Data Structures", "BFS", "DFS", "Dijkstra"],
        },
    ],
    "skills": {
        "languages": ["Java", "Python", "Kotlin", "TypeScript", "JavaScript", "Dart"],
        "frameworks": ["Spring Boot", "Flutter", "Next.js", "Jetpack Compose"],
        "tools": ["Docker", "Git/GitHub", "GitHub Actions", "Jira", "Postman", "Linux", "Microservices", "REST APIs"],
        "clouds": ["Google Cloud Platform (GCP)", "Firebase", "AWS"],
        "databases": ["PostgreSQL", "MySQL", "MongoDB", "Firestore", "NoSQL"],
        "ai_ml": ["LLM Tool-Calling", "Gemini API", "ML Kit", "RAG", "Vector Embeddings", "TFLite"],
    },
    "education": [
        {
            "id": "edu-001-neu",
            "school": "Northeastern University",
            "degree": "Master of Science",
            "field": "Computer Science",
            "start_date": "Jan 2024",
            "end_date": "Dec 2025",
            "gpa": "3.7",
        },
        {
            "id": "edu-002-medicaps",
            "school": "Medi-Caps University",
            "degree": "Bachelor of Technology",
            "field": "Computer Science",
            "start_date": "Aug 2018",
            "end_date": "May 2022",
            "gpa": "",
        },
    ],
    "target_roles": [
        "Software Engineer",
        "Software Development Engineer",
        "Full Stack Developer",
        "Backend Engineer",
        "Android Engineer",
    ],
    "target_locations": ["Open to all locations", "Remote", "Relocation OK"],
    "min_match_score": 65,
    "narrative": {
        "answers": [],
        "summary": (
            "I'm a software engineer who gets energized by hard problems at the intersection of "
            "back-end systems and AI—whether that's shipping an agentic Android app with on-device "
            "LLM tool-calling or squeezing 30% more throughput out of telecom microservices at TCS. "
            "Currently finishing my MS in CS at Northeastern (3.7 GPA), I bring production experience "
            "across the full stack—Java/Spring Boot APIs, Flutter mobile, and Next.js SaaS—backed by "
            "a track record of measurable wins and two hackathon wins at Northeastern's Roux Institute."
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
