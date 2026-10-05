from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.support import RESPONSES, project
from storyforge.checks import references, rules
from storyforge.config import SflError, configuration, model_card
from storyforge.refs import edit, mapping
from storyforge.store import Store


class ReferenceEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-refs-")
        self.addCleanup(self.temporary.cleanup)
        self.store = project(Path(self.temporary.name) / "novel")
        self.config = configuration(self.store.root)
        self.board = deepcopy(RESPONSES["storyboard:ep01"])
        rows = [{**a, "id": rules()["asset_id_prefixes"][a["type"]] + ":" + a["name"] + ("@" + a["variant"] if a["variant"] else ""),
                 "status": "approved", "image_prompt": "测试资产描述"}
                for a in RESPONSES["asset_extract:ep01"]["assets"]]
        self.store.write("assets.csv", Store.assets_text(rows))
        self.store.write("storyboard/ep01.json", self.board, json_data=True)

    def placeholders(self, unit):
        return {r["placeholder"] for r in mapping(self.store, unit, self.config)}

    def test_user_add_overrides_storyboard_reference_exclusion(self):
        unit = self.board["units"][1]
        unit["reference_exclusions"] = [{"placeholder":"@妖商_母图", "reason":"预留引用位置"}]
        self.assertNotIn("@妖商_母图", self.placeholders(unit))
        edit(self.store, unit["id"], "add", "@妖商_母图")
        self.assertIn("@妖商_母图", self.placeholders(unit))
        edit(self.store, unit["id"], "remove", "@妖商_母图")
        self.assertNotIn("@妖商_母图", self.placeholders(unit))

    def test_user_can_add_tail_frame_without_a_storyboard_chain(self):
        unit = self.board["units"][2]
        self.assertFalse(unit["chain_from_previous"])
        before = self.store.assets()
        edit(self.store, unit["id"], "add", "@上段尾帧")
        refs = mapping(self.store, unit, self.config)
        tail = next(r for r in refs if r["placeholder"] == "@上段尾帧")
        self.assertEqual(tail["asset_id"], "external:previous_frame")
        self.assertEqual(tail["category"], "images")
        self.assertEqual(tail["description"], unit["carry_in"])
        self.assertEqual(self.store.assets(), before)
        self.assertEqual(sum(r["placeholder"] == "@上段尾帧" for r in refs), 1)

    def test_tail_frame_obeys_explicit_storyboard_exclusion_and_user_override(self):
        unit = self.board["units"][1]
        unit["reference_exclusions"] = [{"placeholder":"@上段尾帧", "reason":"改用当前单元的角色和场景参考"}]
        self.assertNotIn("@上段尾帧", self.placeholders(unit))
        edit(self.store, unit["id"], "add", "@上段尾帧")
        self.assertIn("@上段尾帧", self.placeholders(unit))

    def test_user_remove_then_add_tail_does_not_rewrite_canon(self):
        unit = self.board["units"][1]
        original = deepcopy(unit)
        edit(self.store, unit["id"], "remove", "@上段尾帧")
        self.assertNotIn("@上段尾帧", self.placeholders(unit))
        edit(self.store, unit["id"], "add", "@上段尾帧")
        self.assertIn("@上段尾帧", self.placeholders(unit))
        self.assertEqual(unit, original)
        self.assertEqual(self.store.json("storyboard/ep01.json"), self.board)
        changes = self.store.json(f".state/ref_edits/{unit['id']}.json")
        self.assertEqual(changes, {"add":["@上段尾帧"], "remove":[]})

    def test_layout_master_and_child_cannot_both_be_references(self):
        unit = self.board["units"][2]
        master = {"id":"layout:站位", "type":"layout", "name":"站位", "parent":"", "what_changed":"",
                  "placeholder":"@站位_母图", "description":"固定站位", "image_prompt":"测试", "status":"approved"}
        child = {**master, "id":"layout:站位@夜", "parent":master["id"], "what_changed":"夜景照明", "placeholder":"@站位_夜"}
        self.store.write("assets.csv", Store.assets_text(self.store.assets() + [master,child]))
        unit["assets"].append(master["id"])
        edit(self.store, unit["id"], "add", child["placeholder"])
        with self.assertRaisesRegex(SflError, "both master and child"):
            mapping(self.store, unit, self.config)

    def test_offscreen_character_and_system_voice_keep_distinct_roles(self):
        refs = mapping(self.store, self.board["units"][1], self.config)
        merchant = next(r for r in refs if r["placeholder"] == "@妖商_母图")
        self.assertEqual(merchant["position"], "画外，身份参考")
        first = mapping(self.store, self.board["units"][0], self.config)
        voice = next(r for r in first if r["placeholder"] == "@系统_声音")
        self.assertEqual((voice["type"], voice["category"]), ("voice", "audio"))
        self.assertIn("@道具_腕镣", {r["placeholder"] for r in first})

    def test_extra_tail_frame_counts_toward_limits_and_is_never_dropped(self):
        unit = self.board["units"][2]
        before = mapping(self.store, unit, self.config)
        edit(self.store, unit["id"], "add", "@上段尾帧")
        refs = mapping(self.store, unit, self.config)
        self.assertEqual(len(refs), len(before) + 1)
        card = model_card(self.config)
        card["refs"]["images"] = sum(r["category"] == "images" for r in before)
        self.assertTrue(references(refs, card))
        self.assertIn("@上段尾帧", {r["placeholder"] for r in refs})
        self.assertEqual(self.store.logs("decisions")[-1]["choice"], "add")


if __name__ == "__main__":
    unittest.main()
