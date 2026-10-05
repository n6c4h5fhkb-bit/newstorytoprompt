from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.support import project, scripted_runner
from tests.support_writer import novel_project, writer_runner, approve_first
from storyforge.cards import Cards
from storyforge.config import configuration
from storyforge.store import Store, atomic_write


class SimulatedCrash(BaseException):
    pass


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-batch-")
        self.addCleanup(self.temporary.cleanup)

    def test_crash_during_multifile_commit_resumes_with_saved_scopes(self):
        store = project(Path(self.temporary.name)/"crash")
        binding = store.binding("episodes/ep01.md")
        job = store.start_job("B7","ep01",[binding])
        outputs = {"prompts/ep01/u01.md":"first result","prompts/ep01/u02.md":"second result"}
        def crash(path,text):
            if path==store.path("prompts/ep01/u02.md"):raise SimulatedCrash()
            return atomic_write(path,text)
        with patch("storyforge.store.atomic_write",side_effect=crash), self.assertRaises(SimulatedCrash):
            store.accept(job,"B7","ep01",[binding],outputs,output_scope=lambda p,b:[] if p.endswith("u02.md") else b)
        self.assertEqual(store.text("prompts/ep01/u01.md"),"first result")
        recovered = Store(store.root)
        with recovered.run_lease():pass
        self.assertTrue(recovered.current("prompts/ep01/u01.md"))
        self.assertTrue(recovered.current("prompts/ep01/u02.md"))
        self.assertEqual(recovered.json(".state/artifacts.json")["prompts/ep01/u02.md"]["bindings"],[])
        self.assertFalse(list(recovered.path(".runtime/transactions").glob("*.json")))
        self.assertEqual(next(j for j in recovered.jobs() if j["id"]==job)["status"],"complete")
        with recovered.run_lease():pass
        completions = [r for r in recovered.logs("decisions") if r.get("event")=="stage_completed" and r.get("job_id")==job]
        self.assertEqual(len(completions),1)

    def test_crash_recovery_preserves_newer_user_content(self):
        store = project(Path(self.temporary.name)/"late")
        binding = store.binding("episodes/ep01.md")
        job = store.start_job("B7","ep01",[binding])
        def crash(path,text):
            if path==store.path("prompts/ep01/u02.md"):raise SimulatedCrash()
            return atomic_write(path,text)
        with patch("storyforge.store.atomic_write",side_effect=crash), self.assertRaises(SimulatedCrash):
            store.accept(job,"B7","ep01",[binding],{"prompts/ep01/u01.md":"old candidate","prompts/ep01/u02.md":"old second"})
        store.write("episodes/ep01.md",store.text("episodes/ep01.md").replace("林恒：打开它。","林恒：等等。"))
        store.write("prompts/ep01/u01.md","newer user content")
        with store.run_lease():pass
        self.assertEqual(store.text("prompts/ep01/u01.md"),"newer user content")
        self.assertFalse(store.path("prompts/ep01/u02.md").exists())
        self.assertEqual(store.text(f".state/stale_candidates/{job}/prompts/ep01/u02.md"),"old second")
        self.assertEqual(next(j for j in store.jobs() if j["id"]==job)["status"],"stale")

    def test_parallel_summaries_and_reviews_obey_concurrency_limit(self):
        store = novel_project(Path(self.temporary.name)/"parallel",4)
        runner, model = writer_runner(store,4)
        runner.config["concurrency"]["llm"] = 2
        # Recreate the shared client limit, as a production runner does on config reload.
        runner.client.semaphore = threading.BoundedSemaphore(2)
        original = runner.client.codex_transport
        guard, active, maximum = threading.Lock(),0,0
        def delayed(profile,packet,schema,**kwargs):
            nonlocal active,maximum
            with guard:active+=1;maximum=max(maximum,active)
            try:
                if packet.role=="chunk_summarizer":time.sleep(.2)
                return original(profile,packet,schema,**kwargs)
            finally:
                with guard:active-=1
        runner.client.codex_transport = delayed
        self.assertFalse(runner.run(until="A2")["waiting"])
        self.assertEqual(maximum,2)
        self.assertEqual(sum(model.counts.values()),4)

    def test_concurrent_cards_never_exceed_visible_capacity(self):
        store = project(Path(self.temporary.name)/"cards")
        cards = Cards(store,configuration(store.root))
        def create(n):return cards.create(kind="stuck",stage="B8",target=f"ep01_u{n:02d}",question="测试",options=[{"key":"retry","label":"重试"},{"key":"stop","label":"停止"}],recommended="retry",reason="测试",dedupe=f"parallel-card:{n}")
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(create,range(1,9)))
        self.assertEqual(sum(c["status"]=="open" for c in cards.list()),3)
        self.assertEqual(sum(c["status"]=="queued" for c in cards.list()),5)

    def test_episode_reviews_overlap_without_sharing_private_context(self):
        store = novel_project(Path(self.temporary.name)/"episode-reviews",1)
        runner, model = writer_runner(store,1)
        runner.config["concurrency"]["llm"] = 2
        runner.client.semaphore = threading.BoundedSemaphore(2)
        rendezvous = threading.Barrier(2)
        original = runner.client.codex_transport
        def overlap(profile,packet,schema,**kwargs):
            if packet.role in ("viewer","story_check"):
                rendezvous.wait(3)
            return original(profile,packet,schema,**kwargs)
        runner.client.codex_transport = overlap
        report = runner.run(until="A8",episodes=[1])
        self.assertFalse(report["paused"],report)
        self.assertTrue(report["waiting"])
        packets = {role:data for role,target,data,repairs in model.packets if role in ("viewer","story_check")}
        self.assertEqual(set(packets),{"viewer","story_check"})
        self.assertNotIn("source_passages",packets["viewer"])
        self.assertNotIn("plan_slice",packets["viewer"])
        self.assertTrue(packets["story_check"]["source_passages"])
        self.assertEqual(packets["viewer"]["script"],packets["story_check"]["script"])
        self.assertEqual(model.counts["episode_writer:ep01"],1)

    def test_episode_reviews_obey_single_call_limit(self):
        store = novel_project(Path(self.temporary.name)/"single-review",1)
        runner, model = writer_runner(store,1)
        runner.config["concurrency"]["llm"] = 1
        runner.client.semaphore = threading.BoundedSemaphore(1)
        original = runner.client.codex_transport
        guard, active, maximum = threading.Lock(),0,0
        def tracked(profile,packet,schema,**kwargs):
            nonlocal active,maximum
            if packet.role not in ("viewer","story_check"):
                return original(profile,packet,schema,**kwargs)
            with guard:active+=1;maximum=max(maximum,active)
            try:
                time.sleep(.05)
                return original(profile,packet,schema,**kwargs)
            finally:
                with guard:active-=1
        runner.client.codex_transport = tracked
        report = runner.run(until="A8",episodes=[1])
        self.assertFalse(report["paused"],report)
        self.assertEqual(maximum,1)
        self.assertEqual(model.counts["viewer:ep01"],1)
        self.assertEqual(model.counts["story_check:ep01"],1)

    def test_review_capacity_pause_preserves_other_review_and_author_for_resume(self):
        store = novel_project(Path(self.temporary.name)/"review-capacity",1)
        runner, model = writer_runner(store,1)
        original = runner.client.codex_transport
        def failed_viewer(profile,packet,schema,**kwargs):
            result = original(profile,packet,schema,**kwargs)
            if packet.role=="viewer" and model.counts["viewer:ep01"]==1:
                return {"error":"Selected model is at capacity", "text":None,"usage":{},"exit_code":1,"retryable":False}
            return result
        runner.client.codex_transport = failed_viewer
        paused = runner.run(until="A8",episodes=[1])
        self.assertTrue(paused["paused"],paused)
        self.assertFalse(store.path("episodes/ep01.md").exists())
        self.assertTrue(list(store.path("findings/story_check").glob("ep01_*.json")))
        self.assertEqual(model.counts["story_check:ep01"],1)
        resumed = runner.run(until="A8",episodes=[1])
        self.assertFalse(resumed["paused"],resumed)
        self.assertTrue(resumed["waiting"])
        self.assertEqual(model.counts["episode_writer:ep01"],1)
        self.assertEqual(model.counts["story_check:ep01"],1)
        self.assertEqual(model.counts["viewer:ep01"],2)
        self.assertTrue(any(r.get("cached") and r["role"]=="story_check" for r in store.logs("calls")))
        self.assertTrue(store.current(".state/script_reviews/ep01.json"))

    def test_ten_episode_batch_and_resume_never_repeats_paid_work(self):
        store = novel_project(Path(self.temporary.name)/"ten",10)
        runner, model = writer_runner(store,10)
        approve_first(runner)
        report = runner.run()
        self.assertFalse(report["paused"],report)
        self.assertTrue(report["waiting"])
        look = next(c for c in runner.cards.list() if c["stage"]=="B4")
        self.assertEqual(len(store.json(".state/locks.json")),10)
        runner.cards.answer(look["id"],"approve")
        report = runner.run()
        self.assertFalse(report["waiting"],report)
        self.assertFalse(report["paused"],report)
        self.assertEqual(sum(store.current(f"delivery/ep{n:02d}/manifest.json") for n in range(1,11)),10)
        self.assertEqual(len(runner.cards.list(include_resolved=True)),2)
        before = sum(model.counts.values())
        self.assertFalse(runner.run()["waiting"])
        self.assertEqual(sum(model.counts.values()),before)
        # Re-open all objects to exercise persisted state, rather than in-memory flags.
        resumed, second_model = writer_runner(Store(store.root),10)
        self.assertFalse(resumed.run()["waiting"])
        self.assertFalse(second_model.counts)

    def test_writing_next_episode_overlaps_direction_of_locked_episode(self):
        store = novel_project(Path(self.temporary.name)/"rolling")
        runner, model = writer_runner(store)
        approve_first(runner)
        original = runner.client.codex_transport
        directing, writing = threading.Event(),threading.Event()
        def overlap(profile,packet,schema,**kwargs):
            if packet.role=="art_director":
                directing.set()
                self.assertTrue(writing.wait(5),"A never ran while B was running")
            if packet.role=="episode_writer" and kwargs["target"]=="ep02":
                writing.set()
                self.assertTrue(directing.wait(5),"B did not begin the locked episode")
            return original(profile,packet,schema,**kwargs)
        runner.client.codex_transport = overlap
        report = runner.run(until="B4")
        self.assertFalse(report["paused"],report)
        self.assertTrue(directing.is_set() and writing.is_set())

    def test_final_call_crossing_budget_is_held_and_resumed_from_cache(self):
        store = project(Path(self.temporary.name)/"budget")
        runner, model = scripted_runner(store)
        runner.config["token_budget"]["per_episode"] = 100
        report = runner.run(until="B1")
        self.assertTrue(report["waiting"])
        self.assertEqual(model.counts["art_director:ep01"],1)
        self.assertFalse(store.path("style.md").exists())
        card = next(c for c in runner.cards.list() if c["kind"]=="budget")
        runner.cards.answer(card["id"],"raise","1000")
        self.assertFalse(runner.run(until="B1")["waiting"])
        self.assertEqual(model.counts["art_director:ep01"],1)
        self.assertTrue(store.current("style.md"))


if __name__=="__main__":unittest.main()
