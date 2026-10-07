"""B returns source problems to A with independent checks and no real calls."""
from pathlib import Path
import tempfile
import unittest

from tests.support_writer import SCRIPT, BIBLE, LEDGER, writer_runner
from storyforge.runner import import_episode
from storyforge.store import create_project

BAD = SCRIPT.replace("妖商站在柜台后，手握铜钥匙","妖商站在柜台后，云清禾手握铜钥匙")
NOTE = {"location":"S01","evidence":"云清禾手握铜钥匙","note":"铜钥匙仍应在妖商手中，修正错误持有者。"}


class ScriptRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfl-routing-")
        self.addCleanup(temporary.cleanup)
        self.store = create_project(Path(temporary.name)/"synthetic",{"name":"synthetic","demo":True})
        import_episode(self.store,BAD,BIBLE,LEDGER)

    def start(self,mutate):
        runner, model = writer_runner(self.store,mutate=mutate)
        self.assertTrue(runner.run(until="B4")["waiting"])
        card = next(c for c in runner.cards.list() if c["stage"]=="B4")
        runner.cards.answer(card["id"],"approve")
        return runner, model

    def test_source_script_problem_returns_to_a_and_delivery_resumes(self):
        def mutate(value,packet,target,count):
            if packet.role=="scene_fidelity" and NOTE["evidence"] in packet.data["script_scene"]:
                value["script_notes"] = [dict(NOTE)]
        runner,model = self.start(mutate)
        report = runner.run()
        self.assertFalse(report["waiting"],report)
        self.assertFalse(report["paused"],report)
        notes = self.store.json("notes/script_notes.json")
        self.assertEqual(len(notes),1)
        self.assertEqual(notes[0]["by"],"model")
        self.assertEqual(notes[0]["status"],"applied")
        self.assertEqual(notes[0]["origin"]["evidence"],NOTE["evidence"])
        self.assertNotIn(NOTE["evidence"],self.store.text("episodes/ep01.md"))
        self.assertEqual(model.counts["episode_writer:ep01"],1)
        self.assertEqual(model.counts["storyboard:ep01"],2)
        self.assertTrue(self.store.current("delivery/ep01/manifest.json"))
        viewer = next(data for role,_,data,_ in model.packets if role=="viewer")
        self.assertNotIn("script_notes",viewer)
        self.assertFalse(runner.run()["waiting"])
        self.assertEqual(model.counts["episode_writer:ep01"],1)
        self.assertEqual(len(self.store.json("notes/script_notes.json")),1)

    def test_unit_only_evidence_cannot_become_a_script_note(self):
        def mutate(value,packet,target,count):
            if packet.role=="scene_fidelity":
                value["script_notes"] = [{**NOTE,"evidence":"画面右"}]
        runner,model = self.start(mutate)
        report = runner.run(until="B5")
        self.assertTrue(report["waiting"],report)
        self.assertEqual(self.store.text("episodes/ep01.md"),BAD)
        self.assertFalse(self.store.json("notes/script_notes.json",[]))
        self.assertEqual(model.counts["episode_writer:ep01"],0)
        self.assertEqual(model.counts["scene_fidelity:ep01:S01"],2)
        reviews = [r for r in self.store.logs("calls") if r["target"]=="ep01:S01" and r.get("role")=="scene_fidelity"]
        self.assertEqual(len(reviews),runner.config["fix_rounds"]+1)
        self.assertTrue(reviews[-1]["cached"])

    def test_late_source_review_does_not_adopt_an_obsolete_note(self):
        def mutate(value,packet,target,count):
            if packet.role=="scene_fidelity":
                value["script_notes"] = [dict(NOTE)]
                import_episode(self.store,SCRIPT,BIBLE,LEDGER)
        runner,model = self.start(mutate)
        report = runner.run(until="B5")
        self.assertTrue(report["waiting"],report)
        self.assertEqual(self.store.text("episodes/ep01.md"),SCRIPT)
        self.assertFalse(self.store.json("notes/script_notes.json",[]))
        self.assertFalse(self.store.path("storyboard/ep01.json").exists())
        self.assertEqual(model.counts["episode_writer:ep01"],0)

    def test_unresolved_source_problem_stops_with_one_note(self):
        retry = False
        def mutate(value,packet,target,count):
            if packet.role=="scene_fidelity" and NOTE["evidence"] in packet.data["script_scene"]:value["script_notes"] = [dict(NOTE)]
            if packet.role=="episode_writer" and not retry:value["script"] = BAD
        runner,model = self.start(mutate)
        report = runner.run(until="B5")
        self.assertTrue(report["waiting"],report)
        self.assertEqual(len(self.store.json("notes/script_notes.json")),1)
        self.assertFalse(self.store.path("storyboard/ep01.json").exists())
        card = next(c for c in runner.cards.list() if c["kind"]=="stuck" and c["stage"]=="B5")
        self.assertEqual(len(card["details"]["script_note_ids"]),1)
        calls = sum(model.counts.values())
        self.assertTrue(runner.run(until="B5")["waiting"])
        self.assertEqual(sum(model.counts.values()),calls)
        retry = True
        runner.cards.answer(card["id"],"retry","修正铜钥匙持有者，保留原来的开锁动作。")
        report = runner.run()
        self.assertFalse(report["waiting"],report)
        self.assertTrue(self.store.current("delivery/ep01/manifest.json"))
        note = self.store.json("notes/script_notes.json")[-1]
        self.assertEqual(note["by"],"user")
        self.assertEqual(note["status"],"applied")
        writer = [p for p in model.packets if p[0]=="episode_writer"][-1][2]
        self.assertTrue(any(n["note"]==note["note"] for n in writer["script_notes"]))

    def test_a_fresh_major_each_round_no_longer_blocks_the_stage(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfl-followup-")
        self.addCleanup(temporary.cleanup)
        store = create_project(Path(temporary.name)/"synthetic",{"name":"synthetic","demo":True})
        import_episode(store,SCRIPT,BIBLE,LEDGER)
        seen = []
        def mutate(value,packet,target,count):
            if packet.role=="scene_fidelity" and "妖商站在柜台后" in packet.data["script_scene"]:
                seen.append((count, [f["location"] for f in packet.data["previous_findings"]]))
                if count==1:
                    value["findings"] = [{"severity":"major","kind":"error","location":"S01 镜头1","evidence":"妖商站在柜台后","problem":"站位交代不够具体","suggested_fix":"补一句"}]
                else:
                    value["findings"] = [{"severity":"major","kind":"error","location":"S09 镜头9","evidence":"手握铜钥匙","problem":"另一处全新的挑剔","suggested_fix":"再补"}]
        runner, model = writer_runner(store,mutate=mutate)
        self.assertTrue(runner.run(until="B4")["waiting"])
        runner.cards.answer(next(c for c in runner.cards.list() if c["stage"]=="B4")["id"],"approve")
        runner.run(until="B5")
        self.assertFalse([c for c in runner.cards.list() if c["kind"]=="stuck"])
        self.assertTrue(store.current("storyboard/ep01.json"))
        self.assertEqual(seen[0][1], [])
        self.assertEqual(seen[1][1], ["S01 镜头1"], "the second round is a follow-up of the first")


class OpenMajorPolicyTests(unittest.TestCase):
    FINDING = {"severity":"major","kind":"error","location":"S01","evidence":"妖商站在柜台后","problem":"站位交代不够具体","suggested_fix":"补一句站位"}

    def run_policy(self, policy):
        temporary = tempfile.TemporaryDirectory(prefix="sfl-policy-")
        self.addCleanup(temporary.cleanup)
        store = create_project(Path(temporary.name)/"synthetic",{"name":"synthetic","demo":True,"fix_policy":{"open_major":policy}})
        import_episode(store,SCRIPT,BIBLE,LEDGER)
        def mutate(value,packet,target,count):
            if packet.role=="scene_fidelity" and self.FINDING["evidence"] in packet.data["script_scene"]:
                value["findings"] = [dict(self.FINDING)]
        runner, model = writer_runner(store,mutate=mutate)
        self.assertTrue(runner.run(until="B4")["waiting"])
        runner.cards.answer(next(c for c in runner.cards.list() if c["stage"]=="B4")["id"],"approve")
        runner.run(until="B5")
        return store, runner

    def test_default_policy_escalates_a_major_that_survives_the_rounds(self):
        store, runner = self.run_policy("card")
        self.assertTrue(any(c["kind"]=="stuck" and c["stage"]=="B5" for c in runner.cards.list()))
        self.assertFalse(store.path("storyboard/ep01.json").exists())

    def test_accept_policy_records_the_open_major_and_continues(self):
        store, runner = self.run_policy("accept")
        self.assertFalse([c for c in runner.cards.list() if c["kind"]=="stuck"])
        self.assertTrue(store.current("storyboard/ep01.json"))
        meta = store.json(".state/artifacts.json")["storyboard/ep01.json"]
        self.assertEqual([f["problem"] for f in meta["open_findings"]],["站位交代不够具体"])
        self.assertTrue(any(e.get("event")=="open_findings_accepted" for e in store.logs("decisions")))


if __name__=="__main__":unittest.main()
