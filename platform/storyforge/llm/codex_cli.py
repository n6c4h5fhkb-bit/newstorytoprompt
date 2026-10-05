"""Stateless Codex CLI transport. Prompts live exclusively in skills/."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sysconfig
import tempfile

from storyforge import ROOT
from storyforge.config import SflError
from storyforge.packets import Packet
from storyforge.store import serialize

DISABLED_FEATURES = ("shell_tool", "unified_exec", "code_mode_host", "multi_agent", "apps", "plugins",
                     "hooks", "memories", "skill_search", "view_image", "image_generation",
                     "browser_use", "browser_use_external", "computer_use", "daemon_auto_start",
                     "goals", "sleep_tool", "workspace_dependencies", "in_app_browser", "in_app_local_automation", "remote_plugin")
ALLOWED_ITEMS = {"agent_message", "reasoning"}


def executable(profile: dict) -> str:
    selected = os.environ.get("SFL_CODEX_COMMAND") or profile.get("command")
    # Match the terminal's PATH order, including npm's Windows launcher.
    selected = selected or shutil.which("codex") or shutil.which("codex.exe")
    if not selected:
        raise SflError("Codex CLI not found; install it or set SFL_CODEX_COMMAND to the native executable")
    resolved = shutil.which(selected) or selected
    if os.name == "nt" and Path(resolved).suffix.lower() in (".cmd", ".bat", ".ps1"):
        resolved = npm_executable(Path(resolved))
    return str(resolved)


def npm_executable(launcher: Path) -> Path:
    """Resolve the official npm package without executing a shell launcher."""
    machine = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or sysconfig.get_platform()
    arch = "arm64" if "arm64" in machine.lower() or "aarch64" in machine.lower() else "x64"
    triple = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
    package = launcher.parent / "node_modules" / "@openai" / "codex"
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = {}
    if metadata.get("name") == "@openai/codex":
        roots = [package / "node_modules" / "@openai" / f"codex-win32-{arch}",
                 package.parent / f"codex-win32-{arch}", package]
        for root in roots:
            binary = root / "vendor" / triple / "bin" / "codex.exe"
            if binary.is_file():
                return binary
    raise SflError("Cannot resolve this Windows CLI launcher; set SFL_CODEX_COMMAND to its native codex.exe")


def environment() -> dict[str, str]:
    # Windows restricted processes can have an OS home different from USERPROFILE.
    # Pin the existing CLI home, rather than letting native home discovery lose auth.
    values = dict(os.environ)
    if not values.get("CODEX_HOME"):
        values["CODEX_HOME"] = str(Path.home() / ".codex")
    return values


def arguments(profile: dict, folder: Path) -> list[str]:
    effort = profile.get("effort", "high")
    if effort not in ("low", "medium", "high", "xhigh", "max", "ultra"):
        raise SflError("Invalid Codex reasoning effort")
    args = [executable(profile), "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
            "--model", profile["model"], "-C", str(folder),
            "--output-schema", str(folder / "schema.json"),
            "--output-last-message", str(folder / "response.json"),
            "-c", "model_reasoning_effort=" + json.dumps(effort),
            "-c", "model_instructions_file=" + json.dumps(str(folder / "instructions.md")),
            "-c", "project_doc_max_bytes=0", "-c", 'history.persistence="none"',
            "-c", "log_dir=" + json.dumps(str(folder / "logs")),
            "-c", "sqlite_home=" + json.dumps(str(folder / "state")),
            # A named provider retains Codex's OpenAI login routing but lets us
            # disable native retries. Client owns the one shared attempt budget.
            "-c", 'model_provider="storyforge"',
            "-c", 'model_providers.storyforge.name="StoryForge Codex login"',
            "-c", 'model_providers.storyforge.wire_api="responses"',
            "-c", 'model_providers.storyforge.requires_openai_auth=true',
            "-c", 'model_providers.storyforge.request_max_retries=0',
            "-c", 'model_providers.storyforge.stream_max_retries=0',
            "-c", 'model_providers.storyforge.supports_websockets=false',
            "-c", 'web_search="disabled"', "-c", "mcp_servers={}",
            "-c", 'approval_policy="never"', "--enable", "skip_host_skill_discovery"]
    for feature in DISABLED_FEATURES:
        args += ["--disable", feature]
    return args + ["-"]


def events(stdout: str) -> dict:
    usage, errors, tool_items, complete, diagnostics, output_started = {}, [], [], False, 0, False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            complete = True
        elif kind in ("error", "turn.failed"):
            detail = event.get("error")
            errors.append(event.get("message") or (detail.get("message") if isinstance(detail, dict) else detail) or "Codex call failed")
            if kind=="turn.failed":complete=False
        elif kind in ("item.started", "item.completed", "item.updated"):
            item = event.get("item", {})
            if item.get("type")=="error":
                # A diagnostic item has no tool action or external result. The
                # completed-turn event still determines whether the request worked.
                diagnostics += 1
            elif item.get("type") not in ALLOWED_ITEMS:
                tool_items.append(item.get("type", "unknown"))
            else:
                output_started = True
    return {"usage": usage, "errors": errors, "tool_items": tool_items, "complete": complete,
            "diagnostic_items": diagnostics, "output_started": output_started}


def retryable_failure(summary: dict) -> bool:
    """Retry only transport failures before any model output/tool action.

    The native JSONL protocol puts the HTTP status in a message, sometimes as
    embedded JSON. Unknown errors, auth/config errors and partial turns pause.
    """
    if summary["complete"] or summary["tool_items"] or summary["output_started"]:
        return False
    message = str(summary["errors"][-1]) if summary["errors"] else ""
    try:
        detail = json.loads(message)
    except ValueError:
        detail = None
    status = detail.get("status") if isinstance(detail, dict) else None
    if status is None:
        match = re.match(r"unexpected status (\d{3})\b", message, flags=re.I)
        status = int(match[1]) if match else None
    if isinstance(status, int):
        return status in (408, 409, 429) or 500 <= status < 600
    return message.lower().startswith(("error sending request", "stream disconnected before completion"))


def structured_schema(value):
    """Keep array uniqueness in the prompt and local validator, outside the
    restricted response-format schema accepted by the Codex backend.
    """
    if isinstance(value,dict):
        return {key:structured_schema(item) for key,item in value.items()
                if not (key=="uniqueItems" and value.get("type")=="array")}
    if isinstance(value,list):return [structured_schema(item) for item in value]
    return value


def run(profile: dict, packet: Packet, schema: dict, *, stage: str | None = None, target: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="sfl-call-") as temporary:
        folder = Path(temporary)
        (folder / "schema.json").write_text(serialize(structured_schema(schema)), encoding="utf-8")
        (folder / "instructions.md").write_text(packet.instruction, encoding="utf-8")
        prompt = serialize({"data": packet.data, "repair": packet.repairs, "output_schema": schema})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(arguments(profile, folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=folder, env=environment(), text=True, encoding="utf-8", errors="replace", creationflags=flags)
        try:
            stdout, _stderr = process.communicate(prompt, timeout=profile.get("timeout_seconds", 600))
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _stderr = process.communicate()
            return {"text": None, **events(stdout), "error": "Codex CLI timed out; call billing/usage may be incomplete", "exit_code": process.returncode}
        summary = events(stdout)
        error = None
        if summary["tool_items"]:
            error = "Codex attempted tools outside the text packet; candidate rejected: " + ", ".join(sorted(set(summary["tool_items"])))
        elif process.returncode != 0:
            reason = summary["errors"][-1] if summary["errors"] else "check sfl doctor and CLI configuration"
            error = f"Codex CLI exited {process.returncode}: {reason}"
        elif not summary["complete"]:
            error = "Codex CLI did not report a completed turn"
        output = folder / "response.json"
        text = output.read_text(encoding="utf-8-sig") if output.exists() else None
        if text is None and error is None:
            error = "Codex CLI did not write its structured final response"
        return {"text": text, **summary, "error": error, "exit_code": process.returncode,
                "retryable": bool(error) and retryable_failure(summary)}


def doctor(profile: dict) -> dict:
    binary = executable(profile)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = environment()
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=15, creationflags=flags)
    login = subprocess.run([binary, "login", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=15, creationflags=flags)
    return {"provider": "codex_cli", "executable": binary, "version": version.stdout.strip(),
            "codex_home": env["CODEX_HOME"],
            "model": profile["model"], "effort": profile.get("effort", "high"), "logged_in": login.returncode == 0,
            "login_status": (login.stdout + login.stderr).strip()}
