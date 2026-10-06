from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.support import ROOT, RESPONSES, project, scripted_runner, approve_look, outfit_child
from storyforge.config import configuration, model_card
from storyforge.checks import storyboard, references, prompt
from storyforge.checks.parsers import parse_script, parse_bible
from storyforge.delivery import export
from storyforge.delivery.prompts import style_parts, unit_label
from storyforge.refs import mapping
from storyforge.runner import Runner, StageBlocked
from storyforge.runner.director import Director
from storyforge.runner.service import feedback, metrics


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-test-")
        self.addCleanup(self.temporary.cleanup)
        self.store = project(Path(self.temporary.name) / "novel")

    def test_golden_delivery_stops_for_approval_then_completes(self):
        runner = Runner(self.store)
        approve_look(runner)
        report = runner.run()
        self.assertFalse(report["waiting"], report)
        self.assertFalse(report["paused"], report)
        manifest = self.store.json("delivery/ep01/manifest.json")
        self.assertTrue(manifest["demo"])
        self.assertEqual(len(manifest["units"]), 3)
        self.assertEqual(sum(u["seconds"] for u in manifest["units"]), 30)
        self.assertTrue(any(w["kind"] == "episode_runtime" and w["target"] == "ep01" for w in manifest["warnings"]))
        for unit in manifest["units"]:
            path = f"delivery/ep01/{unit['id'].split('_')[1]}.md"
            text = self.store.text(path)
            self.assertIn("16:9，电影感三维国漫", text)
            self.assertEqual(text.count("约束："), 1)
            self.assertIn("生成总时长：", text)
            self.assertIn("表演重点：", text)
            self.assertIn("环境与光线：", text)
            self.assertIn("空间布局：", text)
            self.assertIn("参考职责（由用户配图）：", text)
            self.assertIn("镜头1｜约", text)
            self.assertIn("声音：", text)
            self.assertFalse(prompt(text, unit["mapping"]))
        art = style_parts(self.store.text("style.md"))
        asset_prompts = self.store.text("delivery/ep01/assets.md")
        self.assertIn(art["art_prompt"], asset_prompts)
        self.assertIn(art["style_lock"], asset_prompts)
        for row in self.store.assets():
            if row["type"] == "character" and not row["parent"]:
                self.assertIn(row["image_prompt"], asset_prompts)
        self.assertTrue(self.store.path("delivery/ep01/assets.md").exists())
        self.assertTrue(self.store.path("delivery/ep01/overlays.md").exists())
        first = manifest["units"][0]
        first_ids = {r["asset_id"] for r in first["mapping"]}
        self.assertIn("char:云清禾", first_ids)
        self.assertIn("prop:腕镣", first_ids)
        self.assertFalse(any(a.startswith("char:云清禾@") for a in first_ids))
        self.assertEqual(sum(r["asset_id"]=="prop:腕镣" for r in first["mapping"]), 1)
        opening = self.store.text("delivery/ep01/u01.md")
        self.assertIn("已扣在她左右手腕", opening)
        self.assertIn("刚从双腕取下的同一副", opening)
        self.assertIn("不因人物与道具两张参考多出备用腕镣", opening)
        self.assertNotIn("@云清禾_戴镣", opening)
        # Complete-stage snapshots are real Git commits, not a mocked history.
        self.assertGreater(len(self.store.git("rev-list", "HEAD").splitlines()), 10)
        feedback(self.store, "ep01_u01", "redo", "声音错误")
        feedback(self.store, "ep01_u01", "ok", "重做后成功")
        self.assertIsNone(metrics(self.store)["first_pass_usable_rate"])

    def test_visible_unit_numbers_restart_for_each_scene(self):
        units = [{"id": "ep01_u01", "scene": "S01"}, {"id": "ep01_u02", "scene": "S01"},
                 {"id": "ep01_u03", "scene": "S02"}, {"id": "ep01_u04", "scene": "S02"}]
        self.assertEqual([unit_label(u, units) for u in units], ["SC01-U01", "SC01-U02", "SC02-U01", "SC02-U02"])

    def test_resume_reuses_calls_and_rejects_changed_prompt_export(self):
        runner = Runner(self.store)
        approve_look(runner)
        self.assertFalse(runner.run()["waiting"])
        before = len([r for r in self.store.logs("calls") if not r["cached"]])
        self.assertFalse(runner.run()["waiting"])
        after = len([r for r in self.store.logs("calls") if not r["cached"]])
        self.assertEqual(before, after)
        self.store.write("prompts/ep01/u01.md", self.store.text("prompts/ep01/u01.md") + "\n修改")
        with self.assertRaisesRegex(Exception, "current text-only reconstruction"):
            export(self.store, "ep01", runner.card)

    def test_injected_writer_and_reviewer_failures_are_repaired(self):
        cases = ["missing_cast", "standing_to_seated", "pipeline_jargon", "offscreen_drawn_into_frame", "system_as_scene_voice", "unmapped_placeholder", "duplicate_worn_prop"]
        for name in cases:
            with self.subTest(case=name):
                store = project(Path(self.temporary.name) / name)
                case = json.loads((ROOT / "tests/fixtures/failure_cases" / name / "case.json").read_text(encoding="utf-8"))
                runner, transport = scripted_runner(store, case)
                approve_look(runner)
                report = runner.run()
                self.assertFalse(report["waiting"], report)
                self.assertFalse(report["paused"], report)
                self.assertGreater(transport.counts[case["writer_key"]], 1)
                relevant = [repairs for role, target, data, repairs in transport.packets if role + ":" + target == case["writer_key"]]
                self.assertTrue(any(r.get("hard_errors") or r.get("findings") for r in relevant))
                if case.get("reviewer"):
                    logs = list((store.root / "findings").rglob("*.json"))
                    self.assertTrue(any(case["finding"]["problem"] in p.read_text(encoding="utf-8") for p in logs))

    def test_resting_prop_is_kept_and_over_limit_becomes_card(self):
        runner, transport = scripted_runner(self.store)
        approve_look(runner)
        self.assertFalse(runner.run(until="B5")["waiting"])
        case = json.loads((ROOT / "tests/fixtures/failure_cases/resting_prop_over_limit/case.json").read_text(encoding="utf-8"))
        unit = self.store.json("storyboard/ep01.json")["units"][0]
        refs = mapping(self.store, unit, configuration(self.store.root))
        card = deepcopy(runner.card)
        card["refs"]["images"] = case["images_limit"]
        self.assertIn(case["must_keep"], {r["placeholder"] for r in refs})
        self.assertTrue(references(refs, card))
        runner.card = card
        report = runner.run(until="B6")
        self.assertTrue(report["waiting"])
        self.assertTrue(any(c["stage"] == "B6" for c in runner.cards.list()))
        self.assertFalse(self.store.path("delivery/ep01/manifest.json").exists())
        decision = next(c for c in runner.cards.list() if c["stage"] == "B6")
        runner.cards.answer(decision["id"], "retry", "已确认账号允许 9 张图片参考")
        runner.card = deepcopy(model_card(configuration()))
        self.assertFalse(runner.run(until="B6")["waiting"])
        self.assertTrue(any(p[3].get("user_note", {}).get("note") == "已确认账号允许 9 张图片参考" for p in transport.packets if p[0] == "storyboard"))

    def test_later_ledger_never_enters_packets_and_does_not_stale_episode_one(self):
        runner, transport = scripted_runner(self.store)
        case = json.loads((ROOT / "tests/fixtures/failure_cases/later_ledger_leak/case.json").read_text(encoding="utf-8"))
        self.store.write(case["later_file"], case["later_text"])
        approve_look(runner)
        report = runner.run()
        self.assertFalse(report["waiting"])
        for role, target, data, repairs in transport.packets:
            self.assertNotIn(case["forbidden_fragment"], json.dumps(data, ensure_ascii=False))
        self.store.write(case["later_file"], "完全不同的后续状态")
        self.assertNotIn("storyboard/ep01.json", self.store.stale_artifacts())

    def test_legitimate_exit_is_valid_and_blind_packets_are_independent(self):
        runner, transport = scripted_runner(self.store)
        approve_look(runner)
        self.assertFalse(runner.run()["waiting"])
        board = self.store.json("storyboard/ep01.json")
        self.assertEqual(set(board["units"][2]["absent"]), {"林恒", "云清禾"})
        self.assertFalse(storyboard(board, parse_script(self.store.text("episodes/ep01.md")), parse_bible(self.store.text("bible.md")), self.store.assets(), runner.card))
        blind = [data for role, target, data, repairs in transport.packets if role == "reconstruction_blind"]
        self.assertEqual(len(blind), 3)
        self.assertTrue(all(set(data) == {"prompt", "reference_descriptions", "model_card", "warnings"} for data in blind))
        self.assertFalse(any("writer_reasoning" in data or "ledger_out" in data for role, target, data, repairs in transport.packets))

    def test_rejected_look_regenerates_asset_briefs_under_the_new_style(self):
        runner, transport = scripted_runner(self.store)
        original = runner.client.codex_transport
        def revised(profile, packet, schema, **kwargs):
            result = original(profile, packet, schema, **kwargs)
            value = json.loads(result["text"])
            if packet.role == "art_director" and packet.repairs.get("user_note"):
                value["style_lock"] += "；冷色环境光"
            if packet.role == "asset_prompt" and "冷色环境光" in packet.data["style"]:
                value["brief"] += "。使用冷色环境光。"
            result["text"] = json.dumps(value, ensure_ascii=False)
            return result
        runner.client.codex_transport = revised
        self.assertTrue(runner.run()["waiting"])
        first = runner.cards.list()[0]
        runner.cards.answer(first["id"], "reject", "改为冷色环境光")
        self.assertTrue(runner.run()["waiting"])
        replacement = runner.cards.list()[0]
        self.assertNotEqual(first["id"], replacement["id"])
        self.assertTrue(all("冷色环境光" in a["image_prompt"] for a in self.store.assets()))
        self.assertEqual(sum(n for key,n in transport.counts.items() if key.startswith("asset_prompt:")), 20)
        runner.cards.answer(replacement["id"], "approve")
        self.assertFalse(runner.run(until="B4")["waiting"])
        before = sum(transport.counts.values())
        self.assertFalse(runner.run(until="B4")["waiting"])
        self.assertEqual(sum(transport.counts.values()), before)

    def test_child_brief_receives_a_finished_master_even_if_extracted_first(self):
        rows = deepcopy(RESPONSES["asset_extract:ep01"]["assets"])
        child_asset, child_response = outfit_child()
        rows.append(child_asset)
        rows.sort(key=lambda a: not bool(a["parent"]))
        self.assertTrue(rows[0]["parent"])
        runner, transport = scripted_runner(self.store, {"writer_key": "asset_extract:ep01",
            "mutations": [{"path": ["assets"], "value": rows}]})
        with patch.dict(RESPONSES, {"asset_prompt:ep01:asset:char:云清禾@换装":child_response}):
            self.assertTrue(runner.run(until="B4")["waiting"])
        briefs = [(target, data) for role, target, data, repairs in transport.packets if role == "asset_prompt"]
        child_target = "ep01:asset:char:云清禾@换装"
        parent_target = "ep01:asset:char:云清禾"
        child = next(data for target, data in briefs if target == child_target)
        self.assertEqual(child["parent"]["image_prompt"], RESPONSES["asset_prompt:" + parent_target]["brief"])
        self.assertLess([target for target, data in briefs].index(parent_target), [target for target, data in briefs].index(child_target))

    def test_unfulfilled_asset_request_cannot_pass_extraction(self):
        runner, transport = scripted_runner(self.store)
        approve_look(runner)
        self.store.write(".state/asset_requests/ep01.json", ["layout:站位"], json_data=True)
        with self.assertRaises(StageBlocked):
            Director(runner, 1).asset_extract("B2", force=True)
        card = next(c for c in runner.cards.list() if c["stage"] == "B2")
        self.assertTrue(any("layout:站位" in e for e in card["details"]["errors"]))

    def test_requested_asset_gets_a_brief_without_rebuilding_existing_assets(self):
        runner, transport = scripted_runner(self.store)
        approve_look(runner)
        self.assertFalse(runner.run(until="B4")["waiting"])
        original = runner.client.codex_transport
        requested = {"type":"layout", "name":"站位", "variant":"", "parent":"", "what_changed":"",
                     "placeholder":"@妖商店_站位", "description":"妖商在柜台后，林恒画面左，云清禾画面右", "identity_notes":""}
        new_target = "ep01:asset:layout:站位"
        def addition(profile, packet, schema, **kwargs):
            if packet.role == "asset_prompt" and kwargs["target"] == new_target:
                return {"text": json.dumps({"brief":"横屏站位参考，角色身份沿用母图。", "responses":[]}, ensure_ascii=False),
                        "usage":{"input_tokens":10,"output_tokens":10}, "error":None}
            result = original(profile, packet, schema, **kwargs)
            if packet.role == "asset_extract":
                value = json.loads(result["text"])
                value["assets"].append(requested)
                result["text"] = json.dumps(value, ensure_ascii=False)
            return result
        runner.client.codex_transport = addition
        before = sum(n for key,n in transport.counts.items() if key.startswith("asset_prompt:"))
        self.store.write(".state/asset_requests/ep01.json", ["layout:站位"], json_data=True)
        director = Director(runner, 1)
        director.asset_extract("B2", force=True)
        director.asset_briefs("B3")
        row = next(a for a in self.store.assets() if a["id"] == "layout:站位")
        self.assertEqual(row["status"], "approved")
        self.assertIn("横屏站位参考", row["image_prompt"])
        self.assertEqual(sum(n for key,n in transport.counts.items() if key.startswith("asset_prompt:")), before)


if __name__ == "__main__":
    unittest.main()
