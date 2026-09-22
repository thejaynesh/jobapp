"""
Model lists edited on the settings page, and the dropdowns that read them.

NIM and FreeInference release models every few weeks. The lists used to be a
tuple in three source files; now they are data on the profile, and these tests
hold the part that matters: a model added on the page can then be *chosen* and
actually *used*, not merely stored.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.models.profile import Profile
from app.services import model_catalog, model_roles, tunables


class TestTheList:
    def test_defaults_include_the_environment_model(self):
        models = model_catalog.models({}, "nim")
        assert models[0] == settings.NVIDIA_NIM_MODEL
        assert "meta/llama-3.3-70b-instruct" in models

    def test_a_saved_list_replaces_the_defaults(self):
        data = model_catalog.store({}, "nim", ["vendor/new-model"])
        models = model_catalog.models(data, "nim")
        assert "vendor/new-model" in models
        assert "meta/llama-3.3-70b-instruct" not in models

    def test_the_environment_model_is_never_dropped(self):
        """The dropdown has to be able to show the model currently in use."""
        data = model_catalog.store({}, "nim", ["vendor/new-model"])
        assert settings.NVIDIA_NIM_MODEL in model_catalog.models(data, "nim")

    def test_pasting_accepts_lines_or_commas_and_reports_garbage(self):
        valid, rejected = model_catalog.parse("a/one\nb/two, c-three\n\nnot a model\n a/one ")
        assert valid == ["a/one", "b/two", "c-three"]
        assert rejected == ["not a model"]

    def test_reset_goes_back_to_defaults(self):
        data = model_catalog.store({}, "freeinference", ["x"])
        assert model_catalog.saved(model_catalog.reset(data, "freeinference"),
                                   "freeinference") is None


class TestTheDropdownsReadIt:
    def test_a_new_nim_model_is_a_matching_model_choice(self):
        data = model_catalog.store({}, "nim", ["vendor/new-model"])
        spec = tunables.BY_KEY["nvidia_nim_model"]
        assert "vendor/new-model" in tunables.choices_for(spec, data)

    def test_a_new_freeinference_model_can_be_pinned_to_a_role(self, monkeypatch):
        monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "k")
        data = model_catalog.store({}, "freeinference", ["glm-9-ultra"])
        assert "freeinference:glm-9-ultra" in model_roles.choices("generate", data)

    def test_the_pinned_new_model_is_the_one_called(self, monkeypatch):
        from app.llm.providers import generation_chat

        monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "k")
        data = model_catalog.store({}, "freeinference", ["glm-9-ultra"])
        data = tunables.apply_to_profile(
            data, tunables.parse_form({"model_generate": "freeinference:glm-9-ultra"}, data)
        )
        with patch("app.llm.providers.call_provider", return_value="ok") as call:
            generation_chat([], "k", "u", "m", role="generate", profile_data=data)
        first = call.call_args_list[0].args[0]
        assert (first.name, first.model) == ("freeinference", "glm-9-ultra")

    def test_the_runs_page_compares_from_the_list(self, db):
        from app.routers.runs import _nim_models

        db.add(Profile(data=model_catalog.store({}, "nim", ["vendor/new-model"])))
        db.commit()
        assert "vendor/new-model" in _nim_models(db)


class TestDiscovery:
    def test_it_reads_an_openai_style_model_list(self, monkeypatch):
        monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "k")
        resp = MagicMock()
        resp.json.return_value = {"data": [{"id": "b/two"}, {"id": "a/one"}, {"id": "bad id"}]}
        with patch("httpx.get", return_value=resp) as get:
            assert model_catalog.discover("nim") == ["a/one", "b/two"]
        assert get.call_args.args[0].endswith("/models")
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer k"

    def test_gemini_prefixes_are_stripped(self, monkeypatch):
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
        resp = MagicMock()
        resp.json.return_value = {"data": [{"id": "models/gemini-9-flash"}]}
        with patch("httpx.get", return_value=resp):
            assert model_catalog.discover("gemini") == ["gemini-9-flash"]

    def test_no_key_is_a_readable_error(self, monkeypatch):
        monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "")
        with pytest.raises(ValueError, match="no API key"):
            model_catalog.discover("freeinference")


class TestTheSettingsPage:
    def _profile(self, db, data=None):
        db.add(Profile(data=data or {}))
        db.commit()

    def test_the_section_is_rendered(self, client, db):
        self._profile(db)
        body = client.get("/settings").text
        assert "Model lists" in body
        assert 'id="models-nim"' in body
        assert 'id="models-freeinference"' in body

    def test_saving_a_list_stores_it(self, client, db):
        self._profile(db)
        response = client.post("/settings/models/nim",
                               data={"models": "vendor/new-model\nnot a model"})
        assert response.status_code == 200
        assert "not a model" in response.text  # reported, not silently dropped
        stored = db.query(Profile).first().data
        assert model_catalog.saved(stored, "nim") == ["vendor/new-model"]

    def test_an_empty_list_is_refused(self, client, db):
        self._profile(db)
        client.post("/settings/models/nim", data={"models": "   "})
        assert model_catalog.saved(db.query(Profile).first().data, "nim") is None

    def test_discovery_offers_only_what_is_new_and_adding_keeps_the_rest(
            self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "k")
        self._profile(db, model_catalog.store({}, "nim", ["a/one"]))
        resp = MagicMock()
        resp.json.return_value = {"data": [{"id": "a/one"}, {"id": "b/two"}]}
        with patch("httpx.get", return_value=resp):
            body = client.post("/settings/models/nim/discover").text
        assert 'value="b/two"' in body
        assert 'value="a/one"' not in body

        client.post("/settings/models/nim/add", data={"add": ["b/two"]})
        stored = model_catalog.saved(db.query(Profile).first().data, "nim")
        assert "a/one" in stored and "b/two" in stored

    def test_an_added_model_can_be_saved_as_the_matching_model(self, client, db):
        self._profile(db)
        client.post("/settings/models/nim", data={"models": "vendor/new-model"})
        client.post("/settings", data={"nvidia_nim_model": "vendor/new-model"})
        stored = db.query(Profile).first().data
        assert tunables.value(stored, "nvidia_nim_model") == "vendor/new-model"

    def test_an_unknown_provider_is_404(self, client, db):
        self._profile(db)
        assert client.post("/settings/models/bogus", data={"models": "x"}).status_code == 404
