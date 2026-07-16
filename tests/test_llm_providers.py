from unittest.mock import MagicMock, patch

import pytest

from app.llm.providers import (
    Provider,
    call_provider,
    configured_providers,
    generation_chat,
    matching_fallbacks,
)

_MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"},
]


def _settings(anthropic_key="", gemini_key=""):
    return patch.multiple(
        "app.llm.providers.settings",
        ANTHROPIC_API_KEY=anthropic_key,
        GEMINI_API_KEY=gemini_key,
        ANTHROPIC_MODEL="claude-opus-4-8",
        GEMINI_MODEL="gemini-2.5-flash",
        GEMINI_BASE_URL="https://gemini.example/v1/",
        create=True,
    )


class TestConfiguredProviders:
    def test_empty_when_no_keys(self):
        with _settings():
            assert configured_providers() == {}

    def test_includes_configured(self):
        with _settings(anthropic_key="ak", gemini_key="gk"):
            providers = configured_providers()
        assert providers["anthropic"].model == "claude-opus-4-8"
        assert providers["gemini"].base_url == "https://gemini.example/v1/"


class TestGenerationChat:
    def test_uses_primary_when_no_quality_providers(self):
        with _settings():
            with patch("app.llm.providers.call_provider", return_value="out") as mock_call:
                result = generation_chat(_MESSAGES, "nk", "http://nim", "llama")
        assert result == "out"
        provider = mock_call.call_args[0][0]
        assert provider.name == "primary"
        assert provider.api_key == "nk"

    def test_prefers_anthropic_then_gemini_then_primary(self):
        with _settings(anthropic_key="ak", gemini_key="gk"):
            with patch("app.llm.providers.call_provider", return_value="out") as mock_call:
                generation_chat(_MESSAGES, "nk", "http://nim", "llama")
        assert mock_call.call_args[0][0].name == "anthropic"

    def test_falls_back_down_the_chain(self):
        with _settings(anthropic_key="ak", gemini_key="gk"):
            with patch(
                "app.llm.providers.call_provider",
                side_effect=[Exception("anthropic down"), Exception("gemini down"), "primary out"],
            ) as mock_call:
                result = generation_chat(_MESSAGES, "nk", "http://nim", "llama")
        assert result == "primary out"
        names = [c[0][0].name for c in mock_call.call_args_list]
        assert names == ["anthropic", "gemini", "primary"]

    def test_raises_when_all_fail(self):
        with _settings(anthropic_key="ak"):
            with patch("app.llm.providers.call_provider", side_effect=Exception("down")):
                with pytest.raises(Exception, match="down"):
                    generation_chat(_MESSAGES, "nk", "http://nim", "llama")


class TestMatchingFallbacks:
    def test_order_is_gemini_then_anthropic(self):
        with _settings(anthropic_key="ak", gemini_key="gk"):
            names = [p.name for p in matching_fallbacks()]
        assert names == ["gemini", "anthropic"]

    def test_empty_without_keys(self):
        with _settings():
            assert matching_fallbacks() == []


class TestCallProvider:
    def test_anthropic_splits_system_and_skips_sampling_params(self):
        provider = Provider(name="anthropic", api_key="ak", model="claude-opus-4-8")
        mock_client = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "hi there"
        mock_client.messages.create.return_value = MagicMock(
            content=[block], stop_reason="end_turn"
        )
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = call_provider(provider, _MESSAGES, temperature=0.6, max_tokens=700)
        assert result == "hi there"
        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["system"] == "You are helpful."
        assert kwargs["messages"] == [{"role": "user", "content": "Hello"}]
        assert kwargs["max_tokens"] == 700
        assert "temperature" not in kwargs  # Claude models reject sampling params

    def test_anthropic_refusal_raises(self):
        provider = Provider(name="anthropic", api_key="ak", model="claude-opus-4-8")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[], stop_reason="refusal"
        )
        with patch("anthropic.Anthropic", return_value=mock_client):
            with pytest.raises(RuntimeError, match="refused"):
                call_provider(provider, _MESSAGES)

    def test_openai_compatible_passes_temperature(self):
        provider = Provider(name="gemini", api_key="gk", model="gemini-2.5-flash",
                            base_url="https://gemini.example/v1/")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="gem out"))]
        )
        with patch("openai.OpenAI", return_value=mock_client):
            result = call_provider(provider, _MESSAGES, temperature=0.3, max_tokens=256)
        assert result == "gem out"
        _, kwargs = mock_client.chat.completions.create.call_args
        assert kwargs["temperature"] == 0.3
        assert kwargs["max_tokens"] == 256


class TestMatchingModelSplit:
    def test_anthropic_matching_uses_cheap_model(self):
        with _settings(anthropic_key="ak", gemini_key="gk"):
            with patch("app.llm.providers.settings.ANTHROPIC_MATCH_MODEL",
                       "claude-haiku-4-5", create=True):
                chain = matching_fallbacks()
        by_name = {p.name: p for p in chain}
        assert by_name["anthropic"].model == "claude-haiku-4-5"
        assert by_name["gemini"].model == "gemini-2.5-flash"

    def test_generation_still_uses_generation_model(self):
        with _settings(anthropic_key="ak"):
            with patch("app.llm.providers.call_provider", return_value="out") as mock_call:
                generation_chat(_MESSAGES, "nk", "http://nim", "llama")
        assert mock_call.call_args[0][0].model == "claude-opus-4-8"
