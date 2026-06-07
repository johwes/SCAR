"""OpenAI-compatible LLM client.

Reads LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL from the environment so the
same code works against any compliant endpoint (LiteLLM proxy, OpenAI,
OpenRouter, local vLLM, etc.).
"""

import os
from openai import OpenAI

_client: OpenAI | None = None

_prompt_tokens: int = 0
_completion_tokens: int = 0


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
    global _prompt_tokens, _completion_tokens
    model = os.environ["LLM_MODEL"]
    response = _get_client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    if response.usage:
        _prompt_tokens += response.usage.prompt_tokens
        _completion_tokens += response.usage.completion_tokens
    return response.choices[0].message.content or ""


def get_usage() -> dict:
    """Return accumulated token counts across all chat() calls this process."""
    return {
        "prompt_tokens": _prompt_tokens,
        "completion_tokens": _completion_tokens,
        "total_tokens": _prompt_tokens + _completion_tokens,
    }
