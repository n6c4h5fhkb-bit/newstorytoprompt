"""Imported screenplay revisions use real services and explicit fake models."""
from pathlib import Path
import tempfile
import unittest

from tests.support_writer import SCRIPT, BIBLE, LEDGER, writer_runner
from storyforge.runner import import_episode, StageBlocked
from storyforge.runner.director import Director
from storyforge.runner import service
from storyforge.store import create_project

ENDING = "云清禾、林恒已离店，妖商留在柜台后。\n"


class ImportedWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfl-adopted-")
        self.addCleanup(temporary.cleanup)
        self.store = create_project(Path(temporary.name)/"adopted",{"name":"adopted","demo":True})
        import_episode(self.store,SCRIPT,BIBLE,LEDGER)
        import_episode(self.store,SCRIPT.replace("# EP01","# EP02"),BIBLE,ENDING)
        self.store.write("ledger/ep02.md","第二集结束才知道的绝不可见秘密。\n")

    def deliver(self):
        runner, model = writer_runner(self.store)
        report = runner.run()
        self.assertTrue(report["waiting"],report)
        card = next(c for c in runner.cards.list() if c["stage"]=="B4")
        runner.cards.answer(card["id"],"approve")
        report = runner.run()
        self.assertFalse(report["waiting"],report)
        self.assertFalse(report["paused"],report)
        return runner, model

    def test_local_note_reviews_and_relocks_only_one_delivered_episode(self):
        runner, model = self.deliver()
        original, later = self.store.text("episodes/ep01.md"),self.store.text("delivery/ep02/u01.md")
        before = dict(model.counts)
        service.note(self.store,"ep01","只调整开锁前一句对白，保持结尾状态和钩子。")
        self.assertEqual(self.store.text("episodes/ep01.md"),original)
        stale = self.store.stale_artifacts()
        self.assertIn("episodes/ep01.md",stale)
        self.assertNotIn("episodes/ep02.md",stale)
        self.assertNotIn("storyboard/ep02.json",stale)
        report = runner.run()
        self.assertTrue(any(w["episode"]=="ep01" and w["stage"]=="B5" for w in report["waiting"]),report)
        self.assertIn("林恒：把锁打开。",self.store.text("episodes/ep01.md"))
        self.assertEqual(self.store.text("ledger/ep01.md"),ENDING)
        self.assertEqual(self.store.json("notes/script_notes.json")[0]["status"],"applied")
        self.assertTrue(service.status(self.store)["progress"]["ep01"]["script"])
        self.assertTrue(self.store.current("delivery/ep02/manifest.json"))
        self.assertEqual(self.store.text("delivery/ep02/u01.md"),later)
        for key,count in before.items():
            if ":ep02" in key:self.assertEqual(model.counts[key],count)
        report = runner.run(allow_stale=["ep01"])
        self.assertFalse(report["waiting"],report)
        self.assertFalse(report["paused"],report)
        self.assertTrue(self.store.current("delivery/ep01/manifest.json"))
        self.assertTrue(self.store.current("delivery/ep02/manifest.json"))
        for role,target,data,repairs in model.packets:
            if role=="episode_writer":
                self.assertEqual(data["adopted_script"],original)
                self.assertFalse(data["plan_slice"])
                self.assertFalse(data["source_passages"])
                self.assertFalse(data["amplified"])
                self.assertNotIn("绝不可见",str(data)+str(repairs))
            if role=="viewer":
                self.assertEqual(set(data),{"script","recap","example","warnings"})
                self.assertFalse(repairs)
        self.assertFalse(any(role in ("chunk_summarizer","breakdown","adapt_plan") for role,_,_,_ in model.packets))

    def test_new_input_during_revision_preserves_original_and_pending_notes(self):
        def mutate(value,packet,target,count):
            if packet.role=="episode_writer" and count==1:
                service.note(self.store,"ep01","再补充一条修改意见。")
        runner, model = writer_runner(self.store,mutate=mutate)
        original = self.store.text("episodes/ep01.md")
        service.note(self.store,"ep01","只改一句对白。")
        report = runner.run(until="A10",episodes=[1])
        self.assertTrue(report["waiting"],report)
        self.assertEqual(self.store.text("episodes/ep01.md"),original)
        self.assertTrue(all(n["status"]=="pending" for n in self.store.json("notes/script_notes.json")))
        self.assertTrue(list(self.store.path(".state/stale_candidates").rglob("ep01.md")))
        self.assertEqual(model.counts["episode_writer:ep01"],1)
        report = runner.run(until="A10",episodes=[1])
        self.assertFalse(report["waiting"],report)
        self.assertTrue(all(n["status"]=="applied" for n in self.store.json("notes/script_notes.json")))

    def test_reviewer_blocker_keeps_adopted_script_and_note_pending(self):
        def mutate(value,packet,target,count):
            if packet.role=="story_check":
                value["findings"]=[{"severity":"blocker","kind":"error","location":"S01","evidence":"林恒：把锁打开。","problem":"修改仍需解决。","suggested_fix":"保留原文要求。"}]
        runner, model = writer_runner(self.store,mutate=mutate)
        original = self.store.text("episodes/ep01.md")
        service.note(self.store,"ep01","只改一句对白。")
        report = runner.run(until="A10",episodes=[1])
        self.assertTrue(report["waiting"],report)
        self.assertEqual(self.store.text("episodes/ep01.md"),original)
        self.assertEqual(self.store.json("notes/script_notes.json")[0]["status"],"pending")
        self.assertTrue(any(c["stage"]=="A7" and c["kind"]=="stuck" for c in runner.cards.list()))
        self.assertFalse(any(role=="storyboard" for role,_,_,_ in model.packets))

    def test_changed_ending_stales_next_episode_after_acceptance(self):
        def mutate(value,packet,target,count):
            if packet.role=="episode_writer":value["ledger_out"]="云清禾留在妖商店，林恒独自离店。\n"
        runner, model = writer_runner(self.store,mutate=mutate)
        service.note(self.store,"ep01","让云清禾最后留在店内。")
        self.assertNotIn("episodes/ep02.md",self.store.stale_artifacts())
        report = runner.run(until="A10")
        self.assertTrue(any(w["episode"]=="ep02" for w in report["waiting"]),report)
        self.assertIn("episodes/ep02.md",self.store.stale_artifacts())
        self.assertEqual(model.counts["episode_writer:ep02"],0)

    def test_resume_after_writing_still_requires_current_lock(self):
        runner, model = writer_runner(self.store)
        service.note(self.store,"ep01","只改一句对白。")
        self.assertFalse(runner.run(until="A7",episodes=[1])["waiting"])
        self.assertFalse(service.status(self.store)["progress"]["ep01"]["script"])
        with self.assertRaises(StageBlocked):Director(runner,1)
        self.assertFalse(runner.run(until="A10",episodes=[1])["waiting"])
        self.assertTrue(service.status(self.store)["progress"]["ep01"]["script"])
        self.assertEqual(model.counts["episode_writer:ep01"],1)


if __name__=="__main__":unittest.main()
