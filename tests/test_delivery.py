import io
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tests.support import RESPONSES, project, scripted_runner, approve_look, outfit_child
from tests.test_batch import SimulatedCrash
from storyforge.config import SflError
from storyforge.delivery import asset_sheet, export, ready, package
from storyforge.runner import service
from storyforge.store import Store, serialize


class DeliveryTests(unittest.TestCase):
    def test_child_only_episode_exports_its_master_without_adding_a_video_reference(self):
        with tempfile.TemporaryDirectory(prefix="sfl-master-delivery-") as folder:
            store = project(Path(folder) / "story")
            script = store.text("episodes/ep01.md").split("△ 林恒指向门口。")[0]
            service.import_episode(store, script, store.text("bible.md"), store.text("ledger/ep00.md"))
            board = deepcopy(RESPONSES["storyboard:ep01"])
            board["units"] = board["units"][:1]
            board["units"][0]["assets"] = ["char:云清禾@换装" if a=="char:云清禾" else a for a in board["units"][0]["assets"]]
            child_asset, child_response = outfit_child()
            extracted = deepcopy(RESPONSES["asset_extract:ep01"])
            extracted["assets"].append(child_asset)
            prompts = deepcopy(RESPONSES["unit_prompt:ep01:S01"])
            prompts["prompts"][0]["shots"] = prompts["prompts"][0]["shots"].replace("@云清禾_母图", "@云清禾_换装")
            with patch.dict(RESPONSES, {"storyboard:ep01": board, "asset_extract:ep01":extracted,
                "asset_prompt:ep01:asset:char:云清禾@换装":child_response, "unit_prompt:ep01:S01":prompts}):
                runner, model = scripted_runner(store)
                approve_look(runner)
                self.assertFalse(runner.run()["waiting"])
            manifest = store.json("delivery/ep01/manifest.json")
            mapping = manifest["units"][0]["mapping"]
            self.assertEqual(len(manifest["units"]), 1)
            self.assertIn("char:云清禾@换装", {r["asset_id"] for r in mapping})
            self.assertNotIn("char:云清禾", {r["asset_id"] for r in mapping})
            rows = store.assets()
            master = next(a for a in rows if a["id"] == "char:云清禾")
            sheet = store.text("delivery/ep01/assets.md")
            self.assertIn("## @云清禾_母图", sheet)
            self.assertLess(sheet.index("## @云清禾_母图"), sheet.index("## @云清禾_换装"))
            self.assertEqual(sheet.count(master["image_prompt"]), 1)
            self.assertIn("- 母资产：@云清禾_母图", sheet)
            self.assertNotIn("@云清禾_母图", store.text("delivery/ep01/u01.md"))
            with zipfile.ZipFile(io.BytesIO(package(store, "ep01"))) as archive:
                self.assertIn(master["image_prompt"], archive.read("assets.md").decode("utf-8"))
            for invalid in ("missing", "unapproved", "empty_prompt", "wrong_entity", "child_parent", "changed_reference"):
                with self.subTest(invalid=invalid):
                    changed = deepcopy(rows)
                    parent = next(a for a in changed if a["id"] == master["id"])
                    if invalid == "missing":
                        changed.remove(parent)
                    elif invalid == "unapproved":
                        parent["status"] = "described"
                    elif invalid == "empty_prompt":
                        parent["image_prompt"] = ""
                    elif invalid == "wrong_entity":
                        parent["name"] = "另一个人"
                    elif invalid == "child_parent":
                        parent["parent"] = "char:云清禾@换装"
                    else:
                        next(a for a in changed if a["id"] == "char:云清禾@换装")["image_prompt"] += "已修改"
                    store.write("assets.csv", store.assets_text(changed))
                    with self.assertRaises(SflError):
                        asset_sheet(store, mapping)
            store.write("assets.csv", store.assets_text(rows))
            bindings = store.json(".state/artifacts.json")["delivery/ep01/assets.md"]["bindings"]
            asset_binding = next(b for b in bindings if b["path"] == "assets.csv")
            self.assertIn(master["id"], asset_binding["selector"]["ids"])
            unrelated = {**master, "id": "prop:未使用", "type": "prop", "name": "未使用", "placeholder": "@道具_未使用"}
            store.write("assets.csv", store.assets_text(rows + [unrelated]))
            self.assertTrue(ready(store, "ep01"))
            master["image_prompt"] += "\n新的衣料细节。"
            store.write("assets.csv", store.assets_text(rows + [unrelated]))
            self.assertFalse(store.unchanged([asset_binding]))
            self.assertFalse(ready(store, "ep01"))
            self.assertIn("delivery/ep01/assets.md", store.stale_artifacts())

    def test_export_preserves_foreign_files_and_repairs_each_damaged_member_without_model_calls(self):
        with tempfile.TemporaryDirectory(prefix="sfl-delivery-") as folder:
            store = project(Path(folder) / "story")
            runner, model = scripted_runner(store)
            approve_look(runner)
            self.assertFalse(runner.run()["waiting"])
            obsolete = "delivery/ep01/u99.md"
            job = store.start_job("B9", "ep01", [])
            store.accept(job, "B9", "ep01", [], {obsolete: "old generated unit"}, metadata={"demo": True})
            store.write("delivery/ep01/my-notes.md", "user notes")
            export(store, "ep01", runner.card)
            self.assertFalse(store.path(obsolete).exists())
            self.assertNotIn(obsolete, store.json(".state/artifacts.json"))
            self.assertEqual(store.text("delivery/ep01/my-notes.md"), "user notes")
            expected = {"manifest.json", "assets.md", "overlays.md", "u01.md", "u02.md", "u03.md"}
            with zipfile.ZipFile(io.BytesIO(package(store, "ep01"))) as archive:
                self.assertEqual(set(archive.namelist()), expected)
            before = sum(model.counts.values())
            for name in ("assets.md", "overlays.md", "u02.md"):
                with self.subTest(name=name):
                    store.write("delivery/ep01/" + name, "changed after export")
                    self.assertFalse(service.status(store)["progress"]["ep01"]["delivery"])
                    self.assertFalse(service.episode_details(store, "ep01")["delivery_ready"])
                    with self.assertRaises(SflError):
                        package(store, "ep01")
                    flags = [i for i in service.inbox(store)["items"] if i["type"] == "stale"]
                    self.assertEqual([(i["stage"], i["target"]) for i in flags], [("B9", "ep01")])
                    with patch("storyforge.runner.service.Runner", return_value=runner):
                        self.assertFalse(service.rerun(store, "B9", "ep01")["waiting"])
                    self.assertTrue(ready(store, "ep01"))
                    self.assertEqual(sum(model.counts.values()), before)
            store.path("delivery/ep01/u03.md").unlink()
            self.assertFalse(ready(store, "ep01"))
            export(store, "ep01", runner.card)
            self.assertTrue(ready(store, "ep01"))
            # Even a tracked malformed manifest cannot select arbitrary paths.
            manifest = store.json("delivery/ep01/manifest.json")
            manifest["units"][0]["prompt_file"] = "../../episodes/ep01.md"
            job = store.start_job("B9", "ep01", [])
            store.accept(job, "B9", "ep01", [], {"delivery/ep01/manifest.json": serialize(manifest)})
            self.assertFalse(ready(store, "ep01"))
            with self.assertRaises(SflError):
                package(store, "ep01")


class RemovalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sfl-removal-")
        self.addCleanup(self.tmp.cleanup)
        self.store = project(Path(self.tmp.name) / "story")
        self.old = ["delivery/ep01/u98.md", "delivery/ep01/u99.md"]
        job = self.store.start_job("B9", "ep01", [])
        self.store.accept(job, "B9", "ep01", [], {p: "old " + p for p in self.old})
        self.outputs = {"delivery/ep01/manifest.json": "new manifest"}
        self.job = self.store.start_job("B9", "ep01", [])

    def interrupted_removal(self):
        original = Path.unlink
        def crash(path, *args, **kwargs):
            if path == self.store.path(self.old[1]):
                raise SimulatedCrash()
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", crash), self.assertRaises(SimulatedCrash):
            self.store.accept(self.job, "B9", "ep01", [], self.outputs, removals=self.old)
        self.assertFalse(self.store.path(self.old[0]).exists())
        self.assertTrue(self.store.path(self.old[1]).exists())

    def test_prepared_deletions_and_metadata_resume_as_one_transaction(self):
        self.interrupted_removal()
        with self.store.run_lease():
            pass
        self.assertTrue(self.store.current("delivery/ep01/manifest.json"))
        for path in self.old:
            self.assertFalse(self.store.path(path).exists())
            self.assertNotIn(path, self.store.json(".state/artifacts.json"))
        self.assertEqual(next(j for j in self.store.jobs() if j["id"] == self.job)["status"], "complete")
        self.assertEqual(len([r for r in self.store.logs("decisions") if r.get("job_id") == self.job]), 1)

    def test_new_user_content_stops_interrupted_deletions_and_keeps_candidates(self):
        self.interrupted_removal()
        self.store.write(self.old[0], "new user content")
        with self.store.run_lease():
            pass
        self.assertEqual(self.store.text(self.old[0]), "new user content")
        self.assertTrue(self.store.path(self.old[1]).exists())
        self.assertEqual(self.store.json(f".state/stale_candidates/{self.job}/.removals.json"), self.old)
        self.assertEqual(next(j for j in self.store.jobs() if j["id"] == self.job)["status"], "stale")

    def test_committed_recovery_does_not_delete_files_recreated_by_user(self):
        with patch.object(self.store, "snapshot", side_effect=SimulatedCrash()), self.assertRaises(SimulatedCrash):
            self.store.accept(self.job, "B9", "ep01", [], self.outputs, removals=self.old)
        self.store.write(self.old[0], "user file after completed commit")
        with self.store.run_lease():
            pass
        self.assertEqual(self.store.text(self.old[0]), "user file after completed commit")
        self.assertNotIn(self.old[0], self.store.json(".state/artifacts.json"))

    def test_stale_result_never_deletes_and_rejects_overlapping_or_outside_removals(self):
        binding = self.store.binding("episodes/ep01.md")
        self.store.write("episodes/ep01.md", "newer script")
        self.assertFalse(self.store.accept(self.job, "B9", "ep01", [binding], self.outputs, removals=self.old))
        self.assertTrue(all(self.store.path(p).exists() for p in self.old))
        self.assertEqual(self.store.json(f".state/stale_candidates/{self.job}/.removals.json"), self.old)
        for removals in (["../outside.md"], ["delivery/ep01"], list(self.outputs)):
            with self.subTest(removals=removals), self.assertRaises(SflError):
                self.store.accept(self.job, "B9", "ep01", [], self.outputs, removals=removals)


if __name__ == "__main__":
    unittest.main()
