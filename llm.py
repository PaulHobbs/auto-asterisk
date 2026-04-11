"""Thin wrapper around the claude CLI with retries and structured output parsing."""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


# Model aliases for clarity
OPUS = "claude-opus-4-6"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"


@dataclass
class LLMResponse:
    text: str
    model: str


def call(
    prompt: str,
    *,
    model: str = SONNET,
    system: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    max_retries: int = 3,
) -> LLMResponse:
    """Call the claude CLI in print mode. Retries on transient errors."""
    cmd = ["claude", "--print", "--model", model]
    if system:
        cmd.extend(["--system-prompt", system])
    cmd.extend(["-p", prompt])

    last_error = None
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"claude CLI exited with code {result.returncode}: "
                    f"{result.stderr[:500]}"
                )
            return LLMResponse(text=result.stdout, model=model)
        except subprocess.TimeoutExpired as e:
            last_error = e
            print(f"  [llm] Timeout, retrying ({attempt + 1}/{max_retries})...")
        except RuntimeError as e:
            last_error = e
            wait = 2 ** attempt * 2
            print(f"  [llm] Error, retrying in {wait}s: {e}")
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
