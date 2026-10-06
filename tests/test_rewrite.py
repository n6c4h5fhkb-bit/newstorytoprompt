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
