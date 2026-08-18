"""
Choosing which provider scores jobs first.

NVIDIA NIM was the primary by construction: `matcher` called it directly and
everything else was failover. Making that a setting is mostly plumbing — the
part worth testing is what must NOT change when you flip it. The concurrency
gate has to stay on the path (FreeInference takes one request at a time, and
document generation is on the same endpoint), NIM has to remain reachable
rather than disappear, and a typo has to cost a log line rather than every
score in the queue.
"""

import json
from unittest.mock import patch

import pytest

from app.config import settings
from app.llm import providers
from app.services import matcher

REPLY = json.dumps({
    "score": 82, "reasoning": "fits", "matched_skills": ["Python"],
    "missing_skills": [], "seniority_fit": True,
})


@pytest.fixture
def free(monkeypatch):
    monkeypatch.setattr(settings, "FREEINFERENCE_API_KEY", "free-key")
    monkeypatch.setattr(settings, "FREEINFERENCE_MODEL", "glm-5.1")
    monkeypatch.setattr(settings, "FREEINFERENCE_MATCH_MODEL", "glm-5-turbo")
    monkeypatch.setattr(settings, "FREEINFERENCE_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "NVIDIA_NIM_API_KEY", "nim-key")
    monkeypatch.setattr(settings, "NVIDIA_NIM_MODEL", "z-ai/glm-5.2")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")


class TestChoosingIt:
    def test_the_default_is_still_nim(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "nim")
        assert providers.primary_matching_provider() is None

    def test_an_unset_value_is_still_nim(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "")
        assert providers.primary_matching_provider() is None

    def test_naming_freeinference_selects_it(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")
        primary = providers.primary_matching_provider()
        assert primary.name == "freeinference"
        # The cheap high-volume model, not the generation one.
        assert primary.model == "glm-5-turbo"

    def test_the_concurrency_limit_survives_the_model_swap(self, free, monkeypatch):
        # Losing this would silently ungate every matching call — which is most
        # of them — against an endpoint that takes one request at a time.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")
        assert providers.primary_matching_provider().max_concurrency == 1

    def test_a_typo_falls_back_to_nim(self, free, monkeypatch):
        # A misspelt setting should cost a log line, not every score.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinferenc")
        assert providers.primary_matching_provider() is None

    def test_an_unconfigured_provider_falls_back_to_nim(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "anthropic")
        assert providers.primary_matching_provider() is None


class TestTheChainBehindIt:
    def test_nim_moves_to_the_end_rather_than_vanishing(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")
        chain = [p.name for p in providers.matching_fallbacks()]
        assert chain[-1] == "nim"

    def test_the_primary_is_not_also_a_fallback(self, free, monkeypatch):
        # Retrying the endpoint that just failed, immediately, is a call spent
        # to watch it fail again.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")
        assert "freeinference" not in [p.name for p in providers.matching_fallbacks()]

    def test_nim_as_a_fallback_is_not_charged_to_the_paid_budget(self, free, monkeypatch):
        # It never counted when it was the primary; moving it down the chain
        # should not quietly start spending a cap meant for providers that bill.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")
        nim = [p for p in providers.matching_fallbacks() if p.name == "nim"][0]
        assert nim.paid is False

    def test_the_default_chain_is_unchanged(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "nim")
        chain = [p.name for p in providers.matching_fallbacks()]
        assert chain == ["freeinference"]


class TestScoringThroughIt:
    def _score(self, job=None, **kwargs):
        job = job or type("J", (), {"id": None, "title": "Backend Engineer",
                                    "company": "Acme", "location": "Remote",
                                    "is_remote": True, "experience_level": "mid",
                                    "description": "Python and Go."})()
        return matcher.llm_score_job(job, {}, "nim-key", "nim-url", "nim-model", **kwargs)

    def test_it_goes_through_the_gated_path(self, free, monkeypatch):
        # `call_provider` is the path with the concurrency gate on it;
        # `chat_completion` is not. Matching that bypassed the gate would
        # collide with document generation on the same endpoint.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")

        # Patched on `matcher`, which imports it by name at module level —
        # patching it on `providers` would leave matcher's own reference intact
        # and the test would pass against the real network.
        with patch("app.services.matcher.call_provider", return_value=REPLY) as call, \
             patch("app.services.matcher.chat_completion") as direct:
            result = self._score()

        assert result["score"] == 82
        assert result["scored_by"] == "freeinference/glm-5-turbo"
        call.assert_called_once()
        direct.assert_not_called()

    def test_nim_is_still_called_directly_by_default(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "nim")

        with patch("app.services.matcher.chat_completion", return_value=REPLY) as direct:
            result = self._score()

        assert result["scored_by"] == "nim/nim-model"
        direct.assert_called_once()

    def test_a_failing_primary_falls_through_to_the_chain(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")

        calls = []

        def _call(provider, *a, **k):
            calls.append(provider.name)
            if provider.name == "freeinference":
                raise RuntimeError("daily credit spent")
            return REPLY

        with patch("app.services.matcher.call_provider", side_effect=_call):
            result = self._score(budget={"paid_calls": 0, "deep_calls": 0})

        assert calls == ["freeinference", "nim"]
        assert result["scored_by"] == "nim/z-ai/glm-5.2"

    def test_everything_failing_leaves_the_job_to_retry(self, free, monkeypatch):
        # Not a score of zero: that would filter the job out and blame the
        # score for a provider outage.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")

        with patch("app.services.matcher.call_provider",
                   side_effect=RuntimeError("down")):
            with pytest.raises(matcher.LLMUnavailableError):
                self._score(budget={"paid_calls": 0, "deep_calls": 0})


class TestPacing:
    def test_nim_pacing_applies_when_nim_is_primary(self, free, monkeypatch):
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "nim")
        monkeypatch.setattr(settings, "NVIDIA_NIM_RPM", 40)
        assert matcher.match_pace_seconds() == pytest.approx(1.5)

    def test_it_does_not_apply_to_a_provider_that_is_not_nim(self, free, monkeypatch):
        # Sleeping 1.5s between jobs to respect a limit belonging to a provider
        # we are not calling is throttling the pass for nothing.
        monkeypatch.setattr(settings, "MATCH_PRIMARY", "freeinference")
        assert matcher.match_pace_seconds() == 0.0
