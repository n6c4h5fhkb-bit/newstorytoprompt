from copy import deepcopy
import math
import unittest

from tests.support import FIXTURE, RESPONSES, patch
from storyforge.checks import asset_rows, script_names, storyboard, prompt, references, warnings, rules
from storyforge.checks.parsers import parse_bible, parse_script, complete_voice_cast
from storyforge.checks.schema import validate, schema_for
from storyforge.config import configuration, model_card


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.script = parse_script((FIXTURE / "ep01.md").read_text(encoding="utf-8"))
        self.bible = parse_bible((FIXTURE / "bible.md").read_text(encoding="utf-8"))
        self.config = configuration()
        self.card = model_card(self.config)

    def test_screenplay_line_kinds_and_silent_cast(self):
        self.assertIn("妖商", self.script.scenes[0].cast)
        kinds = {line["kind"] for line in self.script.scenes[0].lines}
        self.assertTrue({"system", "thought", "speech", "action", "sfx", "ambience"} <= kinds)
        self.assertFalse(script_names(self.script, self.bible))
        bad = (FIXTURE / "ep01.md").read_text(encoding="utf-8").replace("出场：林恒、云清禾、妖商", "出场：云清禾、妖商")
        with self.assertRaisesRegex(Exception, "missing from cast"):
            parse_script(bad)

    def test_missing_cast_format_and_unknown_names(self):
        text = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
        with self.assertRaisesRegex(Exception, "missing or duplicate cast"):
            parse_script(text.replace("出场：林恒、云清禾、妖商", ""))
        unknown = parse_script(text.replace("妖商", "陌生人"))
        self.assertTrue(script_names(unknown, self.bible))
        self.assertIn("系统", {line.get("who") for line in self.script.scenes[0].lines})

    def test_staging_notes_keep_speaker_identity_and_offscreen_kind(self):
        text = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
        annotated = text.replace("出场：林恒、云清禾、妖商", "出场：林恒（门外）、云清禾（屋内，未露面）、妖商")
        annotated = annotated.replace("林恒：打开它。", "林恒（画外，门外）：打开它。")
        parsed = parse_script(annotated)
        scene = parsed.scenes[0]
        self.assertEqual(scene.cast, ["林恒", "云清禾", "妖商"])
        self.assertEqual(scene.cast_notes["云清禾"], "屋内，未露面")
        line = next(line for line in scene.lines if line.get("who") == "林恒")
        self.assertEqual(line, {"who":"林恒", "kind":"offscreen", "line":"打开它。", "delivery_notes":"门外"})
        self.assertIn("林恒（画外，门外）", scene.text)
        self.assertFalse(script_names(parsed, self.bible))
        for invalid in (
            annotated.replace("林恒（画外，门外）", "林恒（画外，心声）"),
            annotated.replace("出场：林恒（门外）", "出场：林恒（门外）、林恒（屋内）"),
            annotated.replace("出场：林恒（门外）、", "出场："),
            annotated.replace("系统：", "系统（画外，屋内）："),
        ):
            with self.subTest(text=invalid), self.assertRaises(Exception):
                parse_script(invalid)

    def test_registered_voice_cast_repair_does_not_invent_or_hide_people(self):
        text = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
        text = text.replace("林恒：打开它。", "议价声（画外，门外）：三十文。")
        bible = deepcopy(self.bible)
        bible["Voices"]["议价声"] = {"speaker":"议价声", "voice profile":"未具名的交易声音"}
        fixed, repairs = complete_voice_cast(text, bible)
        self.assertEqual(repairs, [{"scene":"S01", "voice_sources":["议价声"]}])
        self.assertIn("议价声", parse_script(fixed).scenes[0].cast)
        self.assertEqual(fixed.replace("、议价声", ""), text)
        self.assertEqual(complete_voice_cast(fixed, bible), (fixed, []))
        # A character with a voice profile still needs an explicit cast entry.
        person = text.replace("议价声（画外，门外）", "林恒（画外，门外）").replace("出场：林恒、", "出场：")
        bible["Voices"]["林恒"] = {"speaker":"林恒", "voice profile":"男声"}
        self.assertEqual(complete_voice_cast(person, bible), (person, []))
        with self.assertRaisesRegex(Exception, "missing from cast"):
            parse_script(person)
        unknown = text.replace("议价声", "未登记声音")
        self.assertEqual(complete_voice_cast(unknown, bible), (unknown, []))

    def test_schema_rejects_unknown_fields_types_nonfinite_and_duplicate_lists(self):
        schema = schema_for("style")
        value = deepcopy(RESPONSES["art_director:ep01"])
        value["sample_frames"] = []
        self.assertTrue(validate(value, schema))
        self.assertTrue(validate(True, {"type": "number"}))
        self.assertTrue(validate(float("nan"), {"type": "number"}))
        self.assertTrue(validate(["x", "x"], {"type": "array", "items": {"type": "string"}, "uniqueItems": True}))

    def test_placeholder_sync_and_jargon_are_hard_checks(self):
        refs = [{"placeholder": "@甲_母图"}, {"placeholder": "@甲_声音"}]
        self.assertTrue(prompt("@甲_母图 @未映射_图", refs))
        self.assertFalse(prompt("@甲_母图 @甲_声音", refs))
        self.assertTrue(prompt("@甲_母图 @甲_声音 空间锚：木门在左后方", refs))
        self.assertFalse(prompt("@甲_母图 @甲_声音 空间布局：木门在左后方", refs))
        self.assertTrue(prompt("@甲_母图 @甲_声音 桌端通路", refs))

    def test_reference_sync_ignores_the_code_written_manifest(self):
        refs = [{"placeholder": "@甲_母图"}, {"placeholder": "@甲_声音"}]
        manifest = "@甲_母图：甲，只定身份；年轻男子；画内。\n@甲_声音：甲，只定音色；低沉男声；参考。\n"
        # Named only in the manifest: the model's own text never uses the references.
        errors = prompt(manifest + "镜头1｜约5秒\n他走向门口。", refs)
        self.assertIn("never used", "\n".join(errors))
        self.assertFalse(prompt(manifest + "镜头1｜约5秒\n@甲_母图 走向门口。\n声音：@甲_声音 低声说话。", refs))

    def test_negatives_inside_shots_warn_but_final_section_is_free(self):
        board = deepcopy(RESPONSES["storyboard:ep01"])
        uid = board["units"][0]["id"]
        def kinds(text):
            return {w["kind"] for w in warnings(board, self.config, {uid: text}) if w["target"] == uid}
        self.assertIn("negatives_in_shots", kinds("镜头1｜约5秒\n不要出现字幕。\n约束：不加水印"))
        self.assertNotIn("negatives_in_shots", kinds("镜头1｜约5秒\n她走向门口。\n约束：不要出现字幕，禁止多余人物"))
        self.assertNotIn("negatives_in_shots", kinds("人物姿势与取景按镜头，避免拼版。\n镜头1｜约5秒\n她走向门口。\n约束：无"))

    def test_storyboard_units_must_carry_a_performance_split(self):
        board = deepcopy(RESPONSES["storyboard:ep01"])
        self.assertFalse(validate(board, schema_for("storyboard")))
        del board["units"][0]["performance"]
        self.assertTrue(validate(board, schema_for("storyboard")))
        board["units"][0]["performance"] = ""
        self.assertTrue(validate(board, schema_for("storyboard")))

    def test_reference_limits_and_approval_are_hard_checks(self):
        refs = [{"placeholder": f"@图_{i}", "category": "images", "status": "approved", "description": "测试描述"} for i in range(10)]
        self.assertTrue(references(refs, self.card))
        refs = refs[:9]
        self.assertFalse(references(refs, self.card))
        refs[0]["status"] = "described"
        self.assertTrue(references(refs, self.card))

    def test_length_and_speech_estimates_warn_without_rewriting(self):
        board = deepcopy(RESPONSES["storyboard:ep01"])
        before = deepcopy(board)
        result = warnings(board, self.config, {"ep01_u01": "近侧 " + "长" * 3600})
        self.assertEqual(board, before)
        self.assertTrue({"episode_runtime", "prompt_length", "ambiguous_position"} <= {w["kind"] for w in result})

    def test_asset_names_parent_identity_and_placeholder_uniqueness(self):
        rows = [{"id":"char:云清禾","type":"character","name":"云清禾","parent":"","what_changed":"","image_prompt":"测试","placeholder":"@云清禾_母图","status":"approved","description":"测试"}]
        self.assertFalse(asset_rows(rows, self.bible))
        child = {**rows[0], "id": "char:云清禾@受伤", "parent": "missing", "what_changed": "左额受伤"}
        self.assertTrue(asset_rows(rows + [child], self.bible))

    def test_a_voice_only_speaker_in_the_cast_needs_no_character_asset(self):
        text = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
        text = text.replace("林恒：打开它。", "议价声（画外，门外）：三十文。")
        bible = deepcopy(self.bible)
        bible["Voices"]["议价声"] = {"speaker": "议价声", "voice profile": "未具名的交易声音"}
        script = parse_script(complete_voice_cast(text, bible)[0])
        board = deepcopy(RESPONSES["storyboard:ep01"])
        assets = [{**a, "id": rules()["asset_id_prefixes"][a["type"]] + ":" + a["name"] + ("@" + a["variant"] if a["variant"] else ""),
                   "status": "approved", "image_prompt": "测试描述"} for a in RESPONSES["asset_extract:ep01"]["assets"]]
        for each in board["units"]:
            each["offscreen"] = sorted(set(each["offscreen"]) | {"议价声"})
        unit = board["units"][0]
        errors = "\n".join(storyboard(board, script, bible, assets, self.card))
        self.assertNotIn("议价声", errors)
        self.assertNotIn("present cast missing", errors)
        # A person who appears in a shot still needs an asset; one kept off-screen does not.
        unit["assets"] = [a for a in unit["assets"] if a != "char:妖商"]
        unit["offscreen"] = sorted(set(unit["offscreen"]) | {"妖商"})
        for shot in unit["shots"]:
            shot["on_screen"] = [n for n in shot["on_screen"] if n != "妖商"]
        self.assertNotIn("present cast missing", "\n".join(storyboard(board, script, bible, assets, self.card)))
        unit["shots"][0]["on_screen"] = sorted(set(unit["shots"][0]["on_screen"]) | {"妖商"})
        unit["offscreen"] = [n for n in unit["offscreen"] if n != "妖商"]
        self.assertIn("present cast missing", "\n".join(storyboard(board, script, bible, assets, self.card)))

    def test_runtime_is_estimated_from_the_script_not_trusted_from_the_model(self):
        from storyforge.checks.writer import script_seconds, warnings as script_warnings
        text = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
        seconds = script_seconds(text, self.config)
        parsed = parse_script(text)
        dialogue = sum(len(l["line"]) for s in parsed.scenes for l in s.lines if "who" in l)
        other = sum(1 for s in parsed.scenes for l in s.lines if "who" not in l)
        self.assertEqual(seconds, round(dialogue / self.config["speech_rate_chars_per_sec"] + other * self.config["action_seconds"]))
        self.assertIn("runtime", {w["kind"] for w in script_warnings(seconds, self.config, "ep01")})
        self.assertFalse([w for w in script_warnings(200, self.config, "ep01") if w["kind"] == "runtime"])

    def test_beat_sheet_must_show_a_rising_ladder_a_burst_a_payoff_and_a_hook(self):
        from storyforge.checks.writer import rhythm_errors, rhythm_warnings
        from tests.support_writer import BEATS
        rules_ = rules()["rhythm"]
        self.assertFalse(rhythm_errors(deepcopy(BEATS), self.script, rules_))
        def broken(change):
            beats = deepcopy(BEATS)
            change(beats)
            return "\n".join(rhythm_errors(beats, self.script, rules_))
        self.assertIn("at least", broken(lambda b: b.__delitem__(slice(3, None))))
        self.assertIn("closing hook", broken(lambda b: b[-1].update(kind="turn")))
        self.assertIn("burst beat", broken(lambda b: b[2].update(kind="turn")))
        self.assertIn("pressure beats", broken(lambda b: b[0].update(kind="turn")))
        self.assertIn("must rise", broken(lambda b: b[1].update(intensity=2)))
        self.assertIn("unknown scene", broken(lambda b: b[0].update(scene="S09")))
        self.assertIn("backwards", broken(lambda b: b[1].update(at_seconds=1)))
        self.assertIn("payoff or consequence", broken(lambda b: b.__delitem__(slice(3, 5))))
        slow = deepcopy(BEATS)
        slow[0]["at_seconds"] = 20
        slow[1]["at_seconds"] = 80
        kinds = {w["kind"] for w in rhythm_warnings(slow, rules_, "ep01")}
        self.assertEqual(kinds, {"rhythm"})
        self.assertEqual(len(rhythm_warnings(slow, rules_, "ep01")), 4)

    def test_unit_pace_limits_and_speech_fit_are_hard_checks(self):
        board = deepcopy(RESPONSES["storyboard:ep01"])
        assets = [{**a, "id": rules()["asset_id_prefixes"][a["type"]] + ":" + a["name"] + ("@" + a["variant"] if a["variant"] else ""),
                   "status": "approved", "image_prompt": "测试描述"} for a in RESPONSES["asset_extract:ep01"]["assets"]]
        rate = self.config["speech_rate_chars_per_sec"]
        self.assertFalse(storyboard(board, self.script, self.bible, assets, self.card, speech_rate=rate))
        crowded = deepcopy(board)
        first = crowded["units"][0]
        shot = first["shots"][0]
        first["shots"] = [{**deepcopy(shot), "id": f"s{i}", "seconds": first["seconds"] / 5} for i in range(1, 6)]
        errors = "\n".join(storyboard(crowded, self.script, self.bible, assets, self.card, speech_rate=rate))
        self.assertIn("5 shots in one unit", errors)
        self.assertIn("minimum", errors)
        fast = deepcopy(board)
        fast["units"][0]["shots"][0]["dialogue"] = [{"who": "林恒", "kind": "speech", "line": "这是一句太长太长太长太长太长太长太长太长的台词"}]
        fast["units"][0]["shots"][0]["seconds"] = 3
        fast["units"][0]["shots"][1]["seconds"] = fast["units"][0]["seconds"] - 3
        self.assertIn("cannot be spoken", "\n".join(storyboard(fast, self.script, self.bible, assets, self.card, speech_rate=rate)))

    def test_placeholder_names_follow_the_convention(self):
        base = {"id":"char:沈砚","type":"character","name":"沈砚","parent":"","what_changed":"","image_prompt":"测试","placeholder":"@沈砚_母图","status":"approved","description":"测试"}
        bible = parse_bible("# Bible\n\n## Characters\n| name | role | look | voice | source name |\n| --- | --- | --- | --- | --- |\n| 沈砚 | 主角 | 青衫 | 冷 | 沈砚 |\n")
        self.assertFalse(asset_rows([base], bible))
        for placeholder in ("@沈砚", "@沈砚_声音", "@沈砚母图"):
            with self.subTest(placeholder=placeholder):
                self.assertIn("naming convention", "\n".join(asset_rows([{**base, "placeholder": placeholder}], bible)))
        child = {**base, "id":"char:沈砚@受伤", "parent":"char:沈砚", "what_changed":"左额受伤", "placeholder":"@沈砚_受伤"}
        self.assertFalse(asset_rows([base, child], bible))
        self.assertTrue(asset_rows([base, {**child, "placeholder":"@沈砚_母图"}], bible))
        palette = {"id":"layout:色卡","type":"layout","name":"色卡","parent":"","what_changed":"","image_prompt":"测试","placeholder":"@色卡","status":"approved","description":"测试"}
        self.assertFalse(asset_rows([base, palette], bible))

    def test_storyboard_mechanical_checks_reject_independent_corruption(self):
        board = deepcopy(RESPONSES["storyboard:ep01"])
        assets = [{**a, "id": rules()["asset_id_prefixes"][a["type"]] + ":" + a["name"] + ("@" + a["variant"] if a["variant"] else ""),
                   "status": "approved", "image_prompt": "测试描述"} for a in RESPONSES["asset_extract:ep01"]["assets"]]
        self.assertFalse(storyboard(board, self.script, self.bible, assets, self.card))
        cases = [
            (["episode"], 2, "does not match"),
            (["units",1,"id"], "ep01_u01", "duplicate unit"),
            (["units",0,"scene"], "S99", "unknown scene"),
            (["units",0,"seconds"], 20, "model maximum"),
            (["units",0,"shots"], [], "no shots"),
            (["units",0,"shots",1,"id"], board["units"][0]["shots"][0]["id"], "duplicate shot"),
            (["units",0,"shots",0,"seconds"], 1, "do not sum"),
            (["units",0,"shots",0,"on_screen"], ["陌生人"], "unknown character"),
            (["units",0,"shots",0,"on_screen"], ["系统"], "not in scene cast"),
            (["units",0,"absent"], [{"name":"林恒","reason":"离开"}], "both present and absent"),
            (["units",0,"assets"], ["char:不存在"], "unknown asset"),
            (["units",0,"assets"], [], "present cast missing"),
            (["units",0,"assets"], [a for a in board["units"][0]["assets"] if not a.startswith("loc:")], "location missing"),
            (["units",0,"shots",0,"dialogue"], [{"who":"陌生人","kind":"speech","line":"走"}], "unknown speaker"),
            (["units",0,"shots",0,"dialogue"], [{"who":"系统","kind":"speech","line":"走"}], "must be system"),
            (["units",0,"shots",0,"dialogue"], [{"who":"系统","kind":"speech","line":"走"}], "not on screen"),
            (["units",0,"shots",0,"sfx"], [{"what":"响声","at":99}], "sound time outside"),
            (["units",0,"shots",0,"overlays"], [{"text":"后期字幕","start":0,"end":99}], "overlay time outside"),
        ]
        for path, value, expected in cases:
            with self.subTest(error=expected):
                corrupt = deepcopy(board)
                patch(corrupt, {"path": path, "value": value})
                self.assertIn(expected, "\n".join(storyboard(corrupt, self.script, self.bible, assets, self.card)))


if __name__ == "__main__":
    unittest.main()
