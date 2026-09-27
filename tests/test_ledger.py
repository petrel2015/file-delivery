"""Worker tests for the FD-003 task ledger (idempotency, recovery, locking)."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

from file_delivery import errors, ledger  # noqa: E402


def _pyzipper_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("pyzipper") is not None


@unittest.skipUnless(_pyzipper_available(), "pyzipper not installed")
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        (self.root / "dir one").mkdir(parents=True)
        (self.root / "dir one" / "ünï.txt").write_text("内容", encoding="utf-8")
        (self.root / "empty.bin").write_bytes(b"")
        self.state = self.base / "state"
        self.store = self.base / "store"
        self.key = "job-1"

    def deliver(self, key=None, checkpoint=None, paths=None):
        return ledger.deliver_local(
            paths if paths is not None else [str(self.root)],
            str(self.root), str(self.state), str(self.store),
            key or self.key, checkpoint=checkpoint)

    def status(self, key=None):
        return ledger.status(str(self.state), key or self.key)

    def test_success_and_reuse(self):
        first = self.deliver()
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["status"], "stored-local")
        self.assertFalse(first["reused"])
        obj = Path(first["artifact_path"])
        self.assertEqual(obj.parent, self.store.resolve())
        self.assertTrue(first["archive_sha256"])
        self.assertEqual(
            hashlib.sha256(obj.read_bytes()).hexdigest(), first["archive_sha256"])
        password = Path(first["password_file"]).read_text().strip()
        self.assertNotIn(password, json.dumps(first))

        second = self.deliver()
        self.assertTrue(second["reused"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(second["archive_sha256"], first["archive_sha256"])
        # no second encryption: same password, single bundle, single object
        self.assertEqual(
            Path(second["password_file"]).read_text().strip(), password)
        bundles = list((self.state / "bundles").iterdir())
        self.assertEqual(len(bundles), 1)
        self.assertEqual(sorted(p.name for p in self.store.iterdir()),
                         [f"{first['task_id']}.zip"])

    def test_overlapping_inputs_same_fingerprint(self):
        first = self.deliver()
        second = self.deliver(paths=[str(self.root / "dir one" / "ünï.txt"),
                                     str(self.root / "empty.bin")])
        self.assertTrue(second["reused"])
        self.assertEqual(second["task_id"], first["task_id"])

    def test_changed_content_conflict_preserves_original(self):
        first = self.deliver()
        (self.root / "empty.bin").write_bytes(b"changed")
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.deliver()
        self.assertEqual(ctx.exception.code, errors.IDEMPOTENCY_CONFLICT)
        # original task, bundle and object are preserved
        st = self.status()
        self.assertEqual(st["status"], "stored-local")
        self.assertEqual(st["archive_sha256"], first["archive_sha256"])
        self.assertEqual(
            hashlib.sha256(Path(first["artifact_path"]).read_bytes()).hexdigest(),
            first["archive_sha256"])

    def test_changed_store_conflict(self):
        self.deliver()
        other_store = self.base / "store2"
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(self.state), str(other_store), self.key)
        self.assertEqual(ctx.exception.code, errors.IDEMPOTENCY_CONFLICT)

    def test_invalid_key(self):
        for bad in ("", "bad key!", "x" * 65, "ä", None):
            with self.assertRaises(errors.DeliveryError) as ctx:
                ledger.deliver_local([str(self.root)], str(self.root),
                                     str(self.state), str(self.store), bad)
            self.assertEqual(ctx.exception.code, errors.INVALID_KEY)

    def test_status_unknown_key(self):
        self.deliver()
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.status(key="other")
        self.assertEqual(ctx.exception.code, errors.TASK_NOT_FOUND)

    def test_status_no_source_access(self):
        first = self.deliver()
        self.root.rename(self.base / "root-moved")
        st = self.status()
        self.assertEqual(st["status"], "stored-local")
        self.assertEqual(st["task_id"], first["task_id"])
        self.assertNotIn("password", json.dumps(st))

    def test_after_pack_oserror_pending_then_recover(self):
        def fail(after):
            raise OSError("boom-secret")
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.deliver(checkpoint=fail)
        self.assertEqual(ctx.exception.code, errors.IO_ERROR)
        self.assertNotIn("boom-secret", ctx.exception.message)
        st = self.status()
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["last_error"]["code"], errors.IO_ERROR)
        # recovery: no new password, same task
        first_password = (self.state / "bundles" / st["task_id"]
                          / "password.txt").read_text()
        result = self.deliver()
        self.assertTrue(result["reused"])
        self.assertEqual(
            (self.state / "bundles" / result["task_id"] / "password.txt").read_text(),
            first_password)

    def test_pending_bundle_verify_failure_preserves_bytes(self):
        from unittest import mock
        from file_delivery import archive

        def fail(after):
            raise OSError("boom")
        with self.assertRaises(errors.DeliveryError):
            self.deliver(checkpoint=fail)
        st = self.status()
        self.assertEqual(st["status"], "pending")
        self.assertIsNone(st.get("archive_sha256"))
        bundle_dir = self.state / "bundles" / st["task_id"]
        archive_bytes = (bundle_dir / archive.ARCHIVE_NAME).read_bytes()
        manifest_bytes = (bundle_dir / archive.MANIFEST_NAME).read_bytes()
        password_bytes = (bundle_dir / archive.PASSWORD_NAME).read_bytes()

        real_verify = archive.verify
        calls = []

        def verify_once(bundle):
            calls.append(bundle)
            if len(calls) == 1:
                raise errors.DeliveryError(errors.VERIFY_FAILED, "patched failure")
            return real_verify(bundle)

        with mock.patch.object(archive, "verify", verify_once):
            with self.assertRaises(errors.DeliveryError) as ctx:
                self.deliver()
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)
        # exact archive/manifest/password bytes and task identity preserved
        self.assertEqual((bundle_dir / archive.ARCHIVE_NAME).read_bytes(), archive_bytes)
        self.assertEqual((bundle_dir / archive.MANIFEST_NAME).read_bytes(), manifest_bytes)
        self.assertEqual((bundle_dir / "password.txt").read_bytes(), password_bytes)
        st2 = self.status()
        self.assertEqual(st2["task_id"], st["task_id"])
        self.assertEqual(st2["status"], "pending")
        self.assertEqual(st2["last_error"]["code"], errors.VERIFY_FAILED)

        # next normal retry reuses the existing bundle/password and completes
        result = self.deliver()
        self.assertTrue(result["reused"])
        self.assertEqual(result["task_id"], st["task_id"])
        self.assertEqual((bundle_dir / archive.ARCHIVE_NAME).read_bytes(), archive_bytes)
        self.assertEqual((bundle_dir / archive.PASSWORD_NAME).read_bytes(), password_bytes)
        self.assertEqual(self.status()["status"], "stored-local")

    def test_after_store_oserror_packaged_then_recover(self):
        seen = []

        def fail_once(after):
            seen.append(after)
            if after == "after_store":
                raise OSError("nope")
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.deliver(checkpoint=fail_once)
        self.assertEqual(ctx.exception.code, errors.IO_ERROR)
        self.assertEqual(seen, ["after_pack", "after_store"])
        st = self.status()
        self.assertEqual(st["status"], "packaged")
        result = self.deliver()
        self.assertTrue(result["reused"])
        self.assertEqual(self.status()["status"], "stored-local")

    def _exit_at(self, after):
        script = self.base / "crash.py"
        script.write_text(
            "import os, sys\n"
            "sys.path.insert(0, {src!r})\n"
            "from file_delivery import ledger\n"
            "def cp(stage):\n"
            "    if stage == {stage!r}:\n"
            "        os._exit(9)\n"
            "ledger.deliver_local([{root!r}], {root!r}, {state!r}, {store!r}, {key!r}, "
            "checkpoint=cp)\n".format(src=str(PROJECT / "src"), stage=after,
                                      root=str(self.root), state=str(self.state),
                                      store=str(self.store), key=self.key))
        return subprocess.run([sys.executable, "-B", str(script)],
                              capture_output=True, text=True, timeout=60)

    def test_crash_at_after_pack_recovers_same_password(self):
        r = self._exit_at("after_pack")
        self.assertEqual(r.returncode, 9)
        st = self.status()
        self.assertEqual(st["status"], "pending")
        bundle_pw = (self.state / "bundles" / st["task_id"] / "password.txt").read_text()
        result = self.deliver()
        self.assertTrue(result["reused"])
        self.assertEqual(
            (self.state / "bundles" / result["task_id"] / "password.txt").read_text(),
            bundle_pw)
        self.assertEqual(self.status()["status"], "stored-local")

    def test_crash_at_after_store_recovers(self):
        r = self._exit_at("after_store")
        self.assertEqual(r.returncode, 9)
        st = self.status()
        self.assertEqual(st["status"], "packaged")
        result = self.deliver()
        self.assertTrue(result["reused"])
        self.assertEqual(self.status()["status"], "stored-local")

    def test_missing_object_restored_from_bundle(self):
        first = self.deliver()
        Path(first["artifact_path"]).unlink()
        second = self.deliver()
        self.assertTrue(second["reused"])
        self.assertEqual(
            hashlib.sha256(Path(second["artifact_path"]).read_bytes()).hexdigest(),
            first["archive_sha256"])

    def test_corrupt_object_conflict_never_overwritten(self):
        first = self.deliver()
        obj = Path(first["artifact_path"])
        obj.write_bytes(b"corrupted")
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.deliver()
        self.assertEqual(ctx.exception.code, errors.LOCAL_STORE_CONFLICT)
        self.assertEqual(obj.read_bytes(), b"corrupted")  # never overwritten
        self.assertEqual(self.status()["last_error"]["code"],
                         errors.LOCAL_STORE_CONFLICT)

    def test_db_never_stores_password(self):
        first = self.deliver()
        password = Path(first["password_file"]).read_text().strip()
        db_dump = (self.state / "ledger.sqlite3").read_bytes()
        self.assertNotIn(password.encode(), db_dump)

    def test_file_modes(self):
        first = self.deliver()
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(
            os.stat(self.state / "ledger.sqlite3").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(
            os.stat(Path(first["artifact_path"])).st_mode), 0o600)
        for lock in (self.state / "locks").iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)

    def test_state_inside_root_rejected(self):
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(self.root / "state"), str(self.store), self.key)
        self.assertEqual(ctx.exception.code, errors.STATE_INVALID)

    def test_state_and_store_disjoint(self):
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(self.state), str(self.state / "inner"), self.key)
        self.assertEqual(ctx.exception.code, errors.STATE_INVALID)

    def test_symlinked_state_rejected(self):
        link = self.base / "state-link"
        real = self.base / "state-real"
        real.mkdir()
        os.symlink(real, link)
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(link), str(self.store), self.key)
        self.assertEqual(ctx.exception.code, errors.STATE_INVALID)

    def test_existing_dir_group_access_rejected(self):
        bad = self.base / "loose"
        bad.mkdir()
        os.chmod(bad, 0o750)
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(bad), str(self.store), self.key)
        self.assertEqual(ctx.exception.code, errors.STATE_INVALID)

    def test_missing_retained_bundle_fails_safely(self):
        first = self.deliver()
        bundle_dir = self.state / "bundles" / first["task_id"]
        obj = Path(first["artifact_path"])
        obj_before = obj.read_bytes()
        import shutil
        shutil.rmtree(bundle_dir)
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.deliver()
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)
        # no second encryption: no new bundle, object untouched, task preserved
        self.assertFalse(bundle_dir.exists())
        self.assertEqual(obj.read_bytes(), obj_before)
        st = self.status()
        self.assertEqual(st["status"], "stored-local")
        self.assertEqual(st["archive_sha256"], first["archive_sha256"])
        self.assertNotIn("password", json.dumps(st).lower())

    def test_dotdot_state_alias_inside_root_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        aliased = outside / ".." / "root" / "state"
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(aliased), str(self.store), self.key)
        self.assertEqual(ctx.exception.code, errors.STATE_INVALID)
        self.assertFalse((self.root / "state").exists())

    def test_dotdot_state_store_overlap_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.state.mkdir()
        aliased = outside / ".." / "state" / "inner"
        with self.assertRaises(errors.DeliveryError) as ctx:
            ledger.deliver_local([str(self.root)], str(self.root),
                                 str(self.state), str(aliased), self.key)
        self.assertEqual(ctx.exception.code, errors.STATE_INVALID)
        self.assertFalse((self.state / "inner").exists())

    def test_benign_dotdot_alias_outside_is_idempotent(self):
        outside = self.base / "outside"
        outside.mkdir()
        aliased = outside / ".." / "state"
        first = ledger.deliver_local([str(self.root)], str(self.root),
                                     str(aliased), str(self.store), self.key)
        second = self.deliver()
        self.assertTrue(second["reused"])
        self.assertEqual(second["task_id"], first["task_id"])

    def test_corrupt_db_structured_error(self):
        self.deliver()
        (self.state / "ledger.sqlite3").write_bytes(b"bad sqlite")
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.deliver()
        self.assertIn(ctx.exception.code,
                      (errors.STATE_INVALID, errors.IO_ERROR))
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.status()
        self.assertIn(ctx.exception.code,
                      (errors.STATE_INVALID, errors.IO_ERROR, errors.TASK_NOT_FOUND))

    def test_concurrent_first_run_repeated(self):
        script = self.base / "concurrent.py"
        script.write_text(
            "import json, sys\n"
            "sys.path.insert(0, {src!r})\n"
            "from file_delivery import ledger\n"
            "state, store = sys.argv[1], sys.argv[2]\n"
            "result = ledger.deliver_local([{root!r}], {root!r}, state, store, {key!r})\n"
            "print(json.dumps(result))\n".format(src=str(PROJECT / "src"),
                                                 root=str(self.root), key=self.key))
        for attempt in range(3):
            state = self.base / f"s{attempt}"
            store = self.base / f"t{attempt}"
            procs = [subprocess.Popen(
                [sys.executable, "-B", str(script), str(state), str(store)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(3)]
            results = []
            for proc in procs:
                out, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0,
                                 f"attempt {attempt}: {err}\n{out}")
                results.append(json.loads(out))
            self.assertEqual(len({r["task_id"] for r in results}), 1)
            self.assertEqual(len({r["archive_sha256"] for r in results}), 1)

    def test_no_network_usage(self):
        import importlib
        source = importlib.import_module("file_delivery.ledger")
        banned = ("socket", "urllib", "http.client", "smtplib", "requests", "boto", "qiniu")
        src = Path(source.__file__).read_text(encoding="utf-8")
        for name in banned:
            self.assertNotIn(f"import {name}", src)
            self.assertNotIn(f"from {name}", src)

    def test_concurrent_same_key_processes(self):
        script = self.base / "concurrent.py"
        script.write_text(
            "import json, sys\n"
            "sys.path.insert(0, {src!r})\n"
            "from file_delivery import ledger\n"
            "result = ledger.deliver_local([{root!r}], {root!r}, {state!r}, {store!r}, {key!r})\n"
            "print(json.dumps(result))\n".format(src=str(PROJECT / "src"),
                                                 root=str(self.root),
                                                 state=str(self.state),
                                                 store=str(self.store), key=self.key))
        procs = [subprocess.Popen([sys.executable, "-B", str(script)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
                 for _ in range(2)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out))
        reused_flags = sorted(r["reused"] for r in results)
        self.assertEqual(reused_flags, [False, True])
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(results[0]["archive_sha256"], results[1]["archive_sha256"])


if __name__ == "__main__":
    unittest.main()
