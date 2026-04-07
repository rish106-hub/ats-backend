"""Gemini helpers for structured prompt execution and token tracking."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types


# Module-level client — reconfigured per-request via configure_genai()
_client: genai.Client | None = None


def configure_genai(api_key: str | None = None) -> str:
    """Configure the Gemini client and return the resolved API key."""
    global _client
    resolved_key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not resolved_key:
        raise ValueError("Missing GOOGLE_API_KEY or GEMINI_API_KEY.")
    _client = genai.Client(api_key=resolved_key)
    return resolved_key


def _get_client() -> genai.Client:
    if _client is None:
        # Auto-init from env if configure_genai() hasn't been called yet
        configure_genai()
    return _client  # type: ignore[return-value]


def render_template(template: str, replacements: dict[str, Any]) -> str:
    rendered = template
    for key, value in replacements.items():
        if isinstance(value, (dict, list)):
            serialized = json.dumps(value, indent=2, ensure_ascii=True)
        else:
            serialized = str(value)
        rendered = rendered.replace(f"{{{{{key}}}}}", serialized)
    return rendered


def extract_json_from_text(text: str) -> Any:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Model returned an empty response.")

    fenced_match = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, flags=re.DOTALL)
    if fenced_match:
        cleaned = fenced_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = min(
        [index for index in [cleaned.find("{"), cleaned.find("[")] if index != -1],
        default=-1,
    )
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Unable to locate JSON in the model response.")

    snippet = cleaned[start : end + 1]
    return json.loads(snippet)


def _usage_to_dict(usage_metadata: Any, elapsed: float) -> dict[str, Any]:
    if not usage_metadata:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "latency_seconds": round(elapsed, 2)}

    input_tokens = int(getattr(usage_metadata, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage_metadata, "candidates_token_count", 0) or 0)
    total_tokens = int(getattr(usage_metadata, "total_token_count", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "latency_seconds": round(elapsed, 2),
    }


def generate_response(
    model_name: str,
    system_instruction: str,
    user_prompt: str,
    temperature: float = 0.0,
) -> tuple[str, dict[str, Any]]:
    started_at = time.perf_counter()
    client = _get_client()

    response = client.models.generate_content(
        model=model_name,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            # Disable thinking mode — Gemini 2.5 models run extended internal
            # chain-of-thought by default, which can take 60–180s before producing
            # any output. thinking_budget=0 switches to direct generation (5–15s).
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            http_options=types.HttpOptions(timeout=90_000),
        ),
    )

    elapsed = time.perf_counter() - started_at
    raw_text = (response.text or "").strip()
    usage = _usage_to_dict(response.usage_metadata, elapsed)
    return raw_text, usage


class GeminiUnavailableError(RuntimeError):
    """Raised when Gemini returns a transient 503/429 after all retries are exhausted."""
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def run_structured_call(
    model_name: str,
    system_instruction: str,
    template: str,
    replacements: dict[str, Any],
    temperature: float = 0.0,
) -> tuple[Any, str, dict[str, Any], str]:
    final_prompt = render_template(template, replacements)
    try:
        raw_text, usage = generate_response(model_name, system_instruction, final_prompt, temperature)
    except genai_errors.ServerError as exc:
        # 503 UNAVAILABLE — model is temporarily overloaded.
        # Tenacity has already retried; we re-raise as our own error so the
        # router can return a proper HTTP 503 instead of crashing with 500.
        raise GeminiUnavailableError(503, str(exc)) from exc
    except genai_errors.ClientError as exc:
        # 429 RESOURCE_EXHAUSTED — rate limit hit.
        raise GeminiUnavailableError(429, str(exc)) from exc
    print(f"Gemini call to {model_name} | {usage.get('total_tokens', 0)} tokens | {usage.get('latency_seconds', 0)}s")
    try:
        parsed_json = extract_json_from_text(raw_text)
    except Exception as exc:
        print(f"FAILED TO PARSE JSON: {raw_text[:200]}...")
        raise exc
    return parsed_json, raw_text, usage, final_prompt


def run_raw_call(
    model_name: str,
    system_instruction: str,
    user_prompt: str,
    temperature: float = 0.0,
) -> tuple[str, dict[str, Any], Any | None]:
    raw_text, usage = generate_response(model_name, system_instruction, user_prompt, temperature)
    parsed_json = None
    try:
        parsed_json = extract_json_from_text(raw_text)
    except Exception:
        parsed_json = None
    return raw_text, usage, parsed_json
