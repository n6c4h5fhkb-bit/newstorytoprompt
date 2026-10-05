import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import ROOT, FIXTURE


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.projects = Path(self.temporary.name) / "projects"

    def command(self, *args, code=0, stdin=None):
        result = subprocess.run([sys.executable, str(ROOT / "cli.py"), "--projects-dir", str(self.projects), *args],
            cwd=ROOT, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return json.loads(result.stdout if code != 1 else result.stderr)

    def test_demo_cli_delivers_only_after_explicit_approval_and_records_feedback(self):
        self.assertTrue(self.command("demo", "sample")["demo"])
        first = self.command("run", "sample", code=2)
        self.assertTrue(first["waiting"])
        card = self.command("cards", "sample")["cards"][0]
        self.command("answer", card["id"], "approve")
        report = self.command("run", "sample")
        self.assertIn("ep01:B9", report["completed"])
        folder = self.projects / "sample/delivery/ep01"
        self.assertTrue((folder / "u01.md").exists())
        self.assertTrue(json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["text_only"])
        self.command("feedback", "ep01_u01", "redo", "--note", "演示反馈")
        metrics = self.command("feedback", "ep01_u01", "ok")
        self.assertIsNone(metrics["first_pass_usable_rate"])
        self.command("refs", "ep01_u02", "remove", "@妖商_母图")
        state = self.command("status", "sample")
        self.assertTrue(state["stale"])
        rejected = self.command("export", "sample", "ep01", code=1)
        self.assertIn("error", rejected)

    def test_real_project_import_supports_pasted_unicode_script_on_stdin(self):
        self.command("new", "pasted")
        result = self.command("import", "pasted", "--episode", "-", "--bible", str(FIXTURE / "bible.md"),
            "--ledger-in", str(FIXTURE / "ep00.md"), stdin=(FIXTURE / "ep01.md").read_text(encoding="utf-8"))
        self.assertEqual(result["imported"], "ep01")
        self.assertEqual(self.command("status", "pasted")["metrics"]["real_calls"], 0)

    def test_bad_import_does_not_partially_replace_canonical_files(self):
        self.command("new", "bad")
        result = self.command("import", "bad", "--episode", "-", "--bible", str(FIXTURE / "bible.md"),
            "--ledger-in", str(FIXTURE / "ep00.md"), stdin="# EP01\nhook: 开场\ncliffhanger: 悬念\n## S01 妖商店 · 日 · 内\n△ 忘了出场行", code=1)
        self.assertIn("error", result)
        self.assertFalse((self.projects / "bad/bible.md").exists())

    def test_ambiguous_unit_command_requires_project_selection(self):
        self.command("demo", "one")
        self.command("demo", "two")
        result = self.command("refs", "ep01_u01", "remove", "@妖商_母图", code=1)
        self.assertIn("--project", result["error"])


if __name__ == "__main__":
    unittest.main()
