"""Thin wrapper around Anthropic API with retries and structured output parsing."""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import anthropic


# Model aliases for clarity
OPUS = "claude-opus-4-6"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"

DEFAULT_MAX_TOKENS = 4096


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


def call(
    prompt: str,
    *,
    model: str = SONNET,
    system: Optional[str] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
    max_retries: int = 3,
) -> LLMResponse:
    """Call the Anthropic API. Retries on transient errors."""
    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var

    messages = [{"role": "user", "content": prompt}]
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
        temperature=temperature,
    )
    if system:
        kwargs["system"] = system

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(**kwargs)
            return LLMResponse(
                text=response.content[0].text,
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        except anthropic.RateLimitError as e:
            last_error = e
            wait = min(2 ** attempt * 5, 60)
            print(f"  [llm] Rate limited, waiting {wait}s...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                wait = 2 ** attempt
                print(f"  [llm] Server error {e.status_code}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
        except anthropic.APIConnectionError as e:
            last_error = e
            wait = 2 ** attempt
            print(f"  [llm] Connection error, retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"LLM call failed after {max_retries} retries: {last_error}")


def extract_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from LLM output.

    Handles:
      - Raw JSON
      - JSON inside ```json ... ``` fences
      - JSON embedded in prose
    """
    # Try fenced code block first
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find the first { or [ in the text and match from there.
    # This ensures [{"x":1}] is matched as an array, not as {"x":1}.
    first_brace = text.find("{")
    first_bracket = text.find("[")

    # Determine which comes first
    candidates = []
    if first_brace >= 0:
        candidates.append((first_brace, "{", "}"))
    if first_bracket >= 0:
        candidates.append((first_bracket, "[", "]"))
    candidates.sort(key=lambda x: x[0])

    for _, open_char, close_char in candidates:
        depth = 0
        start = None
        in_string = False
        escape = False
        for i, c in enumerate(text):
            if escape:
                escape = False
                continue
            if c == "\\" and in_string:
                escape = True
                continue
            if c == '"' and (start is not None):
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == open_char:
                if depth == 0:
                    start = i
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        start = None

    return None


def extract_float(text: str) -> Optional[float]:
    """Extract a score float from LLM output."""
    # Try JSON first
    data = extract_json(text)
    if data and isinstance(data, dict) and "score" in data:
        try:
            return float(data["score"])
        except (ValueError, TypeError):
            pass

    # Fall back to finding a number
    match = re.search(r"(?:score|Score)\s*[:=]\s*([\d.]+)", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Last resort: first standalone number
    match = re.search(r"\b(\d+\.?\d*)\b", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    return None
