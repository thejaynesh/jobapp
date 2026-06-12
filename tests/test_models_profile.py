from app.models.profile import Profile


def test_create_profile(db):
    profile = Profile(data={
        "personal": {"name": "Jay", "email": "jay@example.com"},
        "experience": [],
        "projects": [],
        "skills": {"languages": ["Python"], "frameworks": [], "tools": [], "clouds": []},
        "education": [],
        "target_roles": ["Software Engineer"],
        "target_locations": ["Remote"],
        "excluded_companies": [],
        "min_match_score": 70,
        "narrative": {"answers": [], "summary": ""},
    })
    db.add(profile)
    db.flush()

    assert profile.id is not None
    assert profile.data["personal"]["name"] == "Jay"
    assert profile.updated_at is not None


def test_profile_data_update(db):
    profile = Profile(data={"personal": {"name": "Jay"}})
    db.add(profile)
    db.flush()

    profile.data = {**profile.data, "personal": {"name": "Jay Updated"}}
    db.flush()

    fetched = db.query(Profile).filter_by(id=profile.id).first()
    assert fetched.data["personal"]["name"] == "Jay Updated"
