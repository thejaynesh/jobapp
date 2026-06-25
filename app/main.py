from contextlib import asynccontextmanager
import copy
import logging
import subprocess

from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db, SessionLocal
from app.routers import profile as profile_router
from app.routers.docs import router as docs_router
from app.routers.jobs import router as jobs_router
from app.routers.apps import router as apps_router
from app.routers.settings import router as settings_router
from app.routers.outreach import router as outreach_router

logger = logging.getLogger(__name__)

_PROFILE_SEED = {
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


def _seed_profile_if_empty() -> None:
    from app.models.profile import Profile
    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        if not profile:
            logger.info("No profile found — skipping seed")
            return
        data = profile.data or {}
        if data.get("experience"):
            logger.info("Profile already has experience — skipping seed")
            return
        from sqlalchemy.orm.attributes import flag_modified
        updated = copy.deepcopy(data)
        updated.update(_PROFILE_SEED)
        profile.data = updated
        flag_modified(profile, "data")
        db.commit()
        logger.info("Profile seeded with experience, skills, education, and narrative")
    except Exception as exc:
        logger.error("Profile seed failed: %s", exc)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error("alembic upgrade failed: %s", result.stderr or result.stdout)
        else:
            logger.info("alembic upgrade head: %s", result.stdout.strip() or "up to date")
    except Exception as exc:
        logger.error("alembic upgrade error: %s", exc)
    _seed_profile_if_empty()
    yield


app = FastAPI(title="JobApp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(profile_router.router)
app.include_router(docs_router)
app.include_router(jobs_router)
app.include_router(apps_router)
app.include_router(settings_router)
app.include_router(outreach_router)


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


@app.get("/")
def root():
    return RedirectResponse(url="/apps")
