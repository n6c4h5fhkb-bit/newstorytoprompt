"""User-requested rewrites, blind-review inputs, role labels and episode rollback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.support import project, scripted_runner, approve_look
from storyforge.checks import rules
from storyforge.config import SflError
from storyforge.runner import service
from storyforge.store import create_project


def delivered(folder):
    store = project(Path(folder) / "story")
    runner, model = scripted_runner(store)
    approve_look(runner)
    assert not runner.run()["waiting"]
    return store, runner, model


def rerun(store, runner, stage, target, note=None):
    with patch("storyforge.runner.service.Runner", return_value=runner):
        return service.rerun(store, stage, target, note)


def prompt_calls(model):
    return [(t, d, r) for role, t, d, r in model.packets if role == "unit_prompt"]


class RewriteTests(unittest.TestCase):
    def test_prompt_note_reaches_the_writer_with_the_previous_version_and_bypasses_the_cache(self):
        with tempfile.TemporaryDirectory(prefix="sfl-rewrite-") as folder:
            store, runner, model = delivered(folder)
            before = len(prompt_calls(model))
            self.assertFalse(rerun(store, runner, "B7", "ep01_u02", "镜头2改成慢推近景，其余保持")["waiting"])
            fresh = prompt_calls(model)[before:]
            self.assertEqual(len(fresh), 1)
            _, data, repairs = fresh[0]
            self.assertEqual([u["id"] for u in data["units"]], ["ep01_u02"])
            self.assertEqual(repairs["user_note"]["note"], "镜头2改成慢推近景，其余保持")
            self.assertEqual(repairs["previous_output"]["unit_id"], "ep01_u02")
            self.assertNotIn("responses", repairs["previous_output"])
            self.assertTrue(repairs["retry_id"].startswith("r_"))
            self.assertTrue(store.json(".state/revision_requests/B7_ep01_u02.json")["applied"])
            self.assertTrue(service.status(store)["progress"]["ep01"]["delivery"])
            # The request is consumed: a plain run does not rewrite again.
            count = len(model.packets)
            self.assertFalse(runner.run()["waiting"])
            self.assertEqual(len(model.packets), count)

    def test_rerun_without_a_note_still_asks_the_model_again(self):
        with tempfile.TemporaryDirectory(prefix="sfl-rewrite-") as folder:
            store, runner, model = delivered(folder)
            before = len(prompt_calls(model))
            rerun(store, runner, "B7", "ep01_u03")
            fresh = prompt_calls(model)[before:]
            self.assertEqual(len(fresh), 1)
            self.assertNotIn("user_note", fresh[0][2])
            self.assertIn("retry_id", fresh[0][2])

    def test_storyboard_note_revises_the_saved_storyboard(self):
        with tempfile.TemporaryDirectory(prefix="sfl-rewrite-") as folder:
            store, runner, model = delivered(folder)
            before = len(model.packets)
            rerun(store, runner, "B5", "ep01", "第一段拆成两个镜头")
            fresh = [(d, r) for role, t, d, r in model.packets[before:] if role == "storyboard"]
            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0][1]["user_note"]["note"], "第一段拆成两个镜头")
            self.assertEqual(fresh[0][1]["previous_output"]["episode"], 1)

    def test_notes_are_limited_to_storyboards_and_unit_prompts(self):
        with tempfile.TemporaryDirectory(prefix="sfl-rewrite-") as folder:
            store, runner, model = delivered(folder)
            with self.assertRaisesRegex(SflError, "supported for B5"):
                service.rerun(store, "B9", "ep01", "改一下")
            with self.assertRaisesRegex(SflError, "epNN_uNN"):
                service.rerun(store, "B7", "ep01", "改一下")
            with self.assertRaisesRegex(SflError, "epNN"):
                service.rerun(store, "B5", "ep01_u01", "改一下")
            self.assertFalse(store.path(".state/revision_requests").exists())


class DeliveryWordingTests(unittest.TestCase):
    def test_reference_lines_are_short_and_the_mapping_table_uses_chinese_roles(self):
        with tempfile.TemporaryDirectory(prefix="sfl-wording-") as folder:
            store, runner, model = delivered(folder)
            unit = store.text("delivery/ep01/u02.md")
            self.assertIn("成年男子，约1.78m，墨黑短碎发，深炭灰粗麻交领衣", store.text("prompts/ep01/u02.md"))
            self.assertNotIn("窄长脸、平直浓眉", store.text("prompts/ep01/u02.md"))
            # The full description stays in the mapping table, where the user makes the real asset.
            self.assertIn("窄长脸、平直浓眉", unit)
            for label in ("角色形象", "场景", "道具", "色卡", "音色"):
                self.assertIn("| " + label + " |", unit)
            for english in ("| character |", "| location |", "| prop |", "| voice |", "| layout |"):
                self.assertNotIn(english, unit)
            self.assertIn("- 类型：场景", store.text("delivery/ep01/assets.md"))
            self.assertNotIn("空间锚", store.text("prompts/ep01/u02.md"))

    def test_the_blind_reader_only_gets_warnings_visible_in_the_prompt_text(self):
        from storyforge.checks import warnings as real
        def with_shot_warning(board, config, prompts=None):
            extra = [{"kind": "speech_fit", "target": u["id"], "message": "s1: dialogue may not fit"} for u in board["units"]]
            return real(board, config, prompts) + extra
        with tempfile.TemporaryDirectory(prefix="sfl-blind-") as folder, patch("storyforge.runner.director.warnings", side_effect=with_shot_warning):
            store, runner, model = delivered(folder)
            blind = [d["warnings"] for role, t, d, r in model.packets if role == "reconstruction_blind"]
            compare = [d["warnings"] for role, t, d, r in model.packets if role == "reconstruction_compare"]
            self.assertTrue(blind)
            allowed = set(rules()["blind_warning_kinds"])
            self.assertTrue(all(w["kind"] in allowed for ws in blind for w in ws))
            self.assertTrue(any(w["kind"] == "speech_fit" for ws in compare for w in ws), "the compare call keeps storyboard-based warnings")


class OpenReviewNotesTests(unittest.TestCase):
    FINDING = {"severity": "major", "kind": "error", "location": "ep01_u02 镜头1", "evidence": "只作镜框外右侧柜台后人物的身份参考",
               "problem": "画外参考可能被画进镜框", "suggested_fix": "再强调一次镜框外"}

    def run_policy(self, policy):
        store = project(Path(self.folder) / "story")
        with open(store.path("project.yaml"), "a", encoding="utf-8") as stream:
            stream.write("fix_policy: {open_major: %s}\n" % policy)
        runner, model = scripted_runner(store, {"reviewer": "reconstruction_blind", "review_target": "ep01_u02", "finding": self.FINDING})
        approve_look(runner)
        return store, runner, runner.run()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfl-notes-")
        self.addCleanup(temporary.cleanup)
        self.folder = temporary.name

    def test_default_policy_still_stops_for_a_decision(self):
        store, runner, report = self.run_policy("card")
        self.assertTrue(report["waiting"])
        self.assertTrue(any(c["stage"] == "B8" and c["kind"] == "stuck" for c in runner.cards.list()))

    def test_accept_policy_delivers_with_the_open_major_as_a_review_note(self):
        store, runner, report = self.run_policy("accept")
        self.assertFalse(report["waiting"], report)
        self.assertFalse([c for c in runner.cards.list() if c["kind"] == "stuck"])
        unit = store.text("delivery/ep01/u02.md")
        self.assertIn("## 审查提示", unit)
        self.assertIn("画外参考可能被画进镜框", unit)
        manifest = store.json("delivery/ep01/manifest.json")
        notes = next(u for u in manifest["units"] if u["id"] == "ep01_u02")["open_review_notes"]
        self.assertEqual([n["problem"] for n in notes], ["画外参考可能被画进镜框"])
        self.assertNotIn("## 审查提示", store.text("delivery/ep01/u01.md"))


class QualityLoopTests(unittest.TestCase):
    def test_redo_reasons_are_validated_and_recorded(self):
        with tempfile.TemporaryDirectory(prefix="sfl-reasons-") as folder:
            store, runner, model = delivered(folder)
            with self.assertRaisesRegex(SflError, "Unknown redo reason"):
                service.feedback(store, "ep01_u01", "redo", "手多了一只", reasons=["finger"])
            with self.assertRaisesRegex(SflError, "only apply to a redo"):
                service.feedback(store, "ep01_u01", "ok", reasons=["prop"])
            service.feedback(store, "ep01_u01", "redo", "多出第二副腕镣", reasons=["prop", "state"])
            row = store.logs("feedback")[-1]
            self.assertEqual(row["reasons"], ["prop", "state"])

    def test_review_signal_compares_redo_rates_with_and_without_open_review_notes(self):
        from storyforge.runner.metrics import review_signal
        with tempfile.TemporaryDirectory(prefix="sfl-signal-") as folder:
            store, runner, model = delivered(folder)
            manifest = store.json("delivery/ep01/manifest.json")
            manifest["demo"] = False
            manifest["units"][0]["open_review_notes"] = [{"location": "镜头1", "problem": "x"}]
            manifest["units"][1]["open_review_notes"] = []
            manifest["units"][2]["open_review_notes"] = []
            store.write("delivery/ep01/manifest.json", manifest, json_data=True)
            for unit, result, reasons in (("ep01_u01", "ok", []), ("ep01_u02", "redo", ["prop"]), ("ep01_u03", "ok", [])):
                store.append("feedback", {"unit": unit, "result": result, "reasons": reasons, "demo": False})
            signal = review_signal(store, minimum_units=1)
            self.assertEqual(signal["groups"]["with_open_notes"], {"units": 1, "redo": 0})
            self.assertEqual(signal["groups"]["without_open_notes"], {"units": 2, "redo": 1})
            self.assertEqual(signal["verdict"], "reviews_do_not_predict_redo")
            self.assertEqual(signal["redo_reasons"], {"prop": 1})
            self.assertEqual(review_signal(store)["verdict"], "not_enough_data")

    def test_gold_examples_regress_the_mechanical_checks(self):
        with tempfile.TemporaryDirectory(prefix="sfl-gold-") as folder:
            store, runner, model = delivered(folder)
            service.gold_add(store, "ep01_u01", "good", "站位清楚")
            service.gold_add(store, "ep01_u02", "bad", "手部多出物件")
            report = service.gold_check(store)
            self.assertEqual(report["examples"], 2)
            self.assertEqual(report["good_now_failing"], [])
            self.assertEqual(report["bad_not_mechanical"], ["ep01_u02"])
            gold = store.json("gold/ep01_u01.json")
            gold["prompt"] += "\n空间锚：木门在左\n"
            store.write("gold/ep01_u01.json", gold, json_data=True)
            report = service.gold_check(store)
            self.assertEqual([g["unit"] for g in report["good_now_failing"]], ["ep01_u01"])
            with self.assertRaisesRegex(SflError, "label good or bad"):
                service.gold_add(store, "ep01_u01", "fine")


class AdoptAndEditTests(unittest.TestCase):
    def test_an_adopted_prompt_keeps_the_users_wording_even_when_a_reviewer_objects(self):
        finding = {"severity": "major", "kind": "error", "location": "ep01_u02", "evidence": "用户加的ADOPTMARK",
                   "problem": "多出一句", "suggested_fix": "删掉"}
        case = {"reviewer": "reconstruction_blind", "review_target": "ep01_u02", "finding": finding}
        with tempfile.TemporaryDirectory(prefix="sfl-adopt-") as folder:
            store = project(Path(folder) / "story")
            runner, model = scripted_runner(store, case)
            approve_look(runner)
            self.assertFalse(runner.run()["waiting"])
            path = "prompts/ep01/u02.md"
            original = store.text(path)
            store.write(path, original.rstrip("\n") + "用户加的ADOPTMARK\n")
            self.assertFalse(service.status(store)["progress"]["ep01"]["delivery"])
            service.adopt_prompt(store, "ep01_u02")
            before = len(prompt_calls(model))
            self.assertFalse(runner.run()["waiting"])
            self.assertEqual(len(prompt_calls(model)), before, "an adopted prompt is not rewritten")
            self.assertIn("用户加的ADOPTMARK", store.text("delivery/ep01/u02.md"))
            review = store.json(".state/reviews/ep01_u02.json")
            self.assertTrue(review["adopted"] and review["findings"])
            self.assertTrue(service.status(store)["progress"]["ep01"]["delivery"])

    def test_adoption_runs_the_hard_checks_first(self):
        with tempfile.TemporaryDirectory(prefix="sfl-adopt-") as folder:
            store, runner, model = delivered(folder)
            path = "prompts/ep01/u02.md"
            original = store.text(path)
            with self.assertRaisesRegex(SflError, "nothing to adopt"):
                service.adopt_prompt(store, "ep01_u02")
            with self.assertRaisesRegex(SflError, "hard checks.*Unmapped"):
                service.adopt_prompt(store, "ep01_u02", original + "@未映射_图\n")
            self.assertEqual(store.text(path), original)
            with self.assertRaisesRegex(SflError, "hard checks.*空间锚"):
                service.adopt_prompt(store, "ep01_u02", original + "空间锚\n")
            service.adopt_prompt(store, "ep01_u02", original.rstrip("\n") + "。慢一点。\n")
            self.assertIn("慢一点", store.text(path))

    def test_editing_an_asset_marks_only_its_prompts_stale_and_keeps_a_matching_brief(self):
        with tempfile.TemporaryDirectory(prefix="sfl-asset-") as folder:
            store, runner, model = delivered(folder)
            service.edit_asset(store, "@云清禾_母图", identity_notes="成年女子，青绿眼睛，灰白破裙")
            stale = {p for p in store.stale_artifacts() if p.startswith("prompts/")}
            self.assertTrue(stale)
            briefs = lambda: sum(1 for role, *_ in model.packets if role == "asset_prompt")
            before = briefs()
            self.assertFalse(runner.run(allow_stale=["ep01"])["waiting"])
            self.assertEqual(briefs(), before)
            self.assertIn("成年女子，青绿眼睛，灰白破裙", store.text("prompts/ep01/u01.md"))
            # A new description with its own image prompt is adopted together; no brief is regenerated.
            service.edit_asset(store, "@道具_腕镣", description="一副暗哑黑铁腕镣，短链相连，扣环内侧磨亮", image_prompt="腕镣四宫格：正视、侧视、俯视、扣环近照")
            self.assertFalse(runner.run(allow_stale=["ep01"])["waiting"])
            self.assertEqual(briefs(), before)
            row = next(a for a in store.assets() if a["placeholder"] == "@道具_腕镣")
            self.assertEqual(row["image_prompt"], "腕镣四宫格：正视、侧视、俯视、扣环近照")
            self.assertIn("扣环内侧磨亮", store.text("delivery/ep01/assets.md"))
            for bad in ({}, {"description": ""}):
                with self.assertRaises(SflError):
                    service.edit_asset(store, "@道具_腕镣", **bad)
            with self.assertRaisesRegex(SflError, "Unknown asset"):
                service.edit_asset(store, "@不存在", description="x")


class PlainErrorTests(unittest.TestCase):
    def test_common_hard_errors_are_shown_in_plain_chinese(self):
        from storyforge.ui.render import plain_error
        self.assertIn("没有映射", plain_error("Unmapped placeholders: @甲_图"))
        self.assertIn("一次都没用到", plain_error("Mapped placeholders never used in the prompt text (use each): @乙_图"))
        self.assertIn("超过模型上限 9", plain_error("Reference limit: images 10 > 9; storyboarder must decide"))
        self.assertEqual(plain_error("something unrecognised"), "something unrecognised")


class DismissNoteTests(unittest.TestCase):
    def test_a_dismissed_script_note_is_no_longer_pending_or_shown_to_the_writer(self):
        with tempfile.TemporaryDirectory(prefix="sfl-dismiss-") as folder:
            store, runner, model = delivered(folder)
            record = service.note(store, "ep01", "把结尾改成别的")
            note_id = store.json("notes/script_notes.json")[0]["id"]
            self.assertEqual(record["kind"], "script")
            from storyforge.store import select_text
            text = store.text("notes/script_notes.json")
            self.assertEqual(len(select_text(text, {"kind": "script_notes", "episode": "ep01"})), 1)
            service.dismiss_script_note(store, note_id, "审查误判")
            self.assertEqual(store.json("notes/script_notes.json")[0]["status"], "dismissed")
            self.assertEqual(select_text(store.text("notes/script_notes.json"), {"kind": "script_notes", "episode": "ep01"}), [])
            with self.assertRaisesRegex(SflError, "Only a pending"):
                service.dismiss_script_note(store, note_id)
            with self.assertRaisesRegex(SflError, "Unknown script note"):
                service.dismiss_script_note(store, "n_missing")
            # The episode is not waiting for a note that no longer exists, and nothing finished became stale.
            self.assertFalse([p for p in store.stale_artifacts() if p.startswith(("episodes/", "storyboard/", "prompts/", "delivery/"))])
            self.assertFalse(runner.run()["waiting"])


class HistoryTests(unittest.TestCase):
    def test_history_lists_snapshots_that_revert_accepts(self):
        with tempfile.TemporaryDirectory(prefix="sfl-history-") as folder:
            store = create_project(Path(folder) / "p", {"name": "p", "output": {"model_card": "seedance-2.0", "ratio": "16:9", "music": "none"}})
            store.write("episodes/ep01.md", "# EP01\n旧\n")
            store.snapshot("old")
            store.write("episodes/ep01.md", "# EP01\n新\n")
            store.snapshot("new")
            rows = service.history(store, "ep01")
            self.assertEqual([r["message"] for r in rows], ["new", "old"])
            self.assertTrue(all(r["snapshot"] and r["time"] for r in rows))
            store.revert("ep01", rows[1]["snapshot"])
            self.assertEqual(store.text("episodes/ep01.md"), "# EP01\n旧\n")
            self.assertGreaterEqual(len(service.history(store)), 3)


class EpisodeRevertTests(unittest.TestCase):
    def test_reverting_an_episode_restores_its_ending_ledger(self):
        with tempfile.TemporaryDirectory(prefix="sfl-revert-") as folder:
            store = create_project(Path(folder) / "p", {"name": "p", "output": {"model_card": "seedance-2.0", "ratio": "16:9", "music": "none"}})
            store.write("episodes/ep01.md", "# EP01\n旧\n")
            store.write("ledger/ep01.md", "- knows: 旧\n")
            first = store.snapshot("old")
            store.write("episodes/ep01.md", "# EP01\n新\n")
            store.write("ledger/ep01.md", "- knows: 新\n")
            store.snapshot("new")
            store.revert("ep01", first)
            self.assertEqual(store.text("episodes/ep01.md"), "# EP01\n旧\n")
            self.assertEqual(store.text("ledger/ep01.md"), "- knows: 旧\n")


if __name__ == "__main__":
    unittest.main()
