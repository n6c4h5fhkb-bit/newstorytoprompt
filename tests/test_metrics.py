"""Synthetic measurement records verify accounting, not creative quality."""
from pathlib import Path
import tempfile
import unittest

from tests.support import FIXTURE
from storyforge.config import SflError
from storyforge.runner import service
from storyforge.runner.metrics import production_metrics
from storyforge.store import create_project


class ProductionMetricTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sfl-metrics-")
        self.addCleanup(temporary.cleanup)
        self.store = create_project(Path(temporary.name)/"synthetic",{"name":"synthetic"})
        self.store.write("delivery/ep01/manifest.json",{"episode":"ep01","demo":False,"units":[
            {"id":"ep01_u01","prompt_fingerprint":"one"},{"id":"ep01_u02","prompt_fingerprint":"two"}]},json_data=True)

    def test_partial_reports_stay_unknown_and_repeated_totals_do_not_add(self):
        before = service.metrics(self.store)
        self.assertIsNone(before["generations_per_unit"])
        self.assertIsNone(before["user_minutes"])
        service.feedback(self.store,"ep01_u01","redo",generations=2,user_minutes=5)
        partial = service.metrics(self.store)
        self.assertIsNone(partial["generations_per_unit"])
        self.assertIsNone(partial["user_minutes"])
        self.assertEqual(partial["reported_user_minutes"],5)
        service.feedback(self.store,"ep01_u01","ok",generations=2,user_minutes=5)
        service.feedback(self.store,"ep01_u02","ok",generations=1,user_minutes=0)
        result = service.metrics(self.store)
        self.assertEqual(result["generations_per_unit"],1.5)
        self.assertEqual(result["user_minutes"],5)
        self.assertEqual(result["first_pass_usable_rate"],.5)
        service.feedback(self.store,"ep01_u01","ok")
        self.assertEqual(service.metrics(self.store)["user_minutes"],5)

    def test_invalid_or_decreasing_totals_do_not_append_feedback(self):
        service.feedback(self.store,"ep01_u01","redo",generations=2,user_minutes=5)
        count = len(self.store.logs("feedback"))
        for values in ({"generations":True},{"generations":1.2},{"generations":0},{"generations":1},
                       {"user_minutes":False},{"user_minutes":float("nan")},{"user_minutes":float("inf")},{"user_minutes":-1},{"user_minutes":4}):
            with self.subTest(values=values):
                with self.assertRaises(SflError):service.feedback(self.store,"ep01_u01","ok",**values)
                self.assertEqual(len(self.store.logs("feedback")),count)

    def test_demo_reports_are_excluded_from_real_measurements(self):
        manifest = self.store.json("delivery/ep01/manifest.json")
        manifest["demo"] = True
        self.store.write("delivery/ep01/manifest.json",manifest,json_data=True)
        service.feedback(self.store,"ep01_u01","ok",generations=3,user_minutes=9)
        service.finish_episode(self.store,"ep01",user_minutes=30)
        result = service.metrics(self.store)
        self.assertIsNone(result["first_pass_usable_rate"])
        self.assertIsNone(result["reported_user_minutes"])
        self.assertIsNone(result["generations_per_unit"])
        self.assertEqual(result["finished_episodes"],0)
        self.assertIsNone(result["user_minutes_per_finished_episode"])

    def test_first_delivery_time_and_episode_card_counts_use_file_events(self):
        self.store.append("decisions",{"time":"2026-10-01T00:00:00+00:00","stage":"import","choice":"adopt","demo":False})
        for time in ("2026-10-03T00:00:00+00:00","2026-10-04T00:00:00+00:00"):
            self.store.append("decisions",{"time":time,"event":"stage_completed","stage":"B9","target":"ep01","demo":False})
        for target,detail in (("ep01_u01",{}),("ep100",{}),("project",{"episode":"ep01"}),("project",{})):
            self.store.append("decisions",{"event":"card_opened","card":{"target":target,"details":detail}})
        result = production_metrics(self.store)
        self.assertEqual(result["days_to_first_delivery"],2)
        self.assertEqual(result["per_episode"]["ep01"]["cards_created"],2)
        self.assertEqual(result["per_episode"]["ep100"]["cards_created"],1)
        self.assertEqual(result["cards_shared"],1)

    def test_cli_and_http_share_reporting_fields_and_upload_timestamps(self):
        from storyforge.cli import parser
        from storyforge.ui import Application
        args = parser().parse_args(["feedback","ep01_u01","ok","--generations","3","--user-minutes","4.5"])
        self.assertEqual((args.generations,args.user_minutes),(3,4.5))
        app = Application(self.store.root.parent)
        self.addCleanup(app.pool.shutdown)
        result = app.command({"action":"feedback","project":self.store.root.name,"unit":"ep01_u01","result":"ok","generations":3,"user_minutes":4.5})
        self.assertEqual(result["reported_user_minutes"],4.5)
        args = parser().parse_args(["finish","ep01","--user-minutes","21"])
        self.assertEqual((args.target,args.user_minutes),("ep01",21))
        result = app.command({"action":"finish","project":self.store.root.name,"episode":"ep01","user_minutes":21})
        self.assertEqual(result["finished_episodes"],1)
        self.assertEqual(result["user_minutes_per_finished_episode"],21)
        app.command({"action":"new","project":"pasted","novel":"第一章 测试\n\n明确测试原文。"})
        uploaded = app.store("pasted")
        self.assertTrue(any(r.get("event")=="novel_imported" and r.get("demo") is False for r in uploaded.logs("decisions")))

    def test_completion_requires_a_user_report_and_totals_are_not_added(self):
        service.feedback(self.store,"ep01_u01","ok",user_minutes=4)
        service.feedback(self.store,"ep01_u02","ok",user_minutes=6)
        self.assertEqual(service.metrics(self.store)["finished_episodes"],0)
        self.assertIsNone(service.metrics(self.store)["user_minutes_per_finished_episode"])
        service.finish_episode(self.store,"ep01")
        first = service.metrics(self.store)
        self.assertEqual(first["user_minutes_per_finished_episode"],10)
        service.finish_episode(self.store,"ep01",user_minutes=12)
        service.finish_episode(self.store,"ep01")
        result = service.metrics(self.store)
        self.assertEqual(result["user_minutes_per_finished_episode"],12)
        self.assertEqual(result["user_minutes"],10)
        self.assertEqual(result["finished_episodes"],1)
        self.assertEqual(result["first_finished_at"],first["first_finished_at"])
        self.assertIsNone(result["days_to_first_finished_episode"])

    def test_first_finished_time_is_distinct_from_prompt_delivery(self):
        self.store.append("decisions",{"time":"2026-10-01T00:00:00+00:00","stage":"import","choice":"adopt","demo":False})
        self.store.append("decisions",{"time":"2026-10-03T00:00:00+00:00","event":"stage_completed","stage":"B9","target":"ep01","demo":False})
        self.store.append("feedback",{"time":"2026-10-05T00:00:00+00:00","episode":"ep01","result":"finished","user_minutes":20,"demo":False})
        self.store.append("feedback",{"time":"2026-10-02T00:00:00+00:00","episode":"ep02","result":"finished","user_minutes":1,"demo":True})
        result = service.metrics(self.store)
        self.assertEqual(result["days_to_first_delivery"],2)
        self.assertEqual(result["days_to_first_finished_episode"],4)
        self.assertEqual(result["user_minutes_per_finished_episode"],20)
        self.store.append("feedback",{"time":"2026-10-06T00:00:00+00:00","episode":"ep02","result":"finished","demo":False})
        self.assertIsNone(service.metrics(self.store)["user_minutes_per_finished_episode"])

    def test_invalid_completion_cannot_create_a_record(self):
        for values in ({"episode":"ep02"},{"episode":"../ep01"},{"episode":"ep01","user_minutes":True},
                       {"episode":"ep01","user_minutes":float("nan")},{"episode":"ep01","user_minutes":-1}):
            with self.subTest(values=values),self.assertRaises(SflError):service.finish_episode(self.store,**values)
            self.assertFalse(self.store.logs("feedback"))
        service.finish_episode(self.store,"ep01",user_minutes=10)
        with self.assertRaises(SflError):service.finish_episode(self.store,"ep01",user_minutes=9)
        self.assertEqual(len(self.store.logs("feedback")),1)


if __name__=="__main__":unittest.main()
