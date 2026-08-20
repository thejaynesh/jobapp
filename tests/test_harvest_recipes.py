"""
Learning to read a site whose payload the generic walker cannot.

The walker takes any object holding a title, a company and an identifier. That
covers most boards without being told anything, and it fails two ways.

A payload can name its fields something no alias knows, and nothing comes out.
Or — the case that matters — it can be **normalized**: the job carries a
reference to a company stored elsewhere in the response. The walker then
matches `companyUrn`, and `urn:li:fsd_company:1234` is stored as an employer
name. Every downstream check passes, because title, company and URL are all
non-empty. The row is simply wrong, and nothing says so.

Following a reference across a payload is what a walker cannot do and a recipe
can, and most of these tests are about that specific case: that the join works,
and that a recipe which *fails* to resolve one is refused rather than trusted.

The other half is about not making anything worse. A recipe adds a way to read
a site; it must never be able to remove one, so the walker stays the fallback
and a recipe that throws costs nothing.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.harvest_recipe import HarvestRecipe, HarvestSample
from app.services import harvest_recipes, harvest_samples

# A normalized payload: the job points at its company rather than naming it.
# This is the shape the whole feature exists for.
NORMALIZED = {
    "data": {"jobSearch": {"results": [
        {"jobTitle": "Backend Engineer", "companyRef": "urn:co:1",
         "locationText": "London", "jobUrl": "https://x.test/jobs/1",
         "jobId": "j1", "descriptionHtml": "We need Python."},
        {"jobTitle": "Data Engineer", "companyRef": "urn:co:2",
         "locationText": "Remote", "jobUrl": "https://x.test/jobs/2",
         "jobId": "j2", "descriptionHtml": "We need SQL."},
    ]}},
    "included": [
        {"entityUrn": "urn:co:1", "name": "Acme Ltd"},
        {"entityUrn": "urn:co:2", "name": "Globex"},
    ],
}

RECIPE = {
    "roots": ["data.jobSearch.results"],
    "fields": {
        "title": ["jobTitle"], "company": ["companyRef"],
        "location": ["locationText"], "description": ["descriptionHtml"],
        "url": ["jobUrl"], "id": ["jobId"],
    },
    "join": {"ref": "companyRef", "table": "included",
             "key": "entityUrn", "take": "name", "into": "company"},
}


def sample(db, host="x.test", payload=None):
    row = HarvestSample(
        host=host, source_url=f"https://{host}/jobs/search",
        payload=payload if payload is not None else NORMALIZED,
        bytes=500, found=0,
    )
    db.add(row)
    db.commit()
    return row


class TestFollowingAReferenceAcrossThePayload:
    def test_the_company_comes_out_as_a_name(self, db):
        jobs = harvest_recipes.apply_recipe(NORMALIZED, RECIPE, "x_harvest")
        assert sorted(job["company"] for job in jobs) == ["Acme Ltd", "Globex"]

    def test_the_walker_alone_gets_this_wrong(self, db):
        # Not a criticism of the walker — it cannot follow a reference, and
        # this asserts the gap the recipe is for rather than a bug.
        from app.services.harvest import extract_jobs

        walked = extract_jobs(NORMALIZED, source="x_harvest")
        companies = [job["company"] for job in walked]
        assert not any(c in ("Acme Ltd", "Globex") for c in companies)

    def test_the_rest_of_the_fields_come_through(self, db):
        jobs = sorted(
            harvest_recipes.apply_recipe(NORMALIZED, RECIPE, "x_harvest"),
            key=lambda job: job["title"],
        )
        assert jobs[0]["title"] == "Backend Engineer"
        assert jobs[0]["location"] == "London"
        assert jobs[0]["url"] == "https://x.test/jobs/1"
        assert jobs[0]["source_job_id"] == "j1"
        assert "Python" in jobs[0]["description"]

    def test_a_remote_location_is_noticed(self, db):
        jobs = {job["title"]: job for job in
                harvest_recipes.apply_recipe(NORMALIZED, RECIPE, "x_harvest")}
        assert jobs["Data Engineer"]["is_remote"] is True

    def test_a_reference_with_no_match_leaves_the_raw_value(self, db):
        # Honest rather than clever: an unresolved reference stays visible, so
        # validation can refuse the recipe instead of the row looking fine.
        payload = {**NORMALIZED, "included": []}
        jobs = harvest_recipes.apply_recipe(payload, RECIPE, "x_harvest")
        assert all(job["company"].startswith("urn:") for job in jobs)

    def test_a_recipe_without_a_join_still_works(self, db):
        flat = {"jobs": [{"title": "SRE", "companyName": "Initech",
                          "url": "https://x.test/3", "id": "3"}]}
        recipe = {"roots": ["jobs"],
                  "fields": {"title": ["title"], "company": ["companyName"],
                             "url": ["url"], "id": ["id"]}}
        jobs = harvest_recipes.apply_recipe(flat, recipe, "x_harvest")
        assert jobs[0]["company"] == "Initech"


class TestPaths:
    def test_a_path_steps_through_arrays(self, db):
        # A model writes the shape it saw; a payload rarely says which levels
        # are lists.
        payload = {"groups": [{"items": [
            {"t": "A", "c": "Co", "u": "https://x/1", "i": "1"},
        ]}]}
        recipe = {"roots": ["groups.items"],
                  "fields": {"title": ["t"], "company": ["c"],
                             "url": ["u"], "id": ["i"]}}
        assert len(harvest_recipes.apply_recipe(payload, recipe, "s")) == 1

    def test_a_dotted_field_path_is_followed(self, db):
        payload = {"jobs": [{"title": "SRE", "employer": {"name": "Initech"},
                             "url": "https://x/1", "id": "1"}]}
        recipe = {"roots": ["jobs"],
                  "fields": {"title": ["title"], "company": ["employer.name"],
                             "url": ["url"], "id": ["id"]}}
        jobs = harvest_recipes.apply_recipe(payload, recipe, "s")
        assert jobs[0]["company"] == "Initech"

    def test_aliases_are_tried_in_order(self, db):
        payload = {"jobs": [{"headline": "SRE", "c": "Co",
                             "url": "https://x/1", "id": "1"}]}
        recipe = {"roots": ["jobs"],
                  "fields": {"title": ["jobTitle", "headline"], "company": ["c"],
                             "url": ["url"], "id": ["id"]}}
        assert harvest_recipes.apply_recipe(payload, recipe, "s")[0]["title"] == "SRE"

    def test_a_root_that_matches_nothing_yields_nothing(self, db):
        recipe = {"roots": ["nowhere.at.all"], "fields": {"title": ["t"]}}
        assert harvest_recipes.apply_recipe(NORMALIZED, recipe, "s") == []

    def test_a_job_missing_a_url_and_an_id_is_dropped(self, db):
        payload = {"jobs": [{"title": "SRE", "companyName": "Co"}]}
        recipe = {"roots": ["jobs"],
                  "fields": {"title": ["title"], "company": ["companyName"]}}
        assert harvest_recipes.apply_recipe(payload, recipe, "s") == []


class TestNothingIsTrustedOnTheModelsSayS0:
    def test_a_working_recipe_is_accepted(self, db):
        outcome = harvest_recipes.validate([NORMALIZED], RECIPE)
        assert outcome["ok"] is True
        assert outcome["jobs"] == 2

    def test_a_recipe_that_finds_nothing_is_refused(self, db):
        outcome = harvest_recipes.validate(
            [NORMALIZED], {"roots": ["nowhere"], "fields": {}}
        )
        assert outcome["ok"] is False
        assert "no jobs" in outcome["reason"]

    def test_a_recipe_that_leaves_urns_as_companies_is_refused(self, db):
        # The whole point. Without this check the feature would introduce the
        # exact bug it was built to fix, by a different route.
        no_join = {k: v for k, v in RECIPE.items() if k != "join"}
        outcome = harvest_recipes.validate([NORMALIZED], no_join)

        assert outcome["ok"] is False
        assert "ids" in outcome["reason"]

    def test_it_is_tried_against_every_sample(self, db):
        # A recipe that works only on the payload it was written from is the
        # likeliest failure, so one passing sample is not enough evidence.
        other = {"data": {"jobSearch": {"results": []}}, "included": []}
        outcome = harvest_recipes.validate([NORMALIZED, other], RECIPE)

        assert outcome["samples"] == 2
        assert outcome["matched_samples"] == 1

    def test_no_samples_is_not_a_pass(self, db):
        assert harvest_recipes.validate([], RECIPE)["ok"] is False

    @pytest.mark.parametrize("value,expected", [
        ("Acme Ltd", True), ("Stripe", True), ("R&D Partners", True),
        ("urn:li:fsd_company:1234", False), ("1234567", False),
        ("company:99", False), ("", False), ("x", False),
    ])
    def test_what_counts_as_a_name(self, value, expected):
        assert harvest_recipes.looks_like_a_name(value) is expected


class TestItCannotMakeThingsWorse:
    def test_a_recipe_that_throws_costs_nothing(self, db):
        # Returning nothing sends the payload to the walker, which is the
        # behaviour that existed before recipes did.
        assert harvest_recipes.apply_recipe(NORMALIZED, {"roots": [None]}, "s") == []

    def test_junk_where_a_recipe_should_be_is_survivable(self, db):
        for junk in (None, "recipe", 42, []):
            assert harvest_recipes.apply_recipe(NORMALIZED, junk, "s") == []

    def test_the_walker_is_the_fallback_not_the_other_way_round(self, db, client):
        # A recipe adds a way to read a site. It must not be able to remove one.
        from app.services import harvest_recipes as module

        flat = {"jobs": [{"title": "Backend Engineer", "companyName": "Acme",
                          "url": "https://flat.test/1", "jobPostingId": "991"}]}
        db.add(HarvestRecipe(host="flat.test", recipe={"roots": ["nowhere"]},
                             status="active"))
        db.commit()

        from app.routers.agent import _harvest

        counts = _harvest(db, flat, source_url="https://flat.test/search")
        assert counts["found"] == 1


class TestKeepingTheEvidence:
    def test_a_payload_we_cannot_read_is_kept(self, db):
        from app.routers.agent import _harvest

        _harvest(db, {"weird": {"stuff": 1}},
                 source_url="https://unknown.test/jobs")

        rows = harvest_samples.for_host(db, "unknown.test")
        assert len(rows) == 1
        assert rows[0].found == 0

    def test_a_payload_we_could_read_is_not(self, db):
        from app.routers.agent import _harvest

        good = {"jobs": [{"title": "Backend Engineer", "companyName": "Acme",
                          "url": "https://ok.test/1", "jobPostingId": "77"}]}
        _harvest(db, good, source_url="https://ok.test/jobs")

        assert harvest_samples.for_host(db, "ok.test") == []

    def test_samples_are_capped_per_host(self, db, monkeypatch):
        # A site failing every request would otherwise write one per response,
        # all saying the same thing.
        monkeypatch.setattr(settings, "HARVEST_SAMPLES_PER_HOST", 2)
        for n in range(6):
            harvest_samples.record(db, "x.test", {"n": n})
        db.commit()

        assert len(harvest_samples.for_host(db, "x.test", limit=99)) == 2

    def test_a_payload_is_trimmed_not_stored_whole(self, db):
        # These are responses to a logged-in session, kept for diagnosis. A
        # three-megabyte archive of them is a different thing.
        big = {"items": [{"description": "x" * 5000} for _ in range(200)]}
        harvest_samples.record(db, "big.test", big)
        db.commit()

        kept = harvest_samples.for_host(db, "big.test")[0]
        assert len(kept.payload["items"]) <= harvest_samples.MAX_ARRAY_ITEMS
        assert len(kept.payload["items"][0]["description"]) < 5000
        # The real size is still recorded, so the trimming is visible.
        assert kept.bytes > 100_000

    def test_trimming_keeps_the_shape(self, db):
        # A length is not a shape, and the shape is the useful part.
        trimmed = harvest_samples.trim({"a": [{"b": {"c": "d"}}] * 50})
        assert isinstance(trimmed["a"], list)
        assert trimmed["a"][0]["b"]["c"] == "d"

    def test_capture_can_be_turned_off(self, db, monkeypatch):
        monkeypatch.setattr(settings, "HARVEST_SAMPLES_ENABLED", False)
        assert harvest_samples.record(db, "x.test", {"a": 1}) is False

    def test_a_failed_capture_never_costs_the_harvest(self, db, monkeypatch):
        from app.services import harvest_samples as module

        monkeypatch.setattr(
            module, "_fits",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert module.record(db, "x.test", {"a": 1}) is False

    def test_old_samples_expire(self, db, monkeypatch):
        monkeypatch.setattr(settings, "HARVEST_SAMPLE_TTL_DAYS", 7)
        row = sample(db)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()

        assert harvest_samples.prune(db) == 1

    def test_samples_can_be_cleared_once_a_reader_works(self, db):
        sample(db)
        assert harvest_samples.clear(db, "x.test") == 1


class TestStoringWhatWasLearned:
    def _learn(self, db, reply=None):
        import json

        reply = reply if reply is not None else json.dumps({
            "roots": RECIPE["roots"], "fields": RECIPE["fields"],
            "join": RECIPE["join"], "note": "normalized payload",
        })
        with patch("app.services.matcher.chat_completion", return_value=reply):
            return harvest_recipes.learn(db, "x.test", "k", "https://x/v1", "m")

    def test_a_validated_recipe_becomes_active(self, db):
        sample(db)
        outcome = self._learn(db)

        assert outcome["ok"] is True
        row = db.query(HarvestRecipe).filter(HarvestRecipe.host == "x.test").one()
        assert row.status == "active"
        assert row.activated_at is not None

    def test_a_recipe_that_failed_validation_is_stored_but_not_used(self, db):
        # Kept because the note says what it got wrong, which is the only way
        # to tell "the model guessed badly" from "the site is unreadable".
        import json

        sample(db)
        outcome = self._learn(db, reply=json.dumps({"roots": ["nowhere"], "fields": {}}))

        assert outcome["ok"] is False
        row = db.query(HarvestRecipe).one()
        assert row.status == "proposed"
        assert harvest_recipes.active_for(db, "x.test") is None

    def test_activating_retires_the_previous_one(self, db):
        # One active recipe per host: two would make extraction depend on row
        # order, which fails looking exactly like the site changing.
        sample(db)
        self._learn(db)
        self._learn(db)

        active = db.query(HarvestRecipe).filter(
            HarvestRecipe.host == "x.test", HarvestRecipe.status == "active"
        ).all()
        assert len(active) == 1

    def test_the_recipe_is_used_on_the_next_harvest(self, db):
        from app.routers.agent import _harvest

        sample(db)
        self._learn(db)

        counts = _harvest(db, NORMALIZED, source_url="https://x.test/jobs/search")
        assert counts["found"] == 2

    def test_learning_needs_a_sample(self, db):
        assert self._learn(db)["ok"] is False

    def test_an_unusable_answer_is_reported_not_stored(self, db):
        sample(db)
        outcome = self._learn(db, reply="I could not work it out")

        assert outcome["ok"] is False
        assert db.query(HarvestRecipe).count() == 0

    def test_a_provider_outage_is_reported(self, db):
        sample(db)
        with patch("app.services.matcher.chat_completion",
                   side_effect=RuntimeError("down")):
            outcome = harvest_recipes.learn(db, "x.test", "k", "https://x/v1", "m")

        assert outcome["ok"] is False
        assert "down" in outcome["reason"]

    def test_a_bare_string_where_a_list_belongs_is_accepted(self, db):
        # A model asked for lists will sometimes answer with a string. That is
        # a shape to normalise, not a failure to report.
        import json

        sample(db)
        self._learn(db, reply=json.dumps({
            "roots": "data.jobSearch.results",
            "fields": {"title": "jobTitle", "company": "companyRef",
                       "url": "jobUrl", "id": "jobId"},
            "join": RECIPE["join"],
        }))
        row = db.query(HarvestRecipe).one()

        assert row.recipe["fields"]["title"] == ["jobTitle"]


class TestThePanel:
    @pytest.fixture(autouse=True)
    def _agent_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "test-token")

    def test_an_unreadable_host_is_listed_with_a_button(self, client, db):
        sample(db)
        body = client.get("/runs").text

        assert "x.test" in body
        assert "/runs/agent/learn" in body

    def test_a_host_with_no_samples_is_not_listed(self, client, db):
        assert "Payloads we can&#39;t read" not in client.get("/runs").text

    def test_a_refused_proposal_says_why(self, client, db):
        db.add(HarvestRecipe(host="x.test", recipe={"roots": []},
                             status="proposed", note="found no jobs in any sample"))
        db.commit()

        body = client.get("/runs").text
        assert "found no jobs in any sample" in body

    def test_pressing_learn_stores_a_recipe(self, client, db):
        import json

        sample(db)
        reply = json.dumps({"roots": RECIPE["roots"], "fields": RECIPE["fields"],
                            "join": RECIPE["join"]})
        with patch("app.services.matcher.chat_completion", return_value=reply):
            client.post("/runs/agent/learn", data={"host": "x.test"})

        assert harvest_recipes.active_for(db, "x.test") is not None

    def test_a_failure_does_not_take_the_page_down(self, client, db):
        sample(db)
        with patch("app.services.matcher.chat_completion",
                   side_effect=RuntimeError("boom")):
            assert client.post("/runs/agent/learn",
                               data={"host": "x.test"}).status_code == 200
