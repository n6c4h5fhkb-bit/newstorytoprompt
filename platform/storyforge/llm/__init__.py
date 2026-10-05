from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit, quote
import uuid

from storyforge.config import SflError
from storyforge.checks.schema import require, schema_for
from storyforge.packets import Packet
from storyforge.store import Store, atomic_write, digest, now, serialize


class CallPaused(SflError):
    pass


class OutputError(SflError):
    def __init__(self, message: str, previous=None):
        super().__init__(message)
        self.previous = previous


def request_body(profile: dict, packet: Packet, schema: dict) -> tuple[str, dict, dict]:
    provider = profile.get("provider")
    model = profile.get("model") or os.environ.get(profile.get("model_env", ""))
    key = os.environ.get(profile.get("api_key_env", ""))
    if not model or not key:
        raise CallPaused(f"Configure {profile.get('model_env', 'model')} and {profile.get('api_key_env', 'API key')} before real model calls")
    base = os.environ.get(profile.get("base_url_env", "")) or profile.get("base_url", "https://api.openai.com/v1")
    base = base.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
        raise CallPaused("API URL must use HTTPS or a loopback HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CallPaused("API URL cannot contain credentials, query or fragment")
    content = serialize({"data": packet.data, "repair": packet.repairs, "output_schema": schema})
    mode = profile.get("structured_output", "json_schema")
    output_format = {"type": "json_schema", "name": packet.role, "strict": True, "schema": schema} if mode == "json_schema" else {"type": "json_object"}
    messages = [{"role": "system", "content": packet.instruction}, {"role": "user", "content": content}]
    if provider in ("openai", "openai_compatible"):
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        if profile.get("api", "responses") == "responses":
            body = {"model": model, "input": messages, "store": False,
                    "text": {"format": output_format}, "max_output_tokens": profile["max_output_tokens"]}
            return base + "/responses", headers, body
        if profile["api"] == "chat_completions":
            response_format = {"type": "json_schema", "json_schema": {"name": packet.role, "strict": True, "schema": schema}} if mode == "json_schema" else {"type": "json_object"}
            body = {"model": model, "messages": messages, "response_format": response_format,
                    "max_completion_tokens": profile["max_output_tokens"]}
            return base + "/chat/completions", headers, body
    raise CallPaused(f"Unsupported provider/API: {provider}/{profile.get('api')}")


def extract_response(raw: dict, api: str) -> tuple[str, int | None, int | None, str | None]:
    usage = raw.get("usage") or {}
    if api == "chat_completions":
        choices = raw.get("choices", [])
        if not choices or choices[0].get("finish_reason") != "stop":
            return "", usage.get("prompt_tokens"), usage.get("completion_tokens"), "Model response did not finish normally"
        message = choices[0]["message"]
        if message.get("refusal"):
            return "", usage.get("prompt_tokens"), usage.get("completion_tokens"), "Model refused this request"
        return message.get("content") or "", usage.get("prompt_tokens"), usage.get("completion_tokens"), None
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if raw.get("status") != "completed":
        return "", input_tokens, output_tokens, "Model response incomplete: " + str(raw.get("status"))
    chunks = [part for item in raw.get("output", []) if item.get("type") == "message" for part in item.get("content", [])]
    if any(p.get("type") == "refusal" for p in chunks):
        return "", input_tokens, output_tokens, "Model refused this request"
    return "".join(p.get("text", "") for p in chunks if p.get("type") == "output_text"), input_tokens, output_tokens, None


def http_post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=serialize(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class Client:
    def __init__(self, store: Store, config: dict, *, transport=http_post, codex_transport=None):
        self.store, self.config, self.transport = store, config, transport
        self.check_codex_login = codex_transport is None
        self._codex_ready = False
        if codex_transport is None:
            from storyforge.llm.codex_cli import run
            codex_transport = run
        self.codex_transport = codex_transport
        self.semaphore = threading.BoundedSemaphore(config["concurrency"]["llm"])
        self._lock = threading.Lock()
        self._calls: dict[str, threading.Lock] = {}
        self.recover_receipts()

    def recover_receipts(self):
        for path in (self.store.runtime / "receipts").glob("*.json"):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.store.append("calls", receipt["log"], unique_id=receipt["log"]["id"])
            if receipt.get("text") and not receipt.get("error") and not self.store.path(f".cache/llm/{receipt['fingerprint']}.json").exists():
                atomic_write(self.store.path(f".cache/llm/{receipt['fingerprint']}.json"), serialize(receipt))

    def call(self, packet: Packet, tier: str, schema_name: str, stage: str, target: str) -> dict:
        schema = schema_for(schema_name)
        profile = self.config["models"][tier]
        identity = {k: profile.get(k) for k in ("provider", "model", "effort", "command", "api", "base_url", "structured_output", "max_output_tokens", "replay_dir")}
        identity["model"] = profile.get("model") or os.environ.get(profile.get("model_env", ""))
        identity["base_url"] = os.environ.get(profile.get("base_url_env", "")) or profile.get("base_url")
        fingerprint = digest({"profile": identity, "role": packet.role, "instruction": packet.instruction,
                              "data": packet.data, "repairs": packet.repairs, "schema": schema, "target": target})
        with self._lock:
            mutex = self._calls.setdefault(fingerprint, threading.Lock())
        with mutex, self.semaphore:
            cached = self.store.json(f".cache/llm/{fingerprint}.json")
            if cached:
                self.store.append("calls", {"id": "cache_" + uuid.uuid4().hex, "stage": stage, "target": target,
                    "role": packet.role, "model": identity["model"], "input_tokens": 0, "output_tokens": 0,
                    "cost": 0, "seconds": 0, "cached": True, "fingerprint": fingerprint,
                    "demo": bool(cached["log"].get("demo"))})
                return self.parse(cached["text"], schema, cached.get("error"))
            attempts = self.config.get("external_attempts", 3) if profile["provider"] == "codex_cli" else 1
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
                raise CallPaused("external_attempts must be a positive integer")
            for attempt in range(attempts):
                started = time.monotonic()
                if profile["provider"] == "replay":
                    if not self.config.get("demo"):
                        raise CallPaused("Replay provider is allowed only in an explicitly marked demo project")
                    filename = f"{packet.role}__{quote(target, safe='')}.json"
                    path = Path(profile["replay_dir"]) / filename
                    if path.exists():
                        text = path.read_text(encoding="utf-8-sig")
                    else:
                        manifest_path = Path(profile["replay_dir"]) / "responses.json"
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.exists() else {}
                        if packet.role + ":" + target not in manifest:
                            raise CallPaused(f"Missing explicit demo response: {filename}")
                        text = serialize(manifest[packet.role + ":" + target])
                    input_tokens, output_tokens, error, model = 0, 0, None, "replay-fixture"
                elif profile["provider"] == "codex_cli":
                    if self.check_codex_login and not self._codex_ready:
                        from storyforge.llm.codex_cli import doctor
                        try:
                            ready = doctor(profile)["logged_in"]
                        except (SflError, OSError, subprocess.SubprocessError) as exc:
                            raise CallPaused("Codex CLI preflight failed; check sfl doctor and the configured executable") from exc
                        if not ready:
                            raise CallPaused("Codex CLI is not logged in; run codex login before real calls")
                        self._codex_ready = True
                    try:
                        result = self.codex_transport(profile, packet, schema, stage=stage, target=target)
                    except (OSError, subprocess.SubprocessError) as exc:
                        result = {"error": "Codex CLI process failed; call usage may be incomplete", "usage": {}, "text": None}
                    text, error = result.get("text") or "", result.get("error")
                    usage = result.get("usage") or {}
                    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
                    model = profile["model"]
                else:
                    url, headers, body = request_body(profile, packet, schema)
                    model = body["model"]
                    raw = None
                    for http_attempt in range(self.config.get("external_attempts", 3)):
                        try:
                            raw = self.transport(url, headers, body, profile.get("timeout_seconds", 180))
                            break
                        except urllib.error.HTTPError as exc:
                            retryable = exc.code in (408, 409, 429) or exc.code >= 500
                            self.store.append("calls", {"id": "failed_" + uuid.uuid4().hex, "stage": stage, "target": target,
                                "role": packet.role, "model": model, "input_tokens": None, "output_tokens": None,
                                "cost": None, "seconds": time.monotonic() - started, "cached": False,
                                "status": f"http_{exc.code}", "fingerprint": fingerprint})
                            if not retryable or http_attempt == self.config.get("external_attempts", 3) - 1:
                                raise CallPaused(f"Model request failed (HTTP {exc.code}); this job is paused") from exc
                            time.sleep(2 ** min(http_attempt, 3))
                        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                            self.store.append("calls", {"id": "failed_" + uuid.uuid4().hex, "stage": stage, "target": target,
                                "role": packet.role, "model": model, "input_tokens": None, "output_tokens": None,
                                "cost": None, "seconds": time.monotonic() - started, "cached": False,
                                "status": "transport_unknown", "fingerprint": fingerprint})
                            if http_attempt == self.config.get("external_attempts", 3) - 1:
                                raise CallPaused("Model connection failed; this job is paused (billing status unknown)") from exc
                            time.sleep(2 ** min(http_attempt, 3))
                    text, input_tokens, output_tokens, error = extract_response(raw, profile.get("api", "responses"))
                rates = profile.get("prices_per_million", {})
                cost = 0 if profile["provider"] == "replay" else None
                if input_tokens is not None and output_tokens is not None and all(rates.get(k) is not None for k in ("input", "output")):
                    cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
                log = {"id": "call_" + uuid.uuid4().hex, "stage": stage, "target": target, "role": packet.role,
                       "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost,
                       "seconds": round(time.monotonic() - started, 4), "cached": False, "fingerprint": fingerprint,
                       "status": "output_error" if error else "received", "demo": profile["provider"] == "replay"}
                if profile["provider"] == "codex_cli":
                    log.update(provider="codex_cli", effort=profile.get("effort", "high"),
                               cached_input_tokens=usage.get("cached_input_tokens"), exit_code=result.get("exit_code"),
                               tool_items=result.get("tool_items", []),diagnostic_items=result.get("diagnostic_items",0), attempt=attempt + 1)
                receipt = {"fingerprint": fingerprint, "text": text, "error": error, "log": log}
                atomic_write(self.store.path(f".runtime/receipts/{log['id']}.json"), serialize(receipt))
                self.store.append("calls", log, unique_id=log["id"])
                if not error and text:
                    atomic_write(self.store.path(f".cache/llm/{fingerprint}.json"), serialize(receipt))
                if error and profile["provider"] == "codex_cli" and result.get("retryable") and attempt + 1 < attempts:
                    time.sleep(2 ** min(attempt, 3))
                    continue
                return self.parse(text, schema, error)

    @staticmethod
    def parse(text: str, schema: dict, error: str | None):
        if error:
            raise CallPaused(error)
        try:
            data = json.loads(text)
            return require(data, schema)
        except (ValueError, SflError) as exc:
            raise OutputError(str(exc), text[:12000]) from exc
