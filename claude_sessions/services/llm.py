"""Shared Claude Code CLI helper for LLM calls."""

import json
import re
import subprocess
from typing import Optional


def call_claude(
    prompt: str,
    system_prompt: str = "Respond with valid JSON only. No markdown fences.",
    model: str = "sonnet",
    timeout: int = 120,
) -> Optional[str]:
    """Call Claude Code CLI. Returns the raw result text."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--system-prompt", system_prompt,
             "--output-format", "json", "--model", model],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return None
        if not result.stdout.strip():
            return None
        try:
            cli = json.loads(result.stdout)
            if isinstance(cli, dict):
                content = cli.get("result", "")
                if content:
                    return content
                if cli.get("is_error"):
                    return None
            return result.stdout
        except json.JSONDecodeError:
            return result.stdout
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def parse_json_response(text: str) -> Optional[dict]:
    """Parse a JSON response that might be wrapped in markdown fences."""
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip())
    cleaned = re.sub(r'\s*```$', '', cleaned.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    first = cleaned.find('{')
    last = cleaned.rfind('}')
    if first != -1 and last > first:
        try:
            return json.loads(cleaned[first:last + 1])
        except json.JSONDecodeError:
            pass
    return None
