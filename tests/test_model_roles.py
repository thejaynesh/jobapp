"""
Which model does what, and where you change it.

Written because of a specific, fair complaint: "Work it out" — the crawl-recipe
button — was spending NIM calls while the free provider sat configured and
unused. It did that because the code it grew out of happened to pass NIM
credentials, not because anyone decided a rare button press should come out of
the paid budget.

The general problem behind it was worse. The application makes five quite
different kinds of LLM call and exactly one of them was selectable: a
"Matching model" dropdown. The other four chose their provider in code, in
four different places, and there was nowhere in the application that could tell
you which model was writing your covering letters.

So the calls are named, each resolves through one function, and each is
selectable. What these tests defend is that the naming is real — that changing
the setting changes the call, that "auto" means the role's own preference
order, and that a stale setting degrades rather than breaks.
"""

import pytest

from app.config import settings
from app.services import model_roles


@pytest.fixture
def providers(monkeypatch):
    """Every provider configured, so preference order is what decides."""
    monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "free-key")
    monkeypatch.setattr(settings, "FREEINFERENCE_MODEL", "glm-5.1")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "gemini-key")
    monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "nim-key")


class TestTheRolesAreReal:
    def test_every_role_has_a_setting(self):
        # A role the code uses that the settings page cannot show is exactly
        # the state this replaced, so the two lists have to be generated from
        # one another rather than kept in step by hand.
        from app.services.tunables import BY_KEY

        for role in model_roles.ROLES:
            assert model_roles.tunable_key(role.key) in BY_KEY

    def test_the_roles_cover_the_calls_the_app_makes(self):
        keys = {role.key for role in model_roles.ROLES}
        assert {"match", "match_deep", "generate", "extract", "learn"} <= keys


class TestAutoFollowsTheRolesOwnPreference:
    def test_learning_prefers_the_free_provider(self, providers):
        """
        The complaint, directly. Learning a recipe is a button somebody presses
        occasionally — it should take the free provider and leave the paid
        budget to the scoring passes that run thousands of times.
        """
        assert model_roles.resolve({}, "learn").name == "freeinference"

    def test_writing_documents_prefers_the_best_model(self, providers):
        # A handful of calls a day, and the only output a person reads.
        assert model_roles.resolve({}, "generate").name == "anthropic"

    def test_scoring_prefers_the_cheap_fast_one(self, providers):
        # Runs on every job that passes the keyword filter, so this is the
        # choice that decides what scoring costs.
        assert model_roles.resolve({}, "match").name == "nim"

    def test_roles_can_disagree(self, providers):
        # The whole point of naming them. One global "which model" setting
        # cannot express "the free one for this, the good one for that".
        assert model_roles.resolve({}, "learn").name != \
            model_roles.resolve({}, "generate").name

    def test_it_falls_past_a_provider_with_no_key(self, monkeypatch):
        monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "")
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
        monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "nim-key")
        assert model_roles.resolve({}, "learn").name == "nim"

    def test_nothing_configured_resolves_to_nothing(self, monkeypatch):
        for key in ("FREEINFERENCE_API_KEY", "ANTHROPIC_API_KEY",
                    "GEMINI_API_KEY", "NVIDIA_NIM_API_KEY"):
            monkeypatch.setattr(settings, key, "")
        assert model_roles.resolve({}, "learn") is None

    def test_an_unknown_role_resolves_to_nothing(self, providers):
        assert model_roles.resolve({}, "not-a-role") is None


class TestPinningARole:
    def _pinned(self, value):
        return {"settings": {"model_learn": value}}

    def test_a_pinned_choice_is_used(self, providers):
        provider = model_roles.resolve(
            self._pinned("anthropic:claude-opus-4-8"), "learn")
        assert provider.name == "anthropic"
        assert provider.model == "claude-opus-4-8"

    def test_pinning_one_role_leaves_the_others_alone(self, providers):
        data = self._pinned("anthropic:claude-opus-4-8")
        assert model_roles.resolve(data, "learn").name == "anthropic"
        assert model_roles.resolve(data, "match").name == "nim"

    def test_a_nim_model_can_be_pinned_by_name(self, providers):
        data = {"settings": {"model_match": "nim:meta/llama-3.1-8b-instruct"}}
        assert model_roles.resolve(data, "match").model == \
            "meta/llama-3.1-8b-instruct"

    def test_a_setting_naming_a_provider_that_is_gone_falls_back(self, monkeypatch):
        """
        Degrades rather than breaks. Pinning a provider and later removing its
        key is an ordinary thing to do, and the work should carry on rather
        than fail on a setting that describes a provider nobody has.
        """
        monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "free-key")
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "nim-key")
        provider = model_roles.resolve(
            self._pinned("anthropic:claude-opus-4-8"), "learn")
        assert provider is not None
        assert provider.name == "freeinference"

    def test_a_malformed_setting_falls_back(self, providers):
        assert model_roles.resolve(self._pinned("nonsense"), "learn") is not None


class TestWhatThePageCanOffer:
    def test_only_configured_providers_are_offered(self, monkeypatch):
        """
        Offering a model whose key is not set produces a setting that saves
        cleanly and then fails on the first call, which is the worst possible
        moment to find out.
        """
        monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "free-key")
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
        monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "")

        values = model_roles.choices("learn")
        assert any(v.startswith("freeinference:") for v in values)
        assert not any(v.startswith("anthropic:") for v in values)

    def test_auto_is_always_offered_and_comes_first(self, providers):
        assert model_roles.choices("learn")[0] == model_roles.AUTO

    def test_several_nim_models_are_offered(self, providers):
        nim = [v for v in model_roles.choices("match") if v.startswith("nim:")]
        assert len(nim) > 1


class TestWhatTheSettingsPageShows:
    def test_it_says_what_auto_currently_means(self, providers):
        # "Auto" on its own answers nothing. The question being asked is which
        # model is writing the covering letters.
        rows = {r["key"]: r for r in model_roles.describe({})}
        assert rows["generate"]["provider"] == "anthropic"
        assert rows["generate"]["model"]

    def test_it_marks_a_pinned_role(self, providers):
        rows = {r["key"]: r
                for r in model_roles.describe(
                    {"settings": {"model_learn": "anthropic:claude-opus-4-8"}})}
        assert rows["learn"]["setting"] != model_roles.AUTO
        assert rows["match"]["setting"] == model_roles.AUTO

    def test_it_survives_nothing_being_configured(self, monkeypatch):
        for key in ("FREEINFERENCE_API_KEY", "ANTHROPIC_API_KEY",
                    "GEMINI_API_KEY", "NVIDIA_NIM_API_KEY"):
            monkeypatch.setattr(settings, key, "")
        rows = model_roles.describe({})
        assert all(row["model"] == "" for row in rows)

    def test_the_page_renders_and_names_a_model(self, client, db, providers):
        page = client.get("/settings").text
        assert "Which model does what" in page
        assert "Writing documents" in page


class TestTheLearnersUseTheLearnRole:
    """
    The specific thing that was wrong. Both recipe learners assembled NIM
    credentials by hand, so the button spent paid calls whatever else was
    configured — and neither of them could be pointed anywhere else without a
    code change.
    """

    def test_the_crawl_learner_goes_through_the_role(self, db, providers, monkeypatch):
        from app.services import crawl_recipes

        seen = {}

        def _call(profile_data, role, messages, **kwargs):
            seen["role"] = role
            return '{"mode": "scroll", "scroll_passes": 100}'

        monkeypatch.setattr(model_roles, "call", _call)
        crawl_recipes.record(db, "x.test", "https://x.test/",
                             {"controls": [], "query": {}, "scroll": {}})
        crawl_recipes.learn(db, "x.test")
        assert seen["role"] == "learn"

    def test_the_harvest_learner_goes_through_the_role(self, db, providers, monkeypatch):
        from app.services import harvest_recipes, harvest_samples

        seen = {}

        def _call(profile_data, role, messages, **kwargs):
            seen["role"] = role
            return '{"roots": ["jobs"], "fields": {"title": ["title"]}}'

        monkeypatch.setattr(model_roles, "call", _call)
        harvest_samples.record(db, "x.test", {"jobs": [{"title": "Engineer"}]},
                               source_url="https://x.test/", found=0)
        harvest_recipes.learn(db, "x.test")
        assert seen["role"] == "learn"

    def test_a_pinned_learn_model_is_what_gets_used(self, db, providers, monkeypatch):
        from app.services import crawl_recipes

        seen = {}

        def _call(profile_data, role, messages, **kwargs):
            seen["provider"] = model_roles.resolve(profile_data, role)
            return '{"mode": "scroll", "scroll_passes": 100}'

        monkeypatch.setattr(model_roles, "call", _call)
        crawl_recipes.record(db, "x.test", "https://x.test/",
                             {"controls": [], "query": {}, "scroll": {}})
        crawl_recipes.learn(
            db, "x.test", {"settings": {"model_learn": "gemini:gemini-2.5-flash"}})
        assert seen["provider"].name == "gemini"
