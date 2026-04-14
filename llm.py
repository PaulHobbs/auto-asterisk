"""Thin wrapper around the claude/gemini CLI with retries and structured output parsing."""

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .config import OPUS, SONNET, HAIKU, PROVIDER
from .quota import (
    QuotaPolicy, QuotaState, PersistentQuotaError,
    detect_quota_signal, should_hibernate, backoff_for_quota,
)

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    text: str
    model: str


def _build_cmd(
    prompt: str,
    model: str,
    system: Optional[str],
    provider: str,
) -> list[str]:
    """Build the CLI command for the given provider."""
    if provider == "gemini":
        # Gemini CLI: gemini --yolo -m MODEL -p PROMPT
        # No --system-prompt flag; prepend system instructions to the prompt.
        full_prompt = prompt
        if system:
            full_prompt = f"[System instructions — follow these exactly]\n{system}\n[End system instructions]\n\n{prompt}"
        cmd = ["gemini", "--yolo", "-m", model, "-p", full_prompt]
        return cmd
    else:
        # Claude CLI: claude --print --model MODEL -p PROMPT
        cmd = ["claude", "--print", "--model", model]
        if system:
            cmd.extend(["--system-prompt", system])
        cmd.extend(["-p", prompt])
        return cmd


def _stream_subprocess(
    cmd: list[str],
    *,
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> tuple[int, str, str]:
    """Run *cmd* capturing stdout/stderr via threads to avoid pipe deadlocks.

    Returns ``(returncode, stdout, stderr)``.

    Raises ``subprocess.TimeoutExpired`` (with accumulated output attached) if the
    subprocess runs past *timeout* seconds without exiting.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
    )

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def _reader(stream, parts: list[str]) -> None:
        for line in stream:
            parts.append(line)

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_parts), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_parts), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)

    if timed_out:
        exc = subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
        raise exc

    return proc.returncode, stdout, stderr


def call(
    prompt: str,
    *,
    model: str = SONNET,
    system: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    max_retries: int = 3,
    provider: Optional[str] = None,
    quota_policy: Optional[QuotaPolicy] = None,
    on_quota_error: Optional[Callable[[str, str], None]] = None,
) -> LLMResponse:
    """Call the LLM CLI in non-interactive mode. Retries on transient errors."""
    prov = provider or PROVIDER
    cmd = _build_cmd(prompt, model, system, prov)
    cli_name = "gemini" if prov == "gemini" else "claude"

    # When quota policy is set, ensure we have enough retries to reach the hibernate threshold.
    if quota_policy is not None:
        max_retries = max(max_retries, quota_policy.consecutive_errors_threshold + 2)

    state = QuotaState() if quota_policy is not None else None

    last_error = None
    for attempt in range(max_retries):
        try:
            returncode, stdout, stderr = _stream_subprocess(
                cmd, timeout=300,
            )

            if returncode != 0:
                if quota_policy is not None:
                    signal = detect_quota_signal(stderr)
                    if signal.is_quota_error:
                        if on_quota_error is not None:
                            on_quota_error(stderr, model)
                        state.consecutive_quota_errors += 1
                        state.quota_error_timestamps.append(datetime.utcnow())
                        if should_hibernate(state, quota_policy):
                            raise PersistentQuotaError(
                                f"Persistent quota errors for model {model} after "
                                f"{state.consecutive_quota_errors} consecutive failures",
                                retry_after=signal.retry_after_seconds,
                            )
                        wait = backoff_for_quota(
                            state.consecutive_quota_errors,
                            quota_policy,
                            signal.retry_after_seconds,
                        )
                        log.warning(
                            f"[llm] Quota error, backing off {wait}s "
                            f"(consecutive={state.consecutive_quota_errors})"
                        )
                        time.sleep(wait)
                        last_error = RuntimeError(
                            f"{cli_name} CLI exited with code {returncode}: "
                            f"{stderr[:500]}"
                        )
                        continue
                    else:
                        # Non-quota error — reset consecutive quota counter
                        state.consecutive_quota_errors = 0
                raise RuntimeError(
                    f"{cli_name} CLI exited with code {returncode}: "
                    f"{stderr[:500]}"
                )
            return LLMResponse(text=stdout, model=model)
        except subprocess.TimeoutExpired as e:
            last_error = e
            if quota_policy is not None:
                # Long timeouts are common during quota issues; check what we have
                signal = detect_quota_signal(getattr(e, "stderr", "") or "", "")
                if signal.is_quota_error:
                    if on_quota_error is not None:
                        on_quota_error(str(e), model)
                    state.consecutive_quota_errors += 1
                    state.quota_error_timestamps.append(datetime.utcnow())
                    if should_hibernate(state, quota_policy):
                        raise PersistentQuotaError(
                            f"Persistent quota errors (timeout) for model {model}",
                        )
            log.warning(f"[llm] Timeout, retrying ({attempt + 1}/{max_retries})...")
        except PersistentQuotaError:
            raise
        except RuntimeError as e:
            last_error = e
            wait = 2 ** attempt * 2
            log.warning(f"[llm] Error, retrying in {wait}s: {e}")
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

    return None
