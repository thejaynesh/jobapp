# app/llm/client.py
from openai import OpenAI


def chat_completion(
    messages: list[dict],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> str:
    from app.services import llm_log

    with llm_log.call("nim", model, messages,
                      temperature=temperature, max_tokens=max_tokens) as entry:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        message = response.choices[0].message
        entry.finish(getattr(message, "content", None) or "",
                     reasoning=getattr(message, "reasoning_content", None),
                     raw=response)
        # Reasoning models can spend the whole token budget thinking and leave
        # `content` empty (or None). The answer is often inside the reasoning,
        # and returning None crashes callers that .strip() the reply — so fall
        # back rather than handing back nothing. Same rule as matcher._reply_text.
        content = (getattr(message, "content", None) or "").strip()
        if content:
            return content
        return (getattr(message, "reasoning_content", None) or "").strip()
