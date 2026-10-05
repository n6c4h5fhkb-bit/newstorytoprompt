from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import subprocess

from tests.support import ROOT, project, scripted_runner, approve_look
from storyforge.cards import Cards
from storyforge.config import configuration
from storyforge.runner import Runner
from storyforge.store import Store, serialize


class StoreCardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-store-")
        self.addCleanup(self.temporary.cleanup)
        self.store = project(Path(self.temporary.name) / "novel")

    def test_late_job_cannot_overwrite_new_content(self):
        bindings = [self.store.binding("episodes/ep01.md")]
        job = self.store.start_job("B5", "ep01", bindings)
        self.store.write("episodes/ep01.md", "newer screenplay")
        self.store.write("storyboard/ep01.json", "newer storyboard")
        self.assertFalse(self.store.accept(job, "B5", "ep01", bindings, {"storyboard/ep01.json": "late result"}))
        self.assertEqual(self.store.text("storyboard/ep01.json"), "newer storyboard")
        self.assertEqual(self.store.text(f".state/stale_candidates/{job}/storyboard/ep01.json"), "late result")

    @unittest.skipUnless(os.name=="nt","Windows device path spelling")
    def test_windows_device_prefix_does_not_change_project_containment(self):
        inside = self.store.root/".cache/llm/entry.json"
        with patch.object(Path,"resolve",return_value=Path("\\\\?\\"+str(inside))):
            self.assertEqual(self.store.path(".cache/llm/entry.json"),inside)
        with patch.object(Path,"resolve",return_value=Path("\\\\?\\C:\\outside-project\\entry.json")):
            with self.assertRaisesRegex(Exception,"outside project"):
                self.store.path(".cache/llm/entry.json")

    def test_narrow_bible_fingerprint_and_style_references(self):
        binding = self.store.binding("bible.md", {"kind": "bible", "names": ["林恒"], "locations": ["妖商店"]})
        original = self.store.text("bible.md")
        self.store.write("bible.md", original.replace("| 妖商 | supporting", "| 妖商 | changed"))
        self.assertTrue(self.store.unchanged([binding]))
        self.store.write("bible.md", self.store.text("bible.md").replace("年轻男声，冷静", "年老男声"))
        self.assertFalse(self.store.unchanged([binding]))
        ref = self.store.binding("bible.md", style_reference=True)
        self.store.write("bible.md", original)
        self.assertFalse(self.store.unchanged([ref]))
        self.assertTrue(self.store.unchanged([ref], include_style=False))

    def test_revert_never_rewrites_money_decisions_or_feedback(self):
        self.store.write("style.md", "old look\n")
        snapshot = self.store.snapshot("old look")
        self.store.append("calls", {"id": "money", "model": "test", "cost": 3.5})
        self.store.append("feedback", {"unit": "ep01_u01", "result": "redo"})
        self.store.append("decisions", {"stage": "B4", "choice": "approve", "by": "user"})
        self.store.write("style.md", "new look\n")
        self.store.snapshot("new look")
        before = {name: self.store.text(f"logs/{name}.jsonl") for name in ("calls", "feedback", "decisions")}
        self.store.revert("project", snapshot)
        self.assertEqual(self.store.text("style.md"), "old look\n")
        self.assertEqual(self.store.text("logs/calls.jsonl"), before["calls"])
        self.assertEqual(self.store.text("logs/feedback.jsonl"), before["feedback"])
        self.assertTrue(self.store.text("logs/decisions.jsonl").startswith(before["decisions"]))

    def test_snapshots_are_text_only(self):
        self.store.path("user-video.mp4").write_bytes(b"media is external")
        self.store.snapshot("text only")
        self.assertNotIn("user-video.mp4", self.store.git("ls-files"))
        self.assertTrue(list((self.store.runtime / "backups").glob("*.sqlite")))

    def test_project_snapshots_recover_across_process_owners(self):
        environment = {"GIT_TEST_ASSUME_DIFFERENT_OWNER":"1", "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":os.devnull}
        with patch.dict(os.environ, environment):
            untrusted = subprocess.run(["git", "-C", str(self.store.root), "status", "--porcelain"],
                capture_output=True, text=True, encoding="utf-8")
            self.assertIn("dubious ownership", untrusted.stderr)
            bindings = [self.store.binding("episodes/ep01.md")]
            job = self.store.start_job("B3", "project", bindings)
            with patch.object(self.store, "snapshot", side_effect=RuntimeError("service restart")):
                with self.assertRaisesRegex(RuntimeError, "service restart"):
                    self.store.accept(job, "B3", "project", bindings, {"style.md":"recovered visual direction\n"})
            self.store.recover_transactions()
            self.assertEqual(self.store.git("show", "HEAD:style.md", strip=False), "recovered visual direction\n")
            self.assertFalse(list(self.store.path(".runtime/transactions").glob("*.json")))
            self.assertEqual(next(j for j in self.store.jobs() if j["id"]==job)["status"], "complete")
            # The exception never persists to repository or user configuration.
            self.assertEqual(self.store.git("config", "--local", "--get-all", "safe.directory", check=False), "")
            still_untrusted = subprocess.run(["git", "-C", str(self.store.root), "status", "--porcelain"],
                capture_output=True, text=True, encoding="utf-8")
            self.assertIn("dubious ownership", still_untrusted.stderr)

    def test_revert_preserves_exact_creative_whitespace(self):
        original = "\n  固定风格\n\n"
        self.store.write("style.md", original)
        snapshot = self.store.snapshot("whitespace")
        self.store.write("style.md", "changed\n")
        self.store.revert("project", snapshot)
        self.assertEqual(self.store.text("style.md"), original)

    def test_card_has_no_timeout_default_and_blocks_only_its_target(self):
        cards = Cards(self.store, configuration(self.store.root))
        card = cards.create(kind="stuck", stage="B8", target="ep01_u01", question="测试", options=[{"key":"retry","label":"重试"},{"key":"stop","label":"停止"}],
                            recommended="retry", reason="测试", dedupe="isolation")
        self.assertIsNone(card.get("answer"))
        self.assertTrue(cards.blocking("B8", "ep01_u01"))
        self.assertFalse(cards.blocking("B8", "ep01_u02"))
        self.assertFalse(cards.blocking("A7", "ep01"))
        self.assertFalse(cards.blocking("B7", "ep02:S01"))
        cards.answer(card["id"], "retry", "具体修改")
        self.assertFalse(cards.blocking("B8", "ep01_u01"))
        self.assertEqual(cards.resolution("B8", "ep01_u01")["answer"]["note"], "具体修改")

    def test_cards_recover_from_logs_after_queue_database_loss(self):
        cards = Cards(self.store, configuration(self.store.root))
        for i in range(4):
            cards.create(kind="stuck", stage="B5", target=f"ep0{i+1}", question="测试", options=[{"key":"retry","label":"重试"},{"key":"stop","label":"停止"}],
                         recommended="retry", reason="测试", dedupe=f"restore-{i}")
        self.assertEqual(sum(c["status"] == "open" for c in cards.list()), 3)
        self.assertEqual(sum(c["status"] == "queued" for c in cards.list()), 1)
        cards.answer(cards.list()[0]["id"], "stop")
        self.store.path(".runtime/jobs.sqlite").unlink()
        recovered = Cards(Store(self.store.root), configuration(self.store.root))
        self.assertEqual(len(recovered.list()), 3)
        self.assertEqual(len(recovered.list(include_resolved=True)), 4)
        self.assertEqual(sum(c["status"] == "open" for c in recovered.list()), 3)

    def test_budget_prevents_further_calls_until_explicit_raise(self):
        runner, transport = scripted_runner(self.store)
        runner.config["token_budget"]["per_episode"] = 200
        result = runner.run()
        self.assertTrue(result["waiting"])
        self.assertEqual(sum(transport.counts.values()), 2)
        budget = next(c for c in runner.cards.list() if c["kind"] == "budget")
        with self.assertRaisesRegex(Exception, "must exceed"):
            runner.cards.answer(budget["id"], "raise", "100")
        runner.cards.answer(budget["id"], "raise", "100000")
        result = runner.run()
        self.assertTrue(any(c["kind"] == "checkpoint" for c in runner.cards.list()))

    def test_stuck_writer_waits_then_uses_user_note_on_retry(self):
        case = json.loads((ROOT / "tests/fixtures/failure_cases/missing_cast/case.json").read_text(encoding="utf-8"))
        case["bad_calls"] = 100
        runner, transport = scripted_runner(self.store, case)
        approve_look(runner)
        result = runner.run()
        self.assertTrue(result["waiting"])
        count = transport.counts["storyboard:ep01"]
        self.assertLessEqual(count, 3)
        self.assertEqual(len([r for r in self.store.logs("calls") if r["role"] == "storyboard"]), 3)
        runner.run()
        self.assertEqual(transport.counts["storyboard:ep01"], count)
        stuck = next(c for c in runner.cards.list() if c["stage"] == "B5")
        runner.cards.answer(stuck["id"], "retry", "补齐柜台后的妖商")
        transport.case["bad_calls"] = count
        report = runner.run()
        self.assertFalse(report["waiting"], report)
        self.assertTrue(any(r.get("user_note", {}).get("note") == "补齐柜台后的妖商" for role,target,data,r in transport.packets if role == "storyboard"))


if __name__ == "__main__":
    unittest.main()
