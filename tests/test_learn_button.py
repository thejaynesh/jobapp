"""
The Learn button beside "Payloads we can't read" has to say what happened.

It re-rendered the panel and nothing else, so a refused proposal, a model
that was down and a success all looked like a button that did nothing. It also
showed the model the first three payloads stored — usually the site's
analytics — and had no way to be told where the jobs were.
"""

from unittest.mock import patch

from app.models.harvest_recipe import HarvestRecipe, HarvestSample
from app.services import harvest_recipes

JOBS = {"data": {"search": {"hits": [
    {"jobTitle": "Senior Backend Engineer", "company": {"displayName": "Acme"},
     "jobUrl": "https://board.test/j/1"},
    {"jobTitle": "Data Engineer", "company": {"displayName": "Beta"},
     "jobUrl": "https://board.test/j/2"},
]}}}
ANALYTICS = {"session": {"id": "abc", "flags": {"newNav": True}}}


def _store(db, payload, host="board.test"):
    db.add(HarvestSample(host=host, source_url=f"https://{host}/",
                         payload=payload, bytes=100, found=0))
    db.commit()


class TestTheHint:
    def test_a_title_you_can_see_builds_the_recipe_without_a_model(self, db):
        _store(db, ANALYTICS)
        _store(db, JOBS)
        with patch("app.services.harvest_recipes.propose") as model:
            out = harvest_recipes.learn(db, "board.test", hint="Senior Backend Engineer")
        model.assert_not_called()
        assert out["ok"] and out["jobs"] == 2
        assert db.query(HarvestRecipe).one().status == "active"

    def test_a_title_that_is_nowhere_says_so(self, db):
        _store(db, JOBS)
        out = harvest_recipes.learn(db, "board.test", hint="Underwater Welder")
        assert not out["ok"] and "does not appear" in out["reason"]


class TestJunkSamples:
    def test_analytics_only_is_named_rather_than_sent_to_a_model(self, db):
        _store(db, ANALYTICS)
        with patch("app.services.harvest_recipes.propose") as model:
            out = harvest_recipes.learn(db, "board.test")
        model.assert_not_called()
        assert "analytics" in out["reason"]

    def test_the_most_job_like_payload_is_shown_first(self, db):
        _store(db, JOBS)
        _store(db, ANALYTICS)  # newer, so it used to come first
        seen = {}

        def fake(samples, host, profile_data=None, **kwargs):
            seen["first"] = samples[0].payload
            return {"recipe": None, "error": "stop here"}

        with patch("app.services.harvest_recipes.propose", side_effect=fake):
            harvest_recipes.learn(db, "board.test")
        assert seen["first"] == JOBS


class TestTheButtonReports:
    def test_a_failure_is_shown(self, client, db):
        _store(db, ANALYTICS)
        body = client.post("/runs/agent/learn", data={"host": "board.test"}).text
        assert "not learned" in body and "analytics" in body

    def test_a_success_is_shown(self, client, db):
        _store(db, JOBS)
        body = client.post("/runs/agent/learn",
                           data={"host": "board.test", "hint": "Data Engineer"}).text
        assert "learned" in body and "2 job(s)" in body
