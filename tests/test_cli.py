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


@unittest.skipUnless(_pyzipper_available(), "pyzipper not installed")
class DeliverLocalCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "a.txt").write_bytes(b"payload")
        self.state = self.base / "state"
        self.store = self.base / "store"

    def run_cli(self, *args):
        env = {"PYTHONPATH": str(PROJECT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-B", "-m", "file_delivery", *args],
                              cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)

    def deliver(self, key="k1"):
        return self.run_cli("deliver-local", str(self.root), "--root", str(self.root),
                            "--state-dir", str(self.state), "--store-dir", str(self.store),
                            "--key", key, "--json")

    def test_deliver_local_then_status(self):
        r = self.deliver()
        self.assertEqual(r.returncode, 0, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["status"], "stored-local")
        self.assertEqual(sorted(value.keys()), sorted([
            "schema_version", "status", "task_id", "key", "archive_sha256",
            "artifact_path", "password_file", "file_count", "total_bytes", "reused"]))
        self.assertFalse(value["reused"])
        password = Path(value["password_file"]).read_text().strip()
        self.assertNotIn(password, r.stdout)
        self.assertNotIn(password, r.stderr)

        r = self.run_cli("status", "--state-dir", str(self.state), "--key", "k1", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = json.loads(r.stdout)
        self.assertEqual(st["status"], "stored-local")
        self.assertEqual(st["task_id"], value["task_id"])

    def test_deliver_local_reuse(self):
        first = json.loads(self.deliver().stdout)
        second = json.loads(self.deliver().stdout)
        self.assertTrue(second["reused"])
        self.assertEqual(second["task_id"], first["task_id"])

    def test_deliver_local_invalid_key(self):
        r = self.deliver(key="bad key!")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "INVALID_KEY")

    def test_status_unknown_key_exit2(self):
        r = self.run_cli("status", "--state-dir", str(self.state), "--key", "nope", "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "TASK_NOT_FOUND")

    def test_deliver_local_state_inside_root_rejected(self):
        r = self.run_cli("deliver-local", str(self.root), "--root", str(self.root),
                         "--state-dir", str(self.root / "state"),
                         "--store-dir", str(self.store), "--key", "k1", "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "STATE_INVALID")

    def test_deliver_local_help(self):
        r = self.run_cli("deliver-local", "--help")
        self.assertEqual(r.returncode, 0)
        r = self.run_cli("status", "--help")
        self.assertEqual(r.returncode, 0)

    def test_corrupt_db_no_traceback(self):
        self.deliver()
        (self.state / "ledger.sqlite3").write_bytes(b"bad sqlite")
        r = self.deliver()
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertIn(value["error"]["code"], ("STATE_INVALID", "IO_ERROR"))
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.stdout.count("\n"), 1)
        r = self.run_cli("status", "--state-dir", str(self.state),
                         "--key", "k1", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertNotIn("Traceback", r.stderr)

    def test_dotdot_state_inside_root_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        r = self.run_cli("deliver-local", str(self.root), "--root", str(self.root),
                         "--state-dir", str(outside / ".." / "root" / "state"),
                         "--store-dir", str(self.store), "--key", "k1", "--json")
        self.assertEqual(r.returncode, 2)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "STATE_INVALID")


class DeliverQiniuCliTests(unittest.TestCase):
    """CLI surface checks that need no provider traffic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "a.txt").write_bytes(b"payload")
        self.state = self.base / "state"
        self.config = self.base / "qiniu.json"

    def run_cli(self, *args):
        env = {"PYTHONPATH": str(PROJECT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-B", "-m", "file_delivery", *args],
                              cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)

    def test_deliver_qiniu_invalid_key_exit2(self):
        r = self.run_cli("deliver-qiniu", str(self.root), "--root", str(self.root),
                         "--state-dir", str(self.state), "--config", str(self.config),
                         "--key", "bad key!", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "INVALID_KEY")
        self.assertNotIn("Traceback", r.stderr)

    def test_deliver_qiniu_invalid_ttl_exit2(self):
        r = self.run_cli("deliver-qiniu", str(self.root), "--root", str(self.root),
                         "--state-dir", str(self.state), "--config", str(self.config),
                         "--key", "k1", "--ttl-seconds", "0", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "CONFIG_INVALID")

    def test_status_qiniu_unknown_key_exit2(self):
        r = self.run_cli("status-qiniu", "--state-dir", str(self.state),
                         "--key", "nope", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "TASK_NOT_FOUND")
        self.assertNotIn("Traceback", r.stderr)

    def test_remote_help(self):
        for args in (["deliver-qiniu", "--help"], ["status-qiniu", "--help"],
                     ["revoke-qiniu", "--help"], ["cleanup-qiniu", "--help"]):
            with self.subTest(args=args):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 0)
                self.assertTrue(r.stdout.strip())

    def test_revoke_qiniu_unknown_key_exit2(self):
        r = self.run_cli("revoke-qiniu", "--state-dir", str(self.state),
                         "--config", str(self.config), "--key", "nope", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "TASK_NOT_FOUND")
        self.assertNotIn("Traceback", r.stderr)

    def test_cleanup_qiniu_dry_run_unknown_state_exit2(self):
        r = self.run_cli("cleanup-qiniu", "--state-dir", str(self.state), "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "TASK_NOT_FOUND")

    def test_cleanup_qiniu_execute_without_config_exit2(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "remote.sqlite3").write_bytes(b"not sqlite")
        r = self.run_cli("cleanup-qiniu", "--state-dir", str(self.state),
                         "--execute", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertNotIn("Traceback", r.stderr)


class EmailCliTests(unittest.TestCase):
    """Offline CLI surface checks for send-email/status-email."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / "state"

    def run_cli(self, *args):
        env = {"PYTHONPATH": str(PROJECT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-B", "-m", "file_delivery", *args],
                              cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)

    def test_email_help(self):
        for args in (["send-email", "--help"], ["status-email", "--help"]):
            with self.subTest(args=args):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 0)
                self.assertTrue(r.stdout.strip())

    def test_status_email_unknown_key_exit2(self):
        r = self.run_cli("status-email", "--state-dir", str(self.state),
                         "--key", "nope", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "TASK_NOT_FOUND")
        self.assertNotIn("Traceback", r.stderr)

    def test_send_email_invalid_key_exit2(self):
        smtp = self.base / "smtp.json"
        smtp.write_text("{}")
        r = self.run_cli("send-email", "--state-dir", str(self.state),
                         "--delivery-key", "dk", "--smtp-config", str(smtp),
                         "--to", "a@example.test", "--key", "bad key!", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"]["code"], "INVALID_KEY")
        self.assertNotIn("Traceback", r.stderr)

    def test_send_email_missing_task_exit2(self):
        password = self.base / "smtp-password.txt"
        password.write_text("cli-secret\n")
        password.chmod(0o600)
        smtp = self.base / "smtp.json"
        smtp.write_text(json.dumps({
            "schema_version": 1, "host": "smtp.example.test", "port": 465,
            "tls": "implicit", "username": "sender@example.test",
            "password_file": password.name,
            "from_address": "sender@example.test", "timeout_seconds": 3}))
        smtp.chmod(0o600)
        r = self.run_cli("send-email", "--state-dir", str(self.state),
                         "--delivery-key", "dk", "--smtp-config", str(smtp),
                         "--to", "a@example.test", "--key", "k1", "--json")
        self.assertEqual(r.returncode, 2, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["error"]["code"], "TASK_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
