"""
The model-role selectors on the settings page have to change which model runs.

Four of the five ("Scoring jobs", "Second-pass scoring", "Writing documents",
"Reading a description") saved and were read by nothing — only "learn" went
through `model_roles`. Each test here stores a pin on the profile and asserts
the call actually goes to the pinned model, which is the failure CLAUDE.md
describes: a control that renders, saves, and changes nothing.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services import model_roles, tunables


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "fi-key")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "gm-key")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "nim-key")
    monkeypatch.setattr(settings, "MATCH_PRIMARY", "nim", raising=False)


def _pin(role: str, value: str) -> dict:
    return {tunables.STORE_KEY: {model_roles.tunable_key(role): value}}


def _served(calls):
    return [(c.args[0].name, c.args[0].model) for c in calls]


class TestAutoChangesNothing:
    def test_no_pin_means_no_override(self, providers):
        assert model_roles.pinned({}, "match") is None
        assert model_roles.pinned(_pin("match", "auto"), "match") is None


class TestGenerationAndExtraction:
    def test_a_pinned_generate_model_goes_first(self, providers):
        from app.llm.providers import generation_chat

        with patch("app.llm.providers.call_provider", return_value="ok") as call:
            generation_chat([{"role": "user", "content": "x"}], "k", "u", "m",
                            role="generate",
                            profile_data=_pin("generate", "gemini:gemini-9-pro"))
        assert _served(call.call_args_list)[0] == ("gemini", "gemini-9-pro")

    def test_without_a_pin_the_chain_is_unchanged(self, providers):
        from app.llm.providers import generation_chat

        with patch("app.llm.providers.call_provider", return_value="ok") as call:
            generation_chat([{"role": "user", "content": "x"}], "k", "u", "m",
                            role="generate", profile_data={})
        assert _served(call.call_args_list)[0][0] == "freeinference"

    def test_extraction_reads_its_own_role(self, providers):
        from app.llm.providers import generation_chat

        with patch("app.llm.providers.call_provider", return_value="ok") as call:
            generation_chat([{"role": "user", "content": "x"}], "k", "u", "m",
                            role="extract",
                            profile_data=_pin("extract", "nim:meta/llama-3.1-8b-instruct"))
        assert _served(call.call_args_list)[0] == ("nim", "meta/llama-3.1-8b-instruct")

    def test_the_chain_still_falls_back_when_the_pin_fails(self, providers):
        from app.llm.providers import generation_chat

        replies = [RuntimeError("down"), "ok"]
        with patch("app.llm.providers.call_provider", side_effect=replies) as call:
            assert generation_chat([{"role": "user", "content": "x"}], "k", "u", "m",
                                   role="generate",
                                   profile_data=_pin("generate", "gemini:gemini-9-pro")) == "ok"
        assert len(call.call_args_list) == 2

    def test_the_document_generator_asks_for_the_generate_role(self):
        from app.services import doc_generator

        with patch("app.services.doc_generator.generation_chat", return_value="ok") as chat:
            doc_generator.chat_completion(messages=[], api_key="k", base_url="u", model="m")
        assert chat.call_args.kwargs["role"] == "generate"


class TestScoring:
    def _job(self):
        job = MagicMock()
        job.id = "j1"
        return job

    def test_a_pinned_scoring_model_scores_the_job(self, providers):
        from app.services import matcher

        reply = '{"score": 80, "reasoning": "fit", "matched_skills": [], "missing_skills": []}'
        with patch("app.services.matcher._build_match_prompt", return_value=[]), \
                patch("app.services.matcher.call_provider", return_value=reply) as call, \
                patch("app.services.matcher.chat_completion") as nim:
            result = matcher._llm_score_job(
                self._job(), _pin("match", "gemini:gemini-9-flash"), "k", "u", "nim-default",
            )
        nim.assert_not_called()
        assert _served(call.call_args_list)[0] == ("gemini", "gemini-9-flash")
        assert result["scored_by"] == "gemini/gemini-9-flash"

    def test_a_pinned_nim_model_replaces_the_nim_default(self, providers):
        from app.services import matcher

        reply = '{"score": 80, "reasoning": "fit", "matched_skills": [], "missing_skills": []}'
        with patch("app.services.matcher._build_match_prompt", return_value=[]), \
                patch("app.services.matcher.chat_completion", return_value=reply) as nim:
            result = matcher._llm_score_job(
                self._job(), _pin("match", "nim:openai/gpt-oss-120b"), "k", "u", "nim-default",
            )
        assert nim.call_args.kwargs["model"] == "openai/gpt-oss-120b"
        assert result["scored_by"] == "nim/openai/gpt-oss-120b"


class TestSecondPass:
    def test_a_pinned_second_pass_model_goes_first(self, providers, monkeypatch):
        from app.services import matcher

        monkeypatch.setattr(settings, "DEEP_MATCH_BAND_LOW", 0, raising=False)
        monkeypatch.setattr(settings, "DEEP_MATCH_BAND_HIGH", 100, raising=False)
        job = MagicMock()
        job.id = "j1"
        job.matched_by = "nim/whatever"
        reply = '{"score": 70, "reasoning": "ok", "matched_skills": [], "missing_skills": []}'
        with patch("app.services.matcher._build_match_prompt", return_value=[]), \
                patch("app.services.matcher.call_provider", return_value=reply) as call:
            matcher._deep_score(job, _pin("match_deep", "freeinference:big-model"), 70.0)
        assert _served(call.call_args_list)[0] == ("freeinference", "big-model")

    def test_a_pin_equal_to_the_first_scorer_is_not_its_own_second_opinion(
            self, providers, monkeypatch):
        from app.services import matcher

        monkeypatch.setattr(settings, "DEEP_MATCH_BAND_LOW", 0, raising=False)
        monkeypatch.setattr(settings, "DEEP_MATCH_BAND_HIGH", 100, raising=False)
        job = MagicMock()
        job.id = "j1"
        job.matched_by = "freeinference/big-model"
        reply = '{"score": 70, "reasoning": "ok", "matched_skills": [], "missing_skills": []}'
        with patch("app.services.matcher._build_match_prompt", return_value=[]), \
                patch("app.services.matcher.call_provider", return_value=reply) as call:
            matcher._deep_score(job, _pin("match_deep", "freeinference:big-model"), 70.0)
        assert ("freeinference", "big-model") not in _served(call.call_args_list)
