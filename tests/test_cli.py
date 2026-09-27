"""Unit tests for the CLI surface (worker-owned)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def _pyzipper_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("pyzipper") is not None


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
        for args in (["--help"], ["plan", "--help"], ["pack", "--help"], ["verify", "--help"]):
            with self.subTest(args=args):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 0)
                self.assertTrue(r.stdout.strip())


@unittest.skipUnless(_pyzipper_available(), "pyzipper not installed")
class PackVerifyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        (self.root / "dir one").mkdir(parents=True)
        (self.root / "dir one" / "ünï.txt").write_text("内容", encoding="utf-8")
        (self.root / "empty.bin").write_bytes(b"")
        self.out = self.base / "bundle"

    def run_cli(self, *args):
        env = {"PYTHONPATH": str(PROJECT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-B", "-m", "file_delivery", *args],
                              cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)

    def test_pack_then_verify(self):
        r = self.run_cli("pack", str(self.root), "--root", str(self.root),
                         "--output-dir", str(self.out), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "packaged")
        self.assertEqual(value["file_count"], 2)
        self.assertIn("password_file", value)  # only the file path key, not a secret
        self.assertEqual(sorted(value.keys()), sorted([
            "schema_version", "status", "bundle_dir", "archive_path", "manifest_path",
            "password_file", "file_count", "total_bytes", "archive_sha256"]))
        password = (self.out / "password.txt").read_text().strip()
        self.assertNotIn(password, r.stdout)
        self.assertNotIn(password, r.stderr)

        r = self.run_cli("verify", str(self.out), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        verified = json.loads(r.stdout)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["archive_sha256"], value["archive_sha256"])

    def test_verify_wrong_password_exit2(self):
        self.run_cli("pack", str(self.root), "--root", str(self.root),
                     "--output-dir", str(self.out), "--json")
        wrong = self.base / "wrong.pw"
        wrong.write_text("bogus")
        r = self.run_cli("verify", str(self.out), "--password-file", str(wrong), "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "VERIFY_FAILED")
        self.assertNotIn("bogus", value["error"]["message"])

    def test_pack_output_inside_root_rejected(self):
        r = self.run_cli("pack", str(self.root), "--root", str(self.root),
                         "--output-dir", str(self.root / "out"), "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "OUTPUT_NOT_ALLOWED")

    def test_pack_output_exists_rejected(self):
        self.out.mkdir()
        r = self.run_cli("pack", str(self.root), "--root", str(self.root),
                         "--output-dir", str(self.out), "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "OUTPUT_EXISTS")

    def test_pack_dependency_missing_no_traceback(self):
        # Run a subprocess with pyzipper blocked at import time.
        blocker = self.base / "no_pyzipper.py"
        blocker.write_text(
            "import sys\n"
            "sys.modules['pyzipper'] = None\n"
            "from file_delivery.cli import main\n"
            "sys.exit(main(['pack', sys.argv[1], '--root', sys.argv[1], "
            "'--output-dir', sys.argv[2], '--json']))\n")
        env = {"PYTHONPATH": str(PROJECT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": "/usr/bin:/bin"}
        r = subprocess.run([sys.executable, "-B", str(blocker), str(self.root),
                            str(self.base / "b2")],
                           cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "DEPENDENCY_MISSING")
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
