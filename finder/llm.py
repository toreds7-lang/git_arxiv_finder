"""One-shot Claude calls (no tools) for the Library: topic keywords and search ideas.

Uses the same Claude Code CLI as the search runs, so no API key is needed. Thinking is
turned off (MAX_THINKING_TOKENS=0): with it on, Haiku spent ~4.5k thinking tokens on a
3-topic keyword job (2.6 cents, 43 s); without it the same job costs ~0.3 cents and takes 4 s.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess

from .config import WORK_ROOT, claude_bin

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LLMError(RuntimeError):
    pass


def _parse_json(text: str) -> dict:
    """The reply's JSON object, tolerating a ```json fence or a stray sentence around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


def claude_json(prompt: str, system: str, model: str = "haiku", timeout: int = 120,
                max_usd: float = 0.10) -> tuple[dict, float | None]:
    """Run `claude -p` once with no tools and return (parsed JSON reply, cost in USD)."""
    workdir = WORK_ROOT / f"llm-{secrets.token_hex(3)}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        sys_file = workdir / "system_prompt.txt"
        sys_file.write_text(system, encoding="utf-8")
        cmd = [claude_bin(), "-p", "--model", model, "--output-format", "json",
               "--tools", "", "--strict-mcp-config", "--setting-sources", "project",
               "--disable-slash-commands", "--no-session-persistence",
               "--max-budget-usd", str(max_usd), "--system-prompt-file", str(sys_file)]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["MAX_THINKING_TOKENS"] = "0"
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, cwd=workdir, env=env,
                               creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LLMError(f"Claude Code did not answer: {type(e).__name__}") from e
        try:
            out = json.loads(p.stdout)
        except ValueError:
            raise LLMError(f"Claude Code exited with code {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
        cost = out.get("total_cost_usd")
        if out.get("is_error") or out.get("subtype") != "success":
            raise LLMError(f"Claude stopped: {out.get('subtype')} {str(out.get('result') or '')[:200]}")
        try:
            return _parse_json(str(out.get("result") or "")), cost
        except ValueError:
            raise LLMError("Claude's reply was not valid JSON")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
