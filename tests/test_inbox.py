import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import zipfile
from html import escape
from urllib.parse import urlencode
from copy import deepcopy
from unittest.mock import patch

from tests.support import RESPONSES, project, scripted_runner, approve_look, outfit_child
from tests.support_writer import novel_project, WriterCodex, writer_runner
from storyforge.cards import Cards
from storyforge.config import configuration
from storyforge.llm import Client, CallPaused
from storyforge.runner import Runner
from storyforge.runner.director import Director
from storyforge.runner import service
from storyforge.ui import make_server


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sfl-inbox-")
        self.addCleanup(self.tmp.cleanup)
        self.store = project(Path(self.tmp.name) / "story")

    def test_continuity_changes_only_following_units_and_keeps_existing_clip_current(self):
        runner, transport = scripted_runner(self.store)
        approve_look(runner)
        self.assertFalse(runner.run()["waiting"])
        earlier = self.store.text("prompts/ep01/u01.md")
        result = service.note(self.store, "ep01_u01", "云清禾现在坐着，腕镣仍在地上。")
        self.assertEqual(result["following_units"], ["ep01_u02", "ep01_u03"])
        stale = self.store.stale_artifacts()
        self.assertNotIn("prompts/ep01/u01.md", stale)
        self.assertNotIn(".state/reviews/ep01_u01.json", stale)
        self.assertNotIn("delivery/ep01/u01.md", stale)
        self.assertTrue(self.store.current("storyboard/ep01.json"))
        self.assertTrue(self.store.current("prompts/ep01/u01.md"))
        self.assertIn("prompts/ep01/u02.md", stale)
        self.assertIn("prompts/ep01/u03.md", stale)
        before = sum(transport.counts.values())
        self.assertTrue(runner.run()["waiting"])
        self.assertEqual(sum(transport.counts.values()), before)
        report = runner.run(allow_stale=["ep01"])
        self.assertFalse(report["waiting"], report)
        self.assertEqual(self.store.text("prompts/ep01/u01.md"), earlier)
        self.assertEqual(transport.counts["reconstruction_blind:ep01_u01"], 1)
        requested = [u["id"] for role,target,data,repairs in transport.packets[before:] if role == "unit_prompt" for u in data["units"]]
        self.assertNotIn("ep01_u01", requested)
        self.assertTrue(any(r["result"] == "continuity" for r in self.store.logs("feedback")))
        self.assertEqual(service.metrics(self.store)["reported_units"], 0)

    def test_scene_card_does_not_block_earlier_stages_or_other_scene(self):
        service.import_episode(self.store, self.store.text("episodes/ep01.md") + "\n## S02 妖商店 · 日 · 内\n出场：林恒、云清禾、妖商\n△ 林恒与云清禾已离开，妖商看向空门口。\n", self.store.text("bible.md"), self.store.text("ledger/ep00.md"))
        board = deepcopy(RESPONSES["storyboard:ep01"])
        board["units"][-1]["scene"] = "S02"
        fixture = patch.dict(RESPONSES, {"storyboard:ep01": board,
            "unit_prompt:ep01:S02": deepcopy(RESPONSES["unit_prompt:ep01:S01"]),
            "scene_fidelity:ep01:S02": {"findings": [],"script_notes":[]}})
        fixture.start()
        self.addCleanup(fixture.stop)
        cards = Cards(self.store, configuration(self.store.root))
        cards.create(kind="stuck", stage="B7", target="ep01:S01", question="测试", options=[{"key":"retry","label":"重试"},{"key":"stop","label":"停止"}], recommended="retry", reason="测试", dedupe="scope")
        self.assertFalse(cards.blocking("B1", "ep01"))
        self.assertFalse(cards.blocking("B5", "ep01"))
        self.assertTrue(cards.blocking("B7", "ep01:S01"))
        self.assertFalse(cards.blocking("B7", "ep01:S02"))
        runner, transport = scripted_runner(self.store)
        approve_look(runner)
        report = runner.run()
        self.assertTrue(report["waiting"])
        self.assertEqual(transport.counts["unit_prompt:ep01:S01"], 0)
        self.assertEqual(transport.counts["unit_prompt:ep01:S02"], 1)
        self.assertTrue(self.store.current(".state/reviews/ep01_u03.json"))

    def test_script_note_records_request_without_rewriting_locked_script(self):
        before = self.store.text("episodes/ep01.md")
        self.assertTrue(service.note(self.store, "ep01", "强化结尾的悬念")["pending"])
        self.assertEqual(self.store.text("episodes/ep01.md"), before)
        self.assertIn("强化结尾的悬念", self.store.text("notes/script_notes.md"))

    def test_asset_preview_includes_scenes_props_and_older_cards(self):
        runner, _ = scripted_runner(self.store)
        self.assertTrue(runner.run(until="B4")["waiting"])
        item = next(i for i in service.inbox(self.store)["items"] if i.get("card", {}).get("stage") == "B4")
        assets = item["preview"]["assets"]
        self.assertEqual([a["name"] for a in assets if a["type"] == "location"], ["妖商店"])
        self.assertEqual({a["name"] for a in assets if a["type"] == "prop"}, {"腕镣", "铜钥匙"})
        self.assertTrue(all(a["image_prompt"] for a in assets))
        # Existing on-disk cards must also reveal their already extracted assets.
        card = deepcopy(item["card"])
        card["details"].pop("asset_preview")
        with patch.object(Cards, "list", return_value=[card]):
            legacy = next(i for i in service.inbox(self.store)["items"] if i["id"] == card["id"])
        self.assertEqual(legacy["preview"]["assets"], assets)
        self.assertIsNone(legacy["card"]["answer"])
        # Only characters belong to this checkpoint's saved asset approval.
        # Updated scene/prop prompts must be visible without rewriting its history.
        decisions = self.store.logs("decisions")
        current = self.store.assets()
        for row in current:
            if row["id"] == "prop:腕镣":
                row["image_prompt"] = "更新后的腕镣四宫格 Prompt"
            elif row["id"] == "char:林恒":
                row["image_prompt"] = "尚未提交确认的新角色 Prompt"
        self.store.write("assets.csv", self.store.assets_text(current))
        refreshed = next(i for i in service.inbox(self.store)["items"] if i["id"] == item["id"])
        shown = {a["id"]: a for a in refreshed["preview"]["assets"]}
        self.assertEqual(shown["prop:腕镣"]["image_prompt"], "更新后的腕镣四宫格 Prompt")
        self.assertEqual(shown["char:林恒"]["image_prompt"], next(a["image_prompt"] for a in assets if a["id"] == "char:林恒"))
        self.assertEqual(refreshed["card"]["details"], item["card"]["details"])
        self.assertEqual(self.store.logs("decisions"), decisions)

    def test_episode_assets_include_reused_parents_and_exclude_other_episodes(self):
        runner, _ = scripted_runner(self.store)
        self.assertTrue(runner.run(until="B4")["waiting"])
        rows = self.store.assets()
        definition, response = outfit_child()
        child = {**definition, "id":"char:云清禾@换装", "image_prompt":response["brief"], "status":"described"}
        unrelated = {**rows[0], "id": "char:其他集人物", "name": "其他集人物", "placeholder": "@其他集人物_母图"}
        self.store.write("assets.csv", self.store.assets_text(rows + [child, unrelated]))
        self.store.write(".state/stages/ep01_B2.json", {"assets": ["@云清禾_换装", "@妖商店_母图", "@道具_腕镣"]}, json_data=True)
        details = service.episode_details(self.store, "ep01")
        self.assertEqual({a["id"] for a in details["episode_assets"]}, {"char:云清禾", "char:云清禾@换装", "loc:妖商店", "prop:腕镣"})
        self.assertIn(unrelated, details["assets"])

    def test_applied_look_rejection_history_does_not_request_a_rerun(self):
        self.store.write("style.md", "旧美术")
        marker = ".state/look_revisions/applied.json"
        bindings = [self.store.binding("style.md")]
        job = self.store.start_job("B1", "ep01", bindings)
        self.assertTrue(self.store.accept(job, "B1", "ep01", bindings, {marker:'{"applied":true}'}))
        self.store.write("style.md", "更新后的美术")
        self.assertIn(marker, self.store.stale_artifacts())
        self.assertFalse(any(i["type"] == "stale" for i in service.inbox(self.store)["items"]))
        self.assertEqual(self.store.json(marker), {"applied":True})

    def test_pause_prevents_new_model_calls_and_resume_reuses_completed_work(self):
        runner, transport = scripted_runner(self.store)
        service.pause(self.store)
        self.assertTrue(runner.run()["paused"])
        self.assertFalse(transport.counts)
        service.resume(self.store)
        self.assertTrue(runner.run()["waiting"])

    def test_final_unit_note_reaches_next_episode_and_rejects_a_late_storyboard(self):
        runner, _ = scripted_runner(self.store)
        approve_look(runner)
        self.assertFalse(runner.run()["waiting"])
        metadata = self.store.json(".state/artifacts.json")["storyboard/ep01.json"]
        bindings = metadata["bindings"]
        original = self.store.text("storyboard/ep01.json")
        job = self.store.start_job("B5", "ep01", bindings)
        self.assertEqual(service.note(self.store, "ep01_u03", "妖商移到门口，钥匙留在柜台。")["following_units"], [])
        self.assertTrue(self.store.current("storyboard/ep01.json"))
        self.assertTrue(self.store.current("prompts/ep01/u03.md"))
        self.assertFalse(self.store.accept(job, "B5", "ep01", bindings, {"storyboard/ep01.json":original}))
        self.store.write("episodes/ep02.md", self.store.text("episodes/ep01.md").replace("EP01", "EP02"))
        locks = self.store.json(".state/locks.json")
        locks["ep02"] = {"locked": True}
        self.store.write(".state/locks.json", locks, json_data=True)
        carry = Director(runner, 2).previous_carry()
        self.assertIn("妖商移到门口", carry.data)


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sfl-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.server = make_server(Path(self.tmp.name), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.app.pool.shutdown(wait=True)
        self.thread.join(2)

    def get(self, path):
        with urlopen(self.base + path, timeout=15) as response:
            return json.load(response)

    def post(self, **body):
        request = Request(self.base + "/api/command", data=json.dumps(body).encode("utf-8"), headers={"Content-Type":"application/json"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def html(self, path):
        with urlopen(self.base + path, timeout=15) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            return response.read().decode("utf-8")

    def submit(self, **fields):
        request = Request(self.base + "/action", data=urlencode(fields).encode("utf-8"),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(request, timeout=15) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            return response.read().decode("utf-8")

    def wait_idle(self):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = self.get("/api/status?project=sample")
            if not state["background"]["running"]:
                self.assertNotIn("error", state["background"]["report"])
                return state
            time.sleep(.05)
        self.fail("UI background run did not finish")

    def test_second_server_cannot_share_the_active_port(self):
        with self.assertRaises(OSError):
            make_server(Path(self.tmp.name), self.server.server_port)
        self.assertEqual(self.get("/api/projects"), {"projects":[]})

    def test_unaccepted_script_is_previewed_on_its_retry_card_without_acceptance(self):
        store = novel_project(Path(self.tmp.name)/"sample",1)
        def unresolved(value,packet,target,count):
            if packet.role=="viewer":
                value["findings"]=[{"severity":"major","kind":"error","location":"S01", "evidence":"打开它。",
                    "problem":"测试：修订意见尚未落实", "suggested_fix":"测试：继续修订"}]
        runner, model = writer_runner(store,1,mutate=unresolved)
        report = runner.run(until="A8",episodes=[1])
        self.assertFalse(report["paused"],report)
        self.assertTrue(report["waiting"])
        before = (len(store.logs("calls")),len(store.logs("decisions")))
        item = next(i for i in self.get("/api/inbox?project=sample")["items"] if i["type"]=="card")
        saved = store.json(item["card"]["details"]["candidate_file"])
        self.assertEqual(item["preview"]["script"],saved["candidate"]["script"])
        self.assertFalse(item["preview"]["review"]["passed"])
        html = self.html("/?project=sample&view=inbox")
        self.assertIn("剧本需要继续修订",html)
        self.assertIn("待修订候选稿 · 尚未通过审查",html)
        self.assertIn(escape(saved["candidate"]["script"],quote=True),html)
        self.assertFalse(store.path("episodes/ep01.md").exists())
        self.assertFalse(store.json(".state/locks.json",{}).get("ep01",{}).get("locked"))
        self.assertIsNone(item["card"]["answer"])
        self.assertEqual(before,(len(store.logs("calls")),len(store.logs("decisions"))))

    def test_full_episode_can_be_handled_through_inbox_and_episode_services(self):
        self.post(action="demo", project="sample")
        store = self.server.app.store("sample")
        before = (len(store.jobs()), len(store.logs("calls")), len(store.logs("decisions")))
        initial = self.get("/api/episode?project=sample&episode=ep01")
        example = initial["demo_preview"]
        self.assertFalse(initial["delivery_ready"])
        self.assertEqual(initial["units"], [])
        self.assertEqual(len(example["units"]), 3)
        html = self.html("/?project=sample&view=episode&episode=ep01")
        for label in ("完整演示预览", "美术定调生图 Prompt", "四视图设定表", "2×2四宫格", "SC01-U01", "SC01-U03", "复制完整视频 Prompt"):
            self.assertIn(label, html)
        self.assertNotIn('name="result"', html)
        self.assertNotIn(">下载交付包<", html)
        self.assertFalse(store.path("storyboard/ep01.json").exists())
        self.assertFalse(store.path(".state/look.json").exists())
        self.assertEqual(before, (len(store.jobs()), len(store.logs("calls")), len(store.logs("decisions"))))
        self.post(action="run", project="sample")
        self.wait_idle()
        inbox = self.get("/api/inbox?project=sample")
        item = next(i for i in inbox["items"] if i["type"] == "card")
        self.assertTrue(item["preview"]["style"])
        self.assertTrue(item["preview"]["characters"])
        self.assertEqual({a["type"] for a in item["preview"]["assets"]} & {"location", "prop"}, {"location", "prop"})
        self.post(action="answer", project="sample", card=item["card"]["id"], option="approve")
        self.post(action="run", project="sample")
        state = self.wait_idle()
        self.assertNotIn("jobs", state)
        self.assertTrue(state["progress"]["ep01"]["delivery"])
        details = self.get("/api/episode?project=sample&episode=ep01")
        self.assertEqual(len(details["units"]), 3)
        self.assertTrue(details["delivery_ready"])
        self.assertIsNone(details["demo_preview"])
        self.assertEqual([u["prompt"] for u in details["units"]], [u["prompt"] for u in example["units"]])
        self.assertEqual({a["type"] for a in details["episode_assets"]} & {"location", "prop"}, {"location", "prop"})
        with urlopen(self.base + "/api/delivery?project=sample&episode=ep01") as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertIn("u01.md", archive.namelist())
                self.assertIn(details["art_direction"]["art_prompt"], archive.read("assets.md").decode("utf-8"))
                self.assertTrue(all(Path(n).suffix in (".md", ".json") for n in archive.namelist()))
        self.post(action="feedback", project="sample", unit="ep01_u01", result="ok")
        self.post(action="note", project="sample", target="ep01_u01", note="她已坐在椅子上")
        targets = [i["target"] for i in self.get("/api/inbox?project=sample")["items"] if i["type"] == "stale"]
        self.assertNotIn("ep01_u01", targets)
        self.assertIn("ep01_u02", targets)
        with self.assertRaises(HTTPError):
            urlopen(self.base + "/api/delivery?project=sample&episode=ep01")

    def test_retry_after_restart_preserves_stage_and_episode_scope(self):
        store = novel_project(Path(self.tmp.name) / "sample", count=3)
        def fail_once(value, packet, target, count):
            if packet.role == "adapt_plan" and count == 1:
                raise CallPaused("Synthetic connection failure")
        model = WriterCodex(count=3, mutate=fail_once)
        def runner(current_store):
            config = configuration(current_store.root)
            return Runner(current_store, config=config, client=Client(current_store, config, codex_transport=model))
        with patch("storyforge.ui.Runner", side_effect=runner):
            self.post(action="run", project="sample", until="A8", episodes=[2])
            self.assertTrue(self.wait_idle()["background"]["report"]["paused"])
            self.close()
            self.server = make_server(Path(self.tmp.name), 0)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base = f"http://127.0.0.1:{self.server.server_port}"
            self.submit(action="retry", project="sample", _view="inbox")
            self.wait_idle()
            self.assertEqual(model.counts["amplify:ep01:m01"], 1)
            self.assertEqual(model.counts["amplify:ep02:m02"], 0)
            self.assertEqual(model.counts["amplify:ep03:m03"], 0)
            self.assertEqual(model.counts["breakdown:project"], 1)
            card = next(c for c in Cards(store, configuration(store.root)).list() if c["stage"] == "A8")
            self.post(action="answer", project="sample", card=card["id"], option="approve")
            self.post(action="retry", project="sample", until="A10")
            self.wait_idle()
            self.assertEqual(set(store.json(".state/locks.json")), {"ep01", "ep02"})
            self.assertEqual(model.counts["amplify:ep03:m03"], 0)
            self.post(action="run", project="sample", until="A10")
            self.wait_idle()
            self.assertTrue(store.json(".state/locks.json")["ep03"]["locked"])

    def test_continue_after_checkpoints_keeps_selected_first_episode(self):
        store = novel_project(Path(self.tmp.name)/"sample",3)
        model = WriterCodex(count=3)
        def runner(current_store):
            config = configuration(current_store.root)
            return Runner(current_store,config=config,client=Client(current_store,config,codex_transport=model))
        with patch("storyforge.ui.Runner",side_effect=runner):
            self.post(action="run",project="sample",until="A8",episodes=[1])
            self.wait_idle()
            first = next(i["card"] for i in self.get("/api/inbox?project=sample")["items"] if i["type"]=="card")
            self.assertEqual(first["stage"],"A8")
            self.post(action="answer",project="sample",card=first["id"],option="approve")
            self.post(action="run",project="sample")
            self.wait_idle()
            self.assertEqual(store.json(".runtime/run_scope.json"),{"until":"B9","episodes":[1]})
            look = next(i["card"] for i in self.get("/api/inbox?project=sample")["items"] if i["type"]=="card")
            self.assertEqual(look["stage"],"B4")
            self.post(action="answer",project="sample",card=look["id"],option="approve")
            self.post(action="run",project="sample")
            state = self.wait_idle()
        self.assertTrue(state["progress"]["ep01"]["delivery"])
        self.assertEqual(set(store.json(".state/locks.json")),{"ep01"})
        self.assertFalse(store.path("amplified/m02.json").exists())
        self.assertEqual(model.counts["episode_writer:ep02"],0)

    def test_ui_retry_keeps_imported_episode_cli_stop_stage(self):
        self.post(action="demo", project="sample")
        store = self.server.app.store("sample")
        Runner(store).run(until="B1", episodes=[1])
        calls = len(store.logs("calls"))
        self.submit(action="retry", project="sample", _view="inbox")
        self.wait_idle()
        self.assertEqual(len(store.logs("calls")), calls)
        self.assertFalse(store.assets())
        self.post(action="run", project="sample", until="B2")
        self.wait_idle()
        self.assertTrue(store.assets())

    def test_cross_origin_mutations_and_private_files_are_rejected(self):
        request = Request(self.base + "/api/command", data=b'{"action":"demo","project":"wrong"}',
                          headers={"Content-Type":"application/json", "Origin":"https://unrelated.example"})
        with self.assertRaises(HTTPError):
            urlopen(request)
        self.assertFalse((Path(self.tmp.name) / "wrong").exists())
        self.post(action="demo", project="sample")
        for path in [".runtime/jobs.sqlite", "../outside.md", "../../auth.json"]:
            with self.assertRaises(HTTPError):
                self.get("/api/file?project=sample&path=" + path)

    def test_server_renders_all_views_and_escapes_model_text_before_javascript(self):
        empty = self.html("/")
        self.assertIn("创建你的第一个项目", empty)
        self.assertIn('action="/action"', empty)
        self.post(action="demo", project="sample")
        initial = self.html("/?project=sample&view=projects")
        self.assertIn("剧本已锁定", initial)
        self.assertIn("首轮保留率：未报告", initial)
        self.assertNotIn("<!--SFL:", initial)
        self.post(action="run", project="sample")
        self.wait_idle()
        store = self.server.app.store("sample")
        injected = '<img src="x" onerror="alert(1)"> <!--SFL:TITLE-->'
        store.write("style.md", store.text("style.md").replace("<!-- ART_PROMPT_BEGIN -->", "<!-- ART_PROMPT_BEGIN -->\n" + injected))
        inbox = self.html("/?project=sample&view=inbox")
        self.assertIn("妖商店", inbox)
        self.assertIn("腕镣", inbox)
        self.assertIn("铜钥匙", inbox)
        self.assertNotIn('<img src="x"', inbox)
        self.assertIn(escape(injected.split(" <!--")[0], quote=True), inbox)
        episode = self.html("/?project=sample&view=episode&episode=ep01")
        self.assertIn("本集资产", episode)
        self.assertIn("空店，木柜台", episode)
        settings = self.html("/?project=sample&view=settings")
        self.assertIn("gpt-6-sol", settings)
        self.assertIn('value="high" selected', settings)
        fragment = self.get("/api/view?project=sample&view=episode&episode=ep01")
        self.assertIn("本集资产", fragment["content"])
        self.assertIn("道具 · 2", fragment["content"])
        with self.assertRaises(HTTPError):
            self.html("/?project=missing&view=inbox")
        self.assertFalse((Path(self.tmp.name) / "missing").exists())

    def test_native_forms_complete_episode_and_preserve_optional_numeric_feedback(self):
        created = self.submit(action="demo", project="sample", _view="projects")
        self.assertIn("sample · 演示", created)
        self.submit(action="run", project="sample", _view="inbox")
        self.wait_idle()
        card = next(i["card"] for i in self.get("/api/inbox?project=sample")["items"] if i["type"] == "card")
        self.submit(action="answer", project="sample", card=card["id"], option="approve", note="测试批准", _view="inbox")
        self.submit(action="run", project="sample", _view="episode", _episode="ep01")
        self.wait_idle()
        html = self.html("/?project=sample&view=episode&episode=ep01")
        self.assertIn("下载交付包", html)
        self.assertIn("复制提示词", html)
        self.assertIn('name="placeholder"', html)
        self.submit(action="feedback", project="sample", unit="ep01_u01", result="redo", generations="2.0", user_minutes="3.5", note="保留原始备注", _view="episode", _episode="ep01")
        store = self.server.app.store("sample")
        report = store.logs("feedback")[-1]
        self.assertEqual(report["generations"], 2)
        self.assertIs(type(report["generations"]), int)
        self.assertEqual(report["user_minutes"], 3.5)
        self.assertEqual(report["note"], "保留原始备注")
        self.submit(action="feedback", project="sample", unit="ep01_u01", result="ok", generations="", user_minutes="", _view="episode", _episode="ep01")
        self.assertNotIn("generations", store.logs("feedback")[-1])
        self.submit(action="finish", project="sample", episode="ep01", user_minutes="", _view="episode", _episode="ep01")
        self.assertEqual(store.logs("feedback")[-1]["result"], "finished")
        self.assertNotIn("user_minutes", store.logs("feedback")[-1])
        self.submit(action="refs", project="sample", unit="ep01_u02", operation="remove", placeholder="@妖商_母图", _view="episode", _episode="ep01")
        changed = self.html("/?project=sample&view=episode&episode=ep01")
        self.assertIn("待更新", changed)
        self.assertNotIn(">下载交付包<", changed)

    def test_native_forms_reject_cross_origin_and_nonfinite_feedback(self):
        request = Request(self.base + "/action", data=b"action=demo&project=wrong", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://unrelated.example"})
        with self.assertRaises(HTTPError):
            urlopen(request)
        self.assertFalse((Path(self.tmp.name) / "wrong").exists())
        with self.assertRaises(HTTPError):
            self.submit(action="feedback", project="missing", unit="ep01_u01", result="ok", user_minutes="nan")
        self.assertFalse((Path(self.tmp.name) / "missing").exists())


if __name__ == "__main__":
    unittest.main()
