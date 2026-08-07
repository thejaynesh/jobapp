"""
Show the prompt the matcher actually sends, and what's missing from it.

"Is my profile filled in properly?" isn't answerable from the profile form: the
form shows what you typed, not what survives into the prompt. Fields get read
under names nothing writes, blank sections vanish silently, and a rubric that
spends 25 points on years of experience says nothing when the years aren't
there. The only honest answer is the assembled messages themselves.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.job import Job

# A stand-in when there's nothing fetched yet, so the preview works on a fresh
# install. Deliberately ordinary — the point is the profile half, not this.
SAMPLE_JOB = {
    "title": "Backend Engineer",
    "company": "Example Corp",
    "location": "Remote (US)",
    "is_remote": True,
    "experience_level": "mid",
    "description": (
        "We're looking for a backend engineer to build and operate our API "
        "platform. You'll work in Python and Go, with PostgreSQL and Redis, "
        "deploying to AWS on Kubernetes.\n\n"
        "Requirements: 3+ years of backend experience, strong SQL, and "
        "experience with distributed systems. Nice to have: Kafka, Terraform."
    ),
}


class _StubJob:
    def __init__(self, data: dict):
        self.__dict__.update(data)
        self.id = "sample"


def pick_job(db: Session, job_id: str | None = None):
    """A real stored job to preview against, falling back to the sample."""
    query = db.query(Job).filter(Job.description.isnot(None), Job.description != "")
    job = None
    if job_id:
        job = query.filter(Job.id == job_id).first()
    if job is None:
        job = query.order_by(Job.fetched_at.desc()).first()
    return job or _StubJob(SAMPLE_JOB)


def _missing(profile_data: dict, total_years: float) -> list[str]:
    """
    Profile gaps that measurably change the score, worst first.

    Only things the rubric actually consumes — listing every empty key would
    bury the two or three that cost real points.
    """
    gaps = []
    personal = profile_data.get("personal") or {}
    skills = profile_data.get("skills") or {}
    flat_skills = [s for group in skills.values() for s in (group or [])]

    if not flat_skills:
        gaps.append("No skills listed — the 40-point skill-overlap band has "
                    "nothing to match against.")
    if not profile_data.get("target_roles"):
        gaps.append("No target roles — the 20-point domain/role-fit band is "
                    "guesswork, and the senior-title prefilter can't tell "
                    "which senior words are ones you actually want.")
    if not profile_data.get("experience"):
        gaps.append("No experience entries — the 25-point seniority band has "
                    "no history to judge against.")
    elif not total_years:
        gaps.append("Experience dates don't parse, so total years is unknown "
                    "and the 25-point seniority band is judged on job titles "
                    "alone. Use a form like \"Jun 2022\" or \"2022-06\".")
    if not (profile_data.get("narrative") or {}).get("summary"):
        gaps.append("No narrative summary — the model gets no sense of what "
                    "you're aiming for beyond the role titles.")
    if not personal.get("name"):
        gaps.append("No name set (cosmetic — it just reads \"Candidate\").")
    return gaps


def build(db: Session, job_id: str | None = None) -> dict:
    """Everything the preview panel renders."""
    from app.models.profile import Profile
    from app.services.experience import entry_years, total_years
    from app.services.matcher import _build_match_prompt

    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}
    job = pick_job(db, job_id)
    messages = _build_match_prompt(job, profile_data)

    experience = profile_data.get("experience") or []
    years = total_years(experience)

    return {
        "system": messages[0]["content"],
        "user": messages[1]["content"],
        "job": {"title": job.title, "company": job.company,
                "is_sample": isinstance(job, _StubJob)},
        "total_years": years,
        "per_role": [{"label": f"{e.get('role') or e.get('title') or '?'} "
                               f"@ {e.get('company') or '?'}",
                      "dates": " – ".join(x for x in (e.get("start_date"),
                                                      e.get("end_date")) if x),
                      "years": entry_years(e)}
                     for e in experience],
        "gaps": _missing(profile_data, years),
        "generated_at": datetime.now(timezone.utc),
    }
