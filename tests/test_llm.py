from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.support import RESPONSES, project
from storyforge.config import configuration
from storyforge.llm import Client, CallPaused, OutputError, extract_response
from storyforge.llm import codex_cli
from storyforge.checks.schema import schema_for, validate
from storyforge.packets import Packet
from storyforge.store import atomic_write, serialize


def sample_packet():
    return Packet("art_director", "ROLE CARD", {"script": "云清禾：走。"}, [], {})


class FakeProcess:
    def __init__(self, args, options, *, tool=False, timeout=False):
        self.args, self.options = args, options
        self.tool, self.timeout, self.killed, self.returncode = tool, timeout, False, 0
        self.folder = Path(options["cwd"])
        self.stdin = None
        self.instructions = (self.folder / "instructions.md").read_text(encoding="utf-8")
        self.schema = json.loads((self.folder / "schema.json").read_text(encoding="utf-8"))

    def communicate(self, text=None, timeout=None):
        if text is not None:
            self.stdin = json.loads(text)
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self.killed:
            return "", ""
        (self.folder / "response.json").write_text(serialize(RESPONSES["art_director:ep01"]), encoding="utf-8")
        events = [{"type": "item.completed", "item": {"type": "reasoning"}},
                  {"type": "item.completed", "item": {"type": "command_execution" if self.tool else "agent_message"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 123, "cached_input_tokens": 23, "output_tokens": 45}}]
        return "\n".join(json.dumps(e) for e in events), ""

    def kill(self):
        self.killed, self.returncode = True, -1


class CodexProcessTests(unittest.TestCase):
    def invoke(self, **settings):
        made = []
        def factory(args, **options):
            made.append(FakeProcess(args, options, **settings))
            return made[-1]
        with patch.object(codex_cli, "executable", return_value="codex.exe"), patch.object(codex_cli.subprocess, "Popen", side_effect=factory):
            result = codex_cli.run(configuration()["models"]["strong"], sample_packet(), {"type": "object"})
        return result, made[0]

    def test_defaults_use_user_selected_cli_model_and_effort_for_every_role(self):
        for profile in configuration()["models"].values():
            self.assertEqual((profile["provider"], profile["model"], profile["effort"]), ("codex_cli", "gpt-6-sol", "high"))

    def test_exec_is_stateless_and_receives_only_the_text_packet(self):
        result, process = self.invoke()
        args = process.args
        self.assertEqual(args[args.index("--model") + 1], "gpt-6-sol")
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertTrue({"--ephemeral", "--ignore-user-config", "--ignore-rules", "--output-schema"} <= set(args))
        self.assertNotIn("resume", args)
        self.assertNotIn("shell", process.options)
        self.assertTrue(process.options["env"]["CODEX_HOME"])
        self.assertEqual(process.stdin["data"], sample_packet().data)
        self.assertEqual(process.instructions, "ROLE CARD")
        self.assertEqual(process.schema, {"type": "object"})
        self.assertFalse(process.folder.exists())
        self.assertIsNone(result["error"])
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual(json.loads(result["text"]), RESPONSES["art_director:ep01"])

    def test_cli_schema_omits_unsupported_uniqueness_but_validation_keeps_it(self):
        schema = schema_for("scene_review")
        original = deepcopy(schema)
        made = []
        def factory(args,**options):
            made.append(FakeProcess(args,options))
            return made[-1]
        with patch.object(codex_cli,"executable",return_value="codex.exe"),patch.object(codex_cli.subprocess,"Popen",side_effect=factory):
            codex_cli.run(configuration()["models"]["reviewer"],sample_packet(),schema)
        self.assertNotIn("uniqueItems",made[0].schema["properties"]["script_notes"])
        self.assertTrue(made[0].stdin["output_schema"]["properties"]["script_notes"]["uniqueItems"])
        self.assertEqual(schema,original)
        note = {"location":"S01","evidence":"测试证据","note":"测试意见"}
        self.assertTrue(any("duplicate" in error for error in validate({"findings":[],"script_notes":[note,note]},schema)))
        with self.assertRaises(OutputError):Client.parse(serialize({"findings":[],"script_notes":[note,note]}),schema,None)
        self.assertFalse(validate({"findings":[],"script_notes":[note]},schema))
        board = codex_cli.structured_schema(schema_for("storyboard"))
        assets = board["properties"]["units"]["items"]["properties"]["assets"]
        self.assertNotIn("uniqueItems",assets)
        self.assertEqual(board["properties"]["units"]["items"]["properties"]["seconds"]["exclusiveMinimum"],0)

    def test_cli_request_error_preserves_actual_reason(self):
        class FailedProcess(FakeProcess):
            def communicate(self,text=None,timeout=None):
                self.returncode = 1
                return json.dumps({"type":"turn.failed","error":{"message":"invalid_json_schema: uniqueItems is not permitted"}}),""
        with patch.object(codex_cli,"executable",return_value="codex.exe"),patch.object(codex_cli.subprocess,"Popen",side_effect=lambda args,**options:FailedProcess(args,options)):
            result = codex_cli.run(configuration()["models"]["reviewer"],sample_packet(),{"type":"object"})
        self.assertIn("invalid_json_schema: uniqueItems",result["error"])
        self.assertNotIn("doctor",result["error"])
        self.assertFalse(result["complete"])

    def test_any_tool_item_rejects_a_completed_candidate(self):
        result, _ = self.invoke(tool=True)
        self.assertIn("candidate rejected", result["error"])
        self.assertEqual(result["tool_items"], ["command_execution"])

    def test_timeout_kills_process_and_keeps_usage_unknown(self):
        result, process = self.invoke(timeout=True)
        self.assertTrue(process.killed)
        self.assertIn("usage may be incomplete", result["error"])
        self.assertIsNone(result["text"])
        self.assertEqual(result["usage"], {})

    def test_native_retries_are_disabled_and_existing_login_is_retained(self):
        result, process = self.invoke()
        self.assertIsNone(result["error"])
        for option in ('model_provider="storyforge"',
                       'model_providers.storyforge.requires_openai_auth=true',
                       'model_providers.storyforge.request_max_retries=0',
                       'model_providers.storyforge.stream_max_retries=0',
                       'model_providers.storyforge.supports_websockets=false'):
            self.assertIn(option, process.args)
        self.assertFalse(any('base_url=' in a or 'env_key=' in a for a in process.args))

    def test_only_known_transient_failures_before_output_are_retryable(self):
        for message, expected in (
            ('unexpected status 503 Service Unavailable: test', True),
            ('unexpected status 429 Too Many Requests: test', True),
            ('{"status":502,"error":{"message":"test"}}', True),
            ('unexpected status 401 Unauthorized: test', False),
            ('unexpected status 400 Bad Request: invalid_json_schema', False),
            ('stream disconnected before completion: connection reset', True),
            ('unrecognized CLI failure', False)):
            with self.subTest(message=message):
                failed = json.dumps({'type':'turn.failed','error':{'message':message}})
                self.assertEqual(codex_cli.retryable_failure(codex_cli.events(failed)), expected)
                for kind in ('reasoning', 'agent_message', 'command_execution'):
                    started = json.dumps({'type':'item.started','item':{'type':kind}})
                    self.assertFalse(codex_cli.retryable_failure(codex_cli.events(started+'\n'+failed)))

    def test_event_reader_handles_noise_and_failed_turn(self):
        result = codex_cli.events('noise\n[]\n{"type":"turn.failed","error":"rate limited"}')
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"], ["rate limited"])

    def test_diagnostic_items_are_distinct_from_tool_actions(self):
        result = codex_cli.events('\n'.join(json.dumps(e) for e in [
            {"type":"item.completed","item":{"type":"error","message":"nonfatal startup diagnostic"}},
            {"type":"item.completed","item":{"type":"agent_message","text":"structured response"}},
            {"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":5}}]))
        self.assertTrue(result["complete"])
        self.assertEqual(result["diagnostic_items"],1)
        self.assertFalse(result["tool_items"])

    def test_default_cli_uses_npm_native_binary_before_another_installation(self):
        with tempfile.TemporaryDirectory(prefix="sfl-npm-") as temporary:
            root = Path(temporary)
            launcher = root / "codex.cmd"
            package = root / "node_modules/@openai/codex"
            package.mkdir(parents=True)
            (package / "package.json").write_text('{"name":"@openai/codex"}', encoding="utf-8")
            binary = package / "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict("os.environ", {"PROCESSOR_ARCHITECTURE":"AMD64","PROCESSOR_ARCHITEW6432":""}):
                self.assertEqual(codex_cli.npm_executable(launcher), binary)
            # The resolver must follow the default command, not prefer the app bundle.
            with patch.dict("os.environ", {"SFL_CODEX_COMMAND": ""}), patch.object(codex_cli.shutil, "which", side_effect=lambda name: str(launcher) if name == "codex" else None), patch.object(codex_cli, "npm_executable", return_value=binary) as resolve:
                self.assertEqual(codex_cli.executable({}), str(binary if codex_cli.os.name == "nt" else launcher))
                if codex_cli.os.name == "nt":
                    resolve.assert_called_once_with(launcher)

    def test_existing_codex_home_is_inherited_and_default_is_explicit(self):
        with patch.dict("os.environ", {"CODEX_HOME": "existing-cli-home"}):
            self.assertEqual(codex_cli.environment()["CODEX_HOME"], "existing-cli-home")
        with patch.dict("os.environ", {"CODEX_HOME": ""}):
            self.assertEqual(codex_cli.environment()["CODEX_HOME"], str(Path.home() / ".codex"))

    def test_login_status_and_exec_use_the_same_existing_home(self):
        responses = [subprocess.CompletedProcess([], 0, "codex-cli test", ""),
                     subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT")]
        with patch.object(codex_cli, "executable", return_value="codex.exe"), patch.object(codex_cli.subprocess, "run", side_effect=responses) as invoke, patch.dict("os.environ", {"CODEX_HOME": "existing-cli-home"}):
            report = codex_cli.doctor(configuration()["models"]["strong"])
        self.assertTrue(report["logged_in"])
        self.assertEqual(report["codex_home"], "existing-cli-home")
        self.assertTrue(all(c.kwargs["env"]["CODEX_HOME"] == "existing-cli-home" for c in invoke.call_args_list))


class CallReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-llm-")
        self.addCleanup(self.temporary.cleanup)
        self.store = project(Path(self.temporary.name) / "novel")
        self.config = configuration()

    def success(self, *args, **kwargs):
        return {"text": serialize(RESPONSES["art_director:ep01"]), "usage": {"input_tokens": 100, "output_tokens": 20}, "error": None, "exit_code": 0}

    def test_cache_avoids_spending_and_does_not_mix_reasoning_efforts(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(args[0]["effort"])
            return self.success()
        client = Client(self.store, self.config, codex_transport=transport)
        client.call(sample_packet(), "strong", "style", "B1", "ep01")
        client.call(sample_packet(), "strong", "style", "B1", "ep01")
        self.config["models"]["strong"]["effort"] = "medium"
        client.call(sample_packet(), "strong", "style", "B1", "ep01")
        self.assertEqual(calls, ["high", "medium"])
        rows = self.store.logs("calls")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["input_tokens"], 0)
        self.assertIsNone(rows[0]["cost"])

    def test_failed_call_cannot_poison_future_calls_with_same_fingerprint(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(1)
            return {"text": "", "error": "not logged in", "usage": {}} if len(calls) == 1 else self.success()
        client = Client(self.store, self.config, codex_transport=transport)
        with self.assertRaisesRegex(CallPaused, "not logged in"):
            client.call(sample_packet(), "strong", "style", "B1", "ep01")
        self.assertTrue(client.call(sample_packet(), "strong", "style", "B1", "ep01"))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.store.logs("calls")[0]["input_tokens"])

    def test_receipt_recovers_missing_log_and_cache_once(self):
        client = Client(self.store, self.config, codex_transport=self.success)
        client.call(sample_packet(), "strong", "style", "B1", "ep01")
        receipt = json.loads(next((self.store.runtime / "receipts").glob("*.json")).read_text(encoding="utf-8"))
        # Simulate a crash after the durable receipt but before its projections.
        atomic_write(self.store.path("logs/calls.jsonl"), "")
        self.store.path(f".cache/llm/{receipt['fingerprint']}.json").unlink()
        restored = Client(self.store, self.config, codex_transport=lambda *a, **k: self.fail("Recovered call must be cached"))
        restored.call(sample_packet(), "strong", "style", "B1", "ep01")
        Client(self.store, self.config, codex_transport=self.success)
        self.assertEqual(len([r for r in self.store.logs("calls") if not r["cached"]]), 1)

    def test_process_failure_pauses_and_records_unknown_usage(self):
        def failure(*args, **kwargs):
            raise FileNotFoundError("missing binary")
        with self.assertRaisesRegex(CallPaused, "process failed"):
            Client(self.store, self.config, codex_transport=failure).call(sample_packet(), "strong", "style", "B1", "ep01")
        self.assertIsNone(self.store.logs("calls")[0]["cost"])

    def test_transient_cli_attempts_back_off_record_each_attempt_and_cache_success(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(kwargs['target'])
            if len(calls)<3:
                return {'error':'synthetic 503','retryable':True,'text':None,'usage':{},'exit_code':1}
            return self.success()
        client = Client(self.store, self.config, codex_transport=transport)
        with patch('storyforge.llm.time.sleep') as sleep:
            value = client.call(sample_packet(), 'strong', 'style', 'B1', 'ep01')
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1,2])
        self.assertEqual(value, RESPONSES['art_director:ep01'])
        rows = self.store.logs('calls')
        self.assertEqual([row['attempt'] for row in rows], [1,2,3])
        self.assertEqual([row['status'] for row in rows], ['output_error','output_error','received'])
        self.assertTrue(all(row['cost'] is None for row in rows))
        self.assertTrue(all(row['input_tokens'] is None for row in rows[:2]))
        self.assertEqual(len(list((self.store.runtime/'receipts').glob('*.json'))), 3)
        client.call(sample_packet(), 'strong', 'style', 'B1', 'ep01')
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.store.logs('calls')[-1]['cached'])

    def test_exhausted_cli_attempts_pause_only_the_failed_job(self):
        from storyforge.runner import Runner
        from storyforge.runner.director import Director
        def transport(*args, **kwargs):
            return {'error':'synthetic 503','retryable':True,'text':None,'usage':{},'exit_code':1}
        client = Client(self.store, self.config, codex_transport=transport)
        runner = Runner(self.store, config=self.config, client=client)
        with patch('storyforge.llm.time.sleep') as sleep:
            with self.assertRaisesRegex(CallPaused, 'synthetic 503'):
                Director(runner,1).art_direction('B1')
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(self.store.logs('calls')), 3)
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE stage='B1'").fetchone()['status'],'paused')
        self.assertFalse(self.store.json('.runtime/control.json',{}).get('paused'))
        self.assertFalse(list((self.store.root/'.cache/llm').glob('*.json')))
        client.codex_transport = self.success
        self.assertEqual(client.call(sample_packet(),'strong','style','B1','ep02'), RESPONSES['art_director:ep01'])

    def test_cli_attempt_limit_uses_config_and_rejects_invalid_values(self):
        calls=[]
        def failure(*args,**kwargs):
            calls.append(True)
            return {'error':'synthetic 503','retryable':True,'usage':{}}
        self.config['external_attempts']=2
        client=Client(self.store,self.config,codex_transport=failure)
        with patch('storyforge.llm.time.sleep') as sleep:
            with self.assertRaises(CallPaused):
                client.call(sample_packet(),'strong','style','B1','ep01')
            self.assertEqual(sleep.call_args_list[0].args,(1,))
        self.assertEqual(len(calls),2)
        for value in (0,-1,True,1.5,'3'):
            self.config['external_attempts']=value
            with self.assertRaisesRegex(CallPaused,'positive integer'):
                client.call(sample_packet(),'strong','style','B1','ep01')
        self.assertEqual(len(calls),2)

    def test_no_login_prevents_model_submission(self):
        with patch.object(codex_cli, "doctor", return_value={"logged_in": False}), patch.object(codex_cli, "run") as transport:
            client = Client(self.store, self.config)
            with self.assertRaisesRegex(CallPaused, "not logged in"):
                client.call(sample_packet(), "strong", "style", "B1", "ep01")
            transport.assert_not_called()
        self.assertFalse(self.store.logs("calls"))

    def test_optional_api_transport_posts_real_loopback_schema_request(self):
        captured = []
        response = {"status": "completed", "usage": {"input_tokens": 4, "output_tokens": 8},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": serialize(RESPONSES["art_director:ep01"])}]}]}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                captured.append((handler.path, json.loads(handler.rfile.read(int(handler.headers["Content-Length"])).decode("utf-8"))))
                payload = json.dumps(response).encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["models"]["strong"] = {"provider": "openai_compatible", "model": "contract-test",
                "api_key_env": "SFL_TEST_API_KEY", "base_url": f"http://127.0.0.1:{server.server_port}", "api": "responses", "max_output_tokens": 1000}
            with patch.dict("os.environ", {"SFL_TEST_API_KEY": "fixture-only"}):
                value = Client(self.store, self.config).call(sample_packet(), "strong", "style", "B1", "ep01")
            self.assertEqual(value, RESPONSES["art_director:ep01"])
            path, body = captured[0]
            self.assertEqual(path, "/responses")
            self.assertFalse(body["store"])
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertEqual(len(body["input"]), 2)
            self.assertEqual(self.store.logs("calls")[0]["output_tokens"], 8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_optional_api_refusal_or_incomplete_result_cannot_succeed(self):
        incomplete = {"status": "incomplete", "output": []}
        self.assertIsNotNone(extract_response(incomplete, "responses")[3])
        refused = {"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}
        self.assertIsNotNone(extract_response(refused, "chat_completions")[3])
        complete = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
        self.assertEqual(extract_response(complete, "chat_completions"), ("{}", 2, 1, None))


if __name__ == "__main__":
    unittest.main()
