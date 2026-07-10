"""Seed Jaynesh Bhandari profile data

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-24 00:00:00.000000

"""
from typing import Sequence, Union

import json
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROFILE_PATCH = {
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
                "Held office hours and developed supplementary materials to help students debug code and understand complex technical paradigms, improving student outcomes.",
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
    "projects": [],
    "target_roles": [
        "Software Engineer",
        "Software Development Engineer",
        "Full Stack Developer",
        "Backend Engineer",
    ],
    "target_locations": ["San Francisco Bay Area", "Remote"],
    "excluded_companies": [],
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


def upgrade() -> None:
    conn = op.get_bind()
    patch_json = json.dumps(PROFILE_PATCH)
    conn.execute(
        sa.text(
            # CAST(...) instead of ::jsonb — text() misparses "::" after a bindparam
            "UPDATE profiles SET data = data || CAST(:patch AS jsonb) "
            "WHERE id = (SELECT id FROM profiles ORDER BY id LIMIT 1)"
        ),
        {"patch": patch_json},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE profiles SET data = data - 'experience' - 'skills' - 'education' "
            "- 'projects' - 'target_roles' - 'target_locations' - 'narrative' "
            "WHERE id = (SELECT id FROM profiles ORDER BY id LIMIT 1)"
        )
    )
