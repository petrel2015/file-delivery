"""Unit tests for the CLI surface (worker-owned)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.f = self.root / "a b.txt"
        self.f.write_bytes(b"payload")

    def run_cli(self, *args):
        env = {"PYTHONPATH": str(PROJECT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-B", "-m", "file_delivery", *args],
                              cwd=PROJECT, env=env, capture_output=True, text=True, timeout=30)

    def test_plan_success_json_only(self):
        r = self.run_cli("plan", str(self.f), "--root", str(self.root), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "planned")
        self.assertEqual(value["files"][0]["path"], "a b.txt")

    def test_error_exit_code_and_shape(self):
        r = self.run_cli("plan", str(self.root / "missing"), "--root", str(self.root), "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "INPUT_NOT_FOUND")
        self.assertNotIn("files", value)

    def test_help(self):
        for args in (["--help"], ["plan", "--help"]):
            with self.subTest(args=args):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 0)
                self.assertTrue(r.stdout.strip())


if __name__ == "__main__":
    unittest.main()
