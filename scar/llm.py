"""OpenAI-compatible LLM client.

Reads LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL from the environment so the
same code works against any compliant endpoint (LiteLLM proxy, OpenAI,
OpenRouter, local vLLM, etc.).
"""

import os
from openai import OpenAI

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ["LLM_API_KEY"],
        )
    return _client


def chat(messages: list[dict], *, temperature: float = 0.2) -> str:
    """Send a chat completion request and return the response text."""
    model = os.environ["LLM_MODEL"]
    response = _get_client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""
