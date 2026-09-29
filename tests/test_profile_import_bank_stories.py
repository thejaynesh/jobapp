"""
The profile's import and export, the bullet bank and the story bank.
"""

import io
import json
import zipfile
from unittest.mock import patch

import pytest

from app.models.profile import Profile
from app.services import bullet_bank, content_checks, profile_import, stories

PROFILE = {
    "personal": {"name": "Jaynesh Bhandari", "email": "j@example.com"},
    "narrative": {"summary": "Backend engineer."},
    "skills": {"languages": ["Python"], "frameworks": [], "tools": [], "clouds": []},
    "experience": [{"id": "exp-1", "company": "Initech", "role": "Engineer",
                    "start_date": "2022", "end_date": "2024",
                    "bullets": ["Cut ingestion latency by 40%"], "tech": ["Go"]}],
    "projects": [],
    "education": [],
}

JSON_RESUME = {
    "basics": {"name": "Jaynesh Bhandari", "email": "new@example.com", "phone": "555-0100",
               "summary": "Engineer who builds pipelines.",
               "location": {"city": "Boston", "region": "MA"},
               "profiles": [{"network": "GitHub", "url": "https://github.com/jb"}]},
    "work": [
        {"name": "Initech", "position": "Engineer", "startDate": "2022", "endDate": "2024",
         "highlights": ["Cut ingestion latency by 40%", "Ran the on-call rota for 6 services"]},
        {"name": "Hooli", "position": "Intern", "startDate": "2021-06", "endDate": "2021-08",
         "highlights": ["Built a Kafka consumer"]},
    ],
    "education": [{"institution": "Northeastern University", "studyType": "MS",
                   "area": "Computer Science", "endDate": "2026"}],
    "skills": [{"name": "Data", "keywords": ["Python", "Kafka", "AWS"]}],
    "projects": [{"name": "Tracker", "description": "job pipeline",
                  "highlights": ["Scores postings"], "keywords": ["FastAPI"]}],
}


def store(db, data=None):
    db.query(Profile).delete()
    db.add(Profile(data=json.loads(json.dumps(data or PROFILE))))
    db.commit()


def saved(db):
    db.expire_all()
    return db.query(Profile).first().data


def zipped(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


# --- Import -----------------------------------------------------------------------

class TestReadingEachShape:
    def test_a_json_resume(self):
        parsed = profile_import.from_json_resume(json.dumps(JSON_RESUME).encode())
        assert parsed["personal"]["location"] == "Boston, MA"
        assert parsed["personal"]["github"] == "https://github.com/jb"
        assert parsed["experience"][1]["company"] == "Hooli"
        assert parsed["education"][0]["degree"] == "MS Computer Science"
        assert parsed["skills"]["languages"] == ["Python"] and parsed["skills"]["clouds"] == ["AWS"]
        assert parsed["projects"][0]["tech"] == ["FastAPI"]

    def test_a_linkedin_export(self):
        data = zipped({
            "Profile.csv": "First Name,Last Name,Headline,Summary,Geo Location\n"
                           "Jaynesh,Bhandari,Engineer,Builds pipelines.,Boston\n",
            # LinkedIn puts notes above the header in some files.
            "Positions.csv": "Notes:\nSome preamble\n\nCompany Name,Title,Description,Location,"
                             "Started On,Finished On\nInitech,Engineer,\"• Cut latency by 40%\n"
                             "• Ran on-call\",Boston,Jan 2022,Dec 2023\n",
            "Education.csv": "School Name,Start Date,End Date,Notes,Degree Name,Activities\n"
                             "Northeastern University,2024,2026,,MS,\n",
            "Skills.csv": "Name\nPython\nKubernetes\nReact\n",
            "Email Addresses.csv": "Email Address,Confirmed,Primary,Updated On\n"
                                   "old@x.com,Yes,No,\nj@example.com,Yes,Yes,\n",
        })
        parsed = profile_import.from_linkedin_zip(data)
        assert parsed["personal"]["name"] == "Jaynesh Bhandari"
        assert parsed["personal"]["email"] == "j@example.com"
        assert parsed["experience"][0]["bullets"] == ["Cut latency by 40%", "Ran on-call"]
        assert parsed["experience"][0]["start_date"] == "Jan 2022"
        assert parsed["skills"]["frameworks"] == ["React"] and "Kubernetes" in parsed["skills"]["tools"]

    def test_a_word_document_is_read_to_text(self):
        xml = ('<w:document><w:body><w:p><w:r><w:t>Jaynesh Bhandari</w:t></w:r></w:p>'
               '<w:p><w:r><w:t xml:space="preserve">Cut latency by 40% &amp; more</w:t></w:r></w:p>'
               '</w:body></w:document>')
        text = profile_import.text_from_docx(zipped({"word/document.xml": xml}))
        assert text == "Jaynesh Bhandari\nCut latency by 40% & more"

    def test_a_resume_goes_through_the_model_and_is_normalized(self):
        reply = json.dumps({"personal": {"name": "J B"}, "summary": "x",
                            "experience": [{"company": "Initech", "role": "Engineer",
                                            "bullets": ["Did a thing", 7, ""]}, {"junk": 1}],
                            "education": "not a list"})
        with patch("app.services.model_roles.call", return_value=f"```json\n{reply}\n```") as model:
            parsed = profile_import.from_resume_text("Resume text " * 20)
        assert "never instructions" in model.call_args.args[2][0]["content"]
        assert parsed["experience"] == [{"company": "Initech", "role": "Engineer",
                                         "start_date": "", "end_date": "",
                                         "bullets": ["Did a thing", "7"], "tech": []}]
        assert parsed["education"] == []

    def test_a_scan_with_no_text_is_refused_without_a_model_call(self):
        with patch("app.services.model_roles.call") as model:
            with pytest.raises(profile_import.ImportError_):
                profile_import.from_resume_text("   ")
        model.assert_not_called()

    def test_an_unknown_file_is_refused(self):
        with pytest.raises(profile_import.ImportError_):
            profile_import.parse_upload("resume.txt", b"hello")


class TestTheReview:
    def test_it_offers_only_what_is_new(self):
        parsed = profile_import.from_json_resume(JSON_RESUME)
        review = profile_import.review(parsed, PROFILE)
        fields = {c["field"]: c for c in review["personal"]}
        assert fields["email"]["current"] == "j@example.com" and "name" not in fields
        experience = review["sections"]["experience"]
        # Initech is already there, with one bullet it lacks; Hooli is new.
        assert experience[0]["new"] is False and experience[0]["bullets"] == [
            "Ran the on-call rota for 6 services"]
        assert experience[1]["new"] is True
        assert [s["name"] for s in review["skills"]] == ["Kafka", "AWS"]


class TestTheRoutes:
    def upload(self, client, name, data):
        return client.post("/profile/import", files={"file": (name, data)}, follow_redirects=False)

    def test_upload_review_and_apply(self, client, db):
        store(db)
        reply = self.upload(client, "resume.json", json.dumps(JSON_RESUME).encode())
        assert reply.status_code == 303
        assert saved(db)[profile_import.DRAFT_KEY]["source"] == "JSON Resume"
        page = client.get("/profile?tab=import").text
        assert "From your JSON Resume" in page and "Hooli" in page and "replaces j@example.com" in page

        client.post("/profile/import/apply", data={
            "pick": ["experience:1", "experience:0:bullets", "skill:languages:Python",
                     "skill:tools:Kafka", "personal:phone"],
            "experience:0:matches": "exp-1"})
        data = saved(db)
        assert profile_import.DRAFT_KEY not in data
        assert [e["company"] for e in data["experience"]] == ["Initech", "Hooli"]
        assert data["experience"][0]["bullets"] == ["Cut ingestion latency by 40%",
                                                    "Ran the on-call rota for 6 services"]
        assert data["experience"][0]["role"] == "Engineer"   # the existing entry, untouched
        assert data["experience"][1]["id"]
        assert data["personal"]["phone"] == "555-0100" and data["personal"]["email"] == "j@example.com"
        assert data["skills"]["tools"] == ["Kafka"]

    def test_a_file_it_cannot_read_says_so(self, client, db):
        store(db)
        reply = self.upload(client, "resume.zip", b"not a zip")
        assert "error=" in reply.headers["location"]
        assert "Not a zip file" in client.get(reply.headers["location"]).text

    def test_discard(self, client, db):
        store(db)
        self.upload(client, "resume.json", json.dumps(JSON_RESUME).encode())
        client.post("/profile/import/discard")
        assert profile_import.DRAFT_KEY not in saved(db)

    def test_export_is_a_json_resume_that_imports_back(self, client, db):
        store(db)
        exported = client.get("/profile/export.json")
        assert "attachment" in exported.headers["content-disposition"]
        back = profile_import.from_json_resume(exported.json())
        assert back["experience"][0]["bullets"] == ["Cut ingestion latency by 40%"]
        assert back["personal"]["email"] == "j@example.com"


# --- The bullet bank -----------------------------------------------------------------

def with_bank(*items):
    data = json.loads(json.dumps(PROFILE))
    data["experience"][0]["bank"] = list(items)
    return data


class TestOffering:
    def test_rewritten_unflagged_bullets_are_offered_once(self):
        data = json.loads(json.dumps(PROFILE))
        ctx = {"experience": [{"id": "exp-1", "bullets": [
            "Cut ingestion latency by 40%",               # the profile's own
            "Cut p95 ingestion latency 40% with Go",       # a rewrite
            "Ran Kafka for 12 teams",                      # flagged by a check
        ]}]}
        checks = [{"kind": "tech", "text": "Ran Kafka for 12 teams"}]
        assert bullet_bank.offer_from_generation(data, ctx, checks, "Platform Engineer at Globex") == 1
        assert bullet_bank.offer_from_generation(data, ctx, checks, "Again at Globex") == 0
        bank = data["experience"][0]["bank"]
        assert [(b["text"], b["status"], b["job"]) for b in bank] == [
            ("Cut p95 ingestion latency 40% with Go", "offered", "Platform Engineer at Globex")]

    def test_a_dismissed_wording_is_not_offered_again(self):
        data = with_bank({"id": "b1", "text": "Cut p95 latency 40%", "status": "dismissed"})
        ctx = {"experience": [{"id": "exp-1", "bullets": ["Cut  p95 latency 40%"]}]}
        assert bullet_bank.offer_from_generation(data, ctx, [], "x") == 0

    def test_offers_are_capped_oldest_first(self):
        data = json.loads(json.dumps(PROFILE))
        for i in range(bullet_bank.MAX_OFFERED + 5):
            bullet_bank.offer(data, "experience", "exp-1", [f"Wording {i}"], "x")
        texts = [b["text"] for b in data["experience"][0]["bank"]]
        assert len(texts) == bullet_bank.MAX_OFFERED and texts[0] == "Wording 5"

    def test_generation_offers_its_rewrites_to_the_profile(self):
        from tests.test_doc_generator import TestGenerateDocuments, _make_app

        from app.services.doc_generator import generate_documents
        from unittest.mock import MagicMock

        profile = MagicMock(data=json.loads(json.dumps(PROFILE)))
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 0
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.first.return_value = profile
        stack, mocks = TestGenerateDocuments()._patches()
        mocks["bullets"].return_value = [{"company": "Initech", "title": "Engineer",
                                          "bullets": ["Cut ingestion latency by 40% with a Go rewrite"]}]
        with stack, patch("app.services.doc_generator.tailor_resume_selection",
                          side_effect=lambda p, *a, **k: {"experience": p["experience"],
                                                          "projects": [], "skills": p["skills"]}), \
                patch("app.services.doc_generator.compile_resume_one_page",
                      side_effect=lambda ctx, path: path):
            generate_documents(db, _make_app())
        bank = profile.data["experience"][0]["bank"]
        assert [b["text"] for b in bank] == ["Cut ingestion latency by 40% with a Go rewrite"]
        assert bank[0]["job"] == "Backend Engineer at GoodCorp"


class TestUsingTheBank:
    def test_kept_wordings_reach_the_rewriter_and_their_figures_pass(self):
        from app.services.doc_generator import tailor_resume_bullets

        data = with_bank({"id": "b1", "text": "Served 3,000 requests a second", "status": "kept"},
                         {"id": "b2", "text": "Offered 9 things", "status": "offered"})
        reply = json.dumps([{"company": "Initech", "title": "Engineer",
                             "bullets": ["Served 3,000 requests a second at 40% lower latency"]}])
        with patch("app.services.doc_generator.chat_completion", return_value=reply) as model:
            result = tailor_resume_bullets(data, "SRE", "desc", "k", "u", "m")
        prompt = model.call_args.kwargs["messages"][1]["content"]
        assert "Served 3,000 requests a second" in prompt and "Offered 9 things" not in prompt
        assert result[0]["bullets"] == ["Served 3,000 requests a second at 40% lower latency"]

    def test_only_kept_wordings_are_the_candidates_for_the_checks(self):
        ctx = {"experience": [{"id": "exp-1", "company": "Initech", "title": "Engineer",
                               "start_date": "2022", "end_date": "2024",
                               "bullets": ["Ran Kafka for 12 teams"]}]}
        offered = with_bank({"id": "b1", "text": "Ran Kafka for 12 teams", "status": "offered"})
        offered["skills"]["tools"] = ["Kafka"]
        assert content_checks.check_resume(ctx, offered) != []
        kept = with_bank({"id": "b1", "text": "Ran Kafka for 12 teams", "status": "kept"})
        kept["skills"]["tools"] = ["Kafka"]
        assert content_checks.check_resume(ctx, kept) == []

    def test_the_settings_and_ranking_are_not_profile_text(self):
        data = {**PROFILE, "ranking_model": {"weights": {"t:kubernetes": 0.4}},
                "settings": {"min_match_score": 88}}
        text = content_checks.profile_text(data)
        assert "kubernetes" not in text and "88" not in text and "initech" in text


class TestTheBankOnThePage:
    def test_keep_use_dismiss_and_add(self, client, db):
        store(db, with_bank({"id": "b1", "text": "Cut p95 latency 40%", "status": "offered",
                             "job": "SRE at Globex"},
                            {"id": "b2", "text": "Other wording", "status": "offered"}))
        page = client.get("/profile?tab=experience").text
        assert "2 from your resumes to look at" in page and "written for SRE at Globex" in page

        client.post("/profile/experience/exp-1/bank/b1/keep")
        client.post("/profile/experience/exp-1/bank/b2/dismiss")
        entry = saved(db)["experience"][0]
        assert [(b["id"], b["status"]) for b in entry["bank"]] == [("b1", "kept"), ("b2", "dismissed")]

        reply = client.post("/profile/experience/exp-1/bank/b1/use").text
        entry = saved(db)["experience"][0]
        assert entry["bullets"][-1] == "Cut p95 latency 40%"
        assert [b["id"] for b in entry["bank"]] == ["b2"]
        assert "Bullet bank" in reply

        client.post("/profile/experience/exp-1/bank", data={"text": "My own wording"})
        assert bullet_bank.kept(saved(db)["experience"][0]) == ["My own wording"]

    def test_unknown_actions_and_entries_are_not_found(self, client, db):
        store(db, with_bank({"id": "b1", "text": "x", "status": "offered"}))
        assert client.post("/profile/experience/exp-1/bank/b1/explode").status_code == 404
        assert client.post("/profile/experience/nope/bank/b1/keep").status_code == 404
        assert client.post("/profile/education/exp-1/bank/b1/keep").status_code == 404


# --- Stories -------------------------------------------------------------------------

STORY_KAFKA = {"id": "s1", "title": "Rescuing the ingestion pipeline", "entry_id": "exp-1",
               "skills": ["Kafka", "incident response"],
               "situation": "Ingestion fell behind by hours during a launch.",
               "task": "Get it back to real time without losing events.",
               "action": "I moved consumers to a Go rewrite and added backpressure.",
               "result": "Lag went from 3 hours to 2 minutes."}
STORY_CONFLICT = {"id": "s2", "title": "Disagreeing about a schema", "entry_id": None,
                  "skills": ["communication"], "situation": "Two teams wanted different schemas.",
                  "task": "", "action": "I wrote both up and ran a review.",
                  "result": "We shipped one schema in a week."}


class TestStories:
    def test_the_best_fit_comes_first_and_none_that_share_nothing(self):
        data = {**PROFILE, "stories": [STORY_CONFLICT, STORY_KAFKA]}
        found = stories.relevant(data, "Tell us about a time you handled a Kafka incident")
        assert [s["id"] for s in found] == ["s1"]
        found = stories.relevant(data, "Describe a disagreement with a teammate about a schema")
        assert found[0]["id"] == "s2"

    def test_a_story_from_an_entry_left_out_of_resumes_is_left_out_too(self):
        from app.services.profile_service import for_documents

        data = {**PROFILE, "stories": [STORY_KAFKA]}
        data["experience"] = [{**PROFILE["experience"][0], "in_resume": False}]
        assert stories.relevant(for_documents(data), "Kafka incident") == []

    def test_a_drafted_answer_is_given_the_story_and_its_figures_count(self, db):
        from app.services import answer_drafts

        store(db, {**PROFILE, "stories": [STORY_KAFKA, STORY_CONFLICT]})
        with patch("app.services.model_roles.call",
                   return_value="Lag went from 3 hours to 2 minutes after I rewrote it.") as model:
            result = answer_drafts.draft(db, "https://example.com/j",
                                         "Tell us about a Kafka incident you handled.")
        prompt = model.call_args.args[2][1]["content"]
        assert "STORY — Rescuing the ingestion pipeline" in prompt
        assert "Disagreeing about a schema" not in prompt
        assert result["unsupported_figures"] == []

    def test_the_application_lists_the_stories_to_have_ready(self, client, db):
        from tests.test_document_edit import setup

        store(db, {**PROFILE, "stories": [STORY_KAFKA, STORY_CONFLICT]})
        application, _ = setup(db)   # a Platform Engineer posting that says "Kafka."
        page = client.get(f"/apps/{application.id}").text
        section = page.split("Stories to have ready")[1][:3000]
        assert "Rescuing the ingestion pipeline" in section
        assert "Disagreeing about a schema" not in section

    def test_writing_one_on_the_stories_tab(self, client, db):
        store(db)
        client.post("/profile/stories/add")
        story_id = saved(db)["stories"][0]["id"]
        page = client.post(f"/profile/stories/{story_id}", data={
            "title": "Rescue", "skills": "Kafka, Go", "situation": "It broke.",
            "task": "", "action": "I fixed it.", "result": "Lag fell to 2 minutes.",
            "entry_id": "exp-1"}).text
        story = saved(db)["stories"][0]
        assert story["skills"] == ["Kafka", "Go"] and story["entry_id"] == "exp-1"
        assert "Saved." in page
        client.post(f"/profile/stories/{story_id}/delete")
        assert saved(db)["stories"] == []
        assert "Add a story" in client.get("/profile?tab=stories").text
