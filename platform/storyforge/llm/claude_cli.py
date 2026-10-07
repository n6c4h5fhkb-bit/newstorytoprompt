"""Stateless Claude Code CLI transport (`claude -p`). Prompts live exclusively in skills/."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from storyforge.config import SflError
from storyforge.llm.codex_cli import structured_schema
from storyforge.packets import Packet
from storyforge.store import serialize

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def executable(profile: dict) -> str:
    selected = os.environ.get("SFL_CLAUDE_COMMAND") or profile.get("command") or shutil.which("claude")
    if not selected:
        raise SflError("Claude Code CLI not found; install it or set SFL_CLAUDE_COMMAND")
    return str(shutil.which(selected) or selected)


def arguments(profile: dict, folder: Path, schema: dict) -> list[str]:
    effort = profile.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise SflError("Invalid Claude effort")
    args = [executable(profile), "-p", "--output-format", "json", "--model", profile["model"],
            "--json-schema", json.dumps(structured_schema(schema), ensure_ascii=False),
            "--system-prompt-file", str(folder / "instructions.md"),
            # A text-only call: no tools, skills, MCP servers, settings or saved session.
            "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence"]
    if effort:
        args += ["--effort", effort]
    return args


def usage_of(raw: dict) -> dict:
    usage = raw.get("usage") or {}
    prompt = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"input_tokens": prompt, "cached_input_tokens": usage.get("cache_read_input_tokens"),
            "output_tokens": usage.get("output_tokens")}


def run(profile: dict, packet: Packet, schema: dict, *, stage: str | None = None, target: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="sfl-claude-") as temporary:
        folder = Path(temporary)
        (folder / "instructions.md").write_text(packet.instruction, encoding="utf-8")
        prompt = serialize({"data": packet.data, "repair": packet.repairs, "output_schema": schema})
        process = subprocess.Popen(arguments(profile, folder, schema), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=folder, env=dict(os.environ), text=True, encoding="utf-8", errors="replace")
        try:
            stdout, stderr = process.communicate(prompt, timeout=profile.get("timeout_seconds", 600))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {"text": None, "usage": {}, "tool_items": [], "error": "Claude CLI timed out; call billing/usage may be incomplete",
                    "exit_code": process.returncode, "retryable": False}
        try:
            raw = json.loads(stdout)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            reason = (stderr or stdout or "no output").strip()[:300]
            return {"text": None, "usage": {}, "tool_items": [], "error": f"Claude CLI exited {process.returncode}: {reason}",
                    "exit_code": process.returncode, "retryable": False}
        status = raw.get("api_error_status")
        error, text = None, None
        if raw.get("is_error") or raw.get("subtype") != "success":
            error = "Claude CLI call failed: " + str(raw.get("result") or raw.get("subtype") or "unknown")[:300]
        elif raw.get("structured_output") is not None:
            text = serialize(raw["structured_output"])
        elif raw.get("result"):
            text = raw["result"]
        else:
            error = "Claude CLI returned no structured output"
        return {"text": text, "usage": usage_of(raw), "cost": raw.get("total_cost_usd"), "tool_items": [], "complete": error is None,
                "error": error, "exit_code": process.returncode,
                "retryable": bool(error) and isinstance(status, int) and (status in (408, 409, 429) or 500 <= status < 600)}


def doctor(profile: dict) -> dict:
    binary = executable(profile)
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    return {"provider": "claude_cli", "executable": binary, "version": version.stdout.strip(),
            "model": profile["model"], "effort": profile.get("effort"), "logged_in": version.returncode == 0}
