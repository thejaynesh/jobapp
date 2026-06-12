from unittest.mock import MagicMock, patch
from app.llm.client import chat_completion


def test_chat_completion_calls_nim(monkeypatch):
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "test response"
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    with patch("app.llm.client.OpenAI", return_value=mock_client):
        result = chat_completion(
            messages=[{"role": "user", "content": "hello"}],
            api_key="testkey",
            base_url="https://api.nvidia.com/v1",
            model="meta/llama-3.1-70b-instruct",
        )
    assert result == "test response"
    mock_client.chat.completions.create.assert_called_once()


def test_chat_completion_passes_model(monkeypatch):
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "ok"
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    with patch("app.llm.client.OpenAI", return_value=mock_client):
        chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            api_key="k",
            base_url="https://api.nvidia.com/v1",
            model="custom/model",
        )
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "custom/model"
