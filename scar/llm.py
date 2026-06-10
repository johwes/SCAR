"""OpenAI-compatible LLM client.

Reads LLM_BASE_URL, LLM_API_KEY, and model env vars from the environment so
the same code works against any compliant endpoint (LiteLLM proxy, OpenAI,
OpenRouter, local vLLM, etc.).

Two model roles are supported, each with its own env var:
  LLM_PATCH_MODEL  — used for generation tasks (context, patch synthesis, scan)
  LLM_REVIEW_MODEL — used for review tasks (triage rounds, arbiter)

Both fall back to LLM_MODEL if the role-specific var is not set, so a single
LLM_MODEL is sufficient for deployments that use one model for everything.
"""

import os
import threading
import time
from pathlib import Path

from openai import OpenAI

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 3.0  # seconds; doubled each attempt

_client: OpenAI | None = None
_prompt_tokens: int = 0
_completion_tokens: int = 0
_lock = threading.Lock()


def _get_client() -> OpenAI:
    global _client
    with _lock:
        if _client is None:
            _client = OpenAI(
                base_url=os.environ["LLM_BASE_URL"],
                api_key=os.environ["LLM_API_KEY"],
            )
        return _client


def patch_model() -> str:
    """Model for generation tasks: context briefing, patch synthesis, LLM scan."""
    return os.environ.get("LLM_PATCH_MODEL") or os.environ["LLM_MODEL"]


def review_model() -> str:
    """Model for review tasks: triage rounds and arbiter verdict."""
    return os.environ.get("LLM_REVIEW_MODEL") or os.environ["LLM_MODEL"]


def chat(messages: list[dict], *, model: str, temperature: float = 0.2) -> str:
    """Send a chat completion request and return the response text.

    Retries up to _MAX_RETRIES times with exponential backoff on transient
    network errors (server disconnect, connection reset, timeout).
    """
    global _prompt_tokens, _completion_tokens
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = _get_client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            if response.usage:
                with _lock:
                    _prompt_tokens += response.usage.prompt_tokens
                    _completion_tokens += response.usage.completion_tokens
            return response.choices[0].message.content or ""
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"[llm] transient error ({exc.__class__.__name__}), "
                    f"retrying in {delay:.0f}s (attempt {attempt + 1}/{_MAX_RETRIES})",
                    flush=True,
                )
                time.sleep(delay)
    raise last_exc


def get_usage() -> dict:
    """Return accumulated token counts across all chat() calls this process."""
    with _lock:
        return {
            "prompt_tokens": _prompt_tokens,
            "completion_tokens": _completion_tokens,
            "total_tokens": _prompt_tokens + _completion_tokens,
        }


def write_trace(
    path: Path,
    *,
    title: str,
    messages: list[dict],
    response: str,
    model: str,
    temperature: float,
    extra_sections: dict[str, str] | None = None,
) -> None:
    """Write a human-readable LLM interaction trace to path for student inspection.

    Each trace file shows the exact prompt (system + user) and raw response for
    one LLM call, plus any extra sections (grep results, IKOS witness trace, etc.).
    Files are plain Markdown so students can read them with cat or any editor.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"**Model**: `{model}`  **Temperature**: {temperature}\n\n")
        for msg in messages:
            f.write(f"---\n\n## {msg['role'].capitalize()}\n\n{msg['content']}\n\n")
        f.write(f"---\n\n## Response\n\n{response}\n")
        if extra_sections:
            for heading, content in extra_sections.items():
                if content:
                    f.write(f"\n---\n\n## {heading}\n\n{content}\n")
