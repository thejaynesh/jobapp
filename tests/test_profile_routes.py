def test_get_profile_returns_200(client):
    response = client.get("/profile")
    assert response.status_code == 200
    assert b"Profile" in response.content

def test_get_profile_tab_personal(client):
    response = client.get("/profile?tab=personal")
    assert response.status_code == 200

def test_get_profile_tab_narrative(client):
    response = client.get("/profile?tab=narrative")
    assert response.status_code == 200

def test_save_personal(client):
    response = client.post("/profile/personal", data={
        "name": "Jay Bhandari", "email": "jay@example.com",
        "phone": "555-1234", "linkedin": "linkedin.com/in/jay",
        "github": "github.com/jay", "location": "Boston, MA",
    })
    assert response.status_code == 200
    assert b"Jay Bhandari" in response.content

def test_save_personal_persists(client):
    client.post("/profile/personal", data={
        "name": "Persisted Jay", "email": "", "phone": "",
        "linkedin": "", "github": "", "location": ""
    })
    response = client.get("/profile?tab=personal")
    assert b"Persisted Jay" in response.content

def test_add_experience(client):
    response = client.post("/profile/experience/add")
    assert response.status_code == 200
    assert b"experience-list" in response.content

def test_save_experience_item(client):
    import re
    add_resp = client.post("/profile/experience/add")
    match = re.search(r'data-id="([^"]+)"', add_resp.text)
    assert match, "No data-id in response"
    item_id = match.group(1)
    save_resp = client.post(f"/profile/experience/{item_id}", data={
        "company": "Stripe", "role": "Software Engineer",
        "start_date": "2023-01", "end_date": "Present",
        "bullets": "Built payment APIs\nReduced latency by 40%",
        "tech": "Python, Go, PostgreSQL",
    })
    assert save_resp.status_code == 200
    assert b"Stripe" in save_resp.content

def test_delete_experience_item(client):
    import re
    add_resp = client.post("/profile/experience/add")
    item_id = re.search(r'data-id="([^"]+)"', add_resp.text).group(1)
    del_resp = client.delete(f"/profile/experience/{item_id}")
    assert del_resp.status_code == 200
    assert item_id.encode() not in del_resp.content

def test_add_project(client):
    response = client.post("/profile/projects/add")
    assert response.status_code == 200
    assert b"projects-list" in response.content

def test_save_skills(client):
    response = client.post("/profile/skills", data={
        "languages": "Python, Go", "frameworks": "FastAPI, React",
        "tools": "Docker, Git", "clouds": "AWS",
    })
    assert response.status_code == 200
    assert b"Python" in response.content

def test_add_education(client):
    response = client.post("/profile/education/add")
    assert response.status_code == 200
    assert b"education-list" in response.content

def test_save_templates(client):
    response = client.post("/profile/templates", data={
        "latex_template": r"\documentclass{article}\begin{document}Hello\end{document}",
        "cover_letter_template": "Dear Hiring Manager,",
    })
    assert response.status_code == 200
    assert b"documentclass" in response.content
