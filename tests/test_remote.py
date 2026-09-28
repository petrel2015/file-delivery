"""Worker tests for the FD-004B remote delivery ledger (synthetic provider)."""

import hashlib
import json
import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from file_delivery import errors, remote  # noqa: E402


def _optional(*names) -> bool:
    import importlib.util
    return all(importlib.util.find_spec(n) is not None for n in names)


class SyntheticStore:
    """API-compatible in-memory provider following the frozen protocol."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.uploads = 0
        self.signs = 0
        self.stats = 0
        self.deletions = 0
        self.probes = 0
        self.unknown = False
        self.conflicting = False
        self.delete_unknown = False
        self.probe_result = {"status": "link-accessible", "http_status": 200}

    def _object(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".object")

    def ensure_private(self):
        pass

    def stat(self, key):
        self.stats += 1
        if self.conflicting:
            return {"key": key, "etag": "different", "size": 1}
        p = self._object(key)
        if not p.exists():
            return None
        import qiniu
        return {"key": key, "etag": qiniu.etag(str(p)), "size": p.stat().st_size}

    def upload(self, path, key, retention_days=30):
        self.uploads += 1
        if self.unknown:
            self._object(key).write_bytes(Path(path).read_bytes())
            raise errors.DeliveryError("REMOTE_UNKNOWN", "secret-diagnostic-marker")
        data = Path(path).read_bytes()
        if self._object(key).exists():
            raise errors.DeliveryError("REMOTE_CONFLICT", "secret-diagnostic-marker")
        self._object(key).write_bytes(data)
        import qiniu
        return {"status": "uploaded", "key": key, "etag": qiniu.etag(str(path)), "size": len(data)}

    def verify_download(self, key, expected_sha256, expected_size):
        data = self._object(key).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if (digest, len(data)) != (expected_sha256, expected_size):
            raise errors.DeliveryError("REMOTE_INTEGRITY", "secret-diagnostic-marker")
        return {"status": "link-verified", "sha256": digest, "size": len(data)}

    def signed_url(self, key, ttl_seconds=604800):
        self.signs += 1
        deadline = int(time.time()) + ttl_seconds
        return {"url": "https://files.example.com/" + key + "?e=" + str(deadline)
                + "&token=synthetic-signature", "expires_at": deadline}

    def delete(self, key):
        self.deletions += 1
        if self.delete_unknown:
            raise errors.DeliveryError("REMOTE_UNKNOWN", "secret-diagnostic-marker")
        self._object(key).unlink(missing_ok=True)
        return {"status": "object-deleted", "key": key}

    def probe_link(self, url):
        self.probes += 1
        return dict(self.probe_result)


@unittest.skipUnless(_optional("pyzipper", "qiniu"), "optional dependencies missing")
class RemoteDeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "报告.txt").write_text("交付内容", encoding="utf-8")
        self.config = self.base / "qiniu.json"
        self.write_config()
        self.state = self.base / "state"
        self.store = SyntheticStore(self.base / "objects")
        self.key = "remote-1"

    def write_config(self, changes=None, path=None):
        values = dict(access_key="synthetic-access-key", secret_key="synthetic-secret-key",
                      bucket="fd-test", region="z0",
                      download_domain="https://files.example.com/")
        target = path or self.config
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({**values, **(changes or {})}))
        target.chmod(0o600)
        return target

    def deliver(self, **kwargs):
        args = dict(paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
                    config_path=str(self.config), key=self.key, store=self.store)
        args.update(kwargs)
        return remote.deliver(**args)

    def failure(self, code, fn):
        with self.assertRaises(errors.DeliveryError) as ctx:
            fn()
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        self.assertNotIn("secret-diagnostic-marker", str(ctx.exception))
        return ctx.exception

    def test_success_result_and_handoff(self):
        result = self.deliver()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "link-verified")
        self.assertEqual(result["key"], self.key)
        self.assertEqual(result["object_key"], "file-delivery/" + result["task_id"] + ".zip")
        self.assertEqual(result["file_count"], 1)
        self.assertFalse(result["reused"])
        bundle = self.state / "remote-bundles" / result["task_id"]
        data = (bundle / "archive.zip").read_bytes()
        self.assertEqual(result["archive_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["archive_size"], len(data))
        self.assertEqual(Path(result["password_file"]), bundle / "password.txt")
        handoff = json.loads(Path(result["handoff_file"]).read_text())
        self.assertEqual(handoff["url"], "https://files.example.com/" + result["object_key"]
                         + "?e=" + str(result["expires_at"]) + "&token=synthetic-signature")
        self.assertEqual(handoff["archive_sha256"], result["archive_sha256"])
        status = remote.status(str(self.state), self.key)
        self.assertEqual(status["status"], "link-verified")
        self.assertEqual(status["task_id"], result["task_id"])
        self.assertEqual(status["handoff_file"], result["handoff_file"])

    def test_duplicate_reuses_everything(self):
        first = self.deliver()
        bundle = self.state / "remote-bundles" / first["task_id"]
        password = (bundle / "password.txt").read_bytes()
        archive = (bundle / "archive.zip").read_bytes()
        again = self.deliver()
        self.assertTrue(again["reused"])
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertEqual(again["expires_at"], first["expires_at"])
        self.assertEqual(self.store.uploads, 1)
        self.assertEqual((bundle / "password.txt").read_bytes(), password)
        self.assertEqual((bundle / "archive.zip").read_bytes(), archive)

    def test_policy_change_conflicts_secret_rotation_does_not(self):
        first = self.deliver()
        self.failure("IDEMPOTENCY_CONFLICT", lambda: self.deliver(ttl_seconds=60))
        self.failure("IDEMPOTENCY_CONFLICT", lambda: self.deliver(retention_days=31))
        self.write_config({"bucket": "other-bucket"})
        self.failure("IDEMPOTENCY_CONFLICT", self.deliver)
        self.write_config({"secret_key": "rotated"})
        reused = self.deliver()
        self.assertEqual(reused["task_id"], first["task_id"])

    def test_invalid_policy_rejected_before_provider(self):
        for kwargs in ({"ttl_seconds": 0}, {"ttl_seconds": True}, {"retention_days": 0},
                       {"ttl_seconds": 100000, "retention_days": 1}):
            self.failure("CONFIG_INVALID", lambda kwargs=kwargs: self.deliver(**kwargs))
        self.failure("INVALID_KEY", lambda: self.deliver(key="../bad"))
        self.assertEqual(self.store.uploads, 0)
        self.assertEqual(self.store.signs, 0)

    def test_config_inside_root_rejected(self):
        inner = self.write_config(path=self.root / "qiniu.json")
        self.failure("CONFIG_INVALID", lambda: self.deliver(config_path=str(inner)))

    def test_unknown_upload_recovers_without_reupload(self):
        self.store.unknown = True
        self.failure("REMOTE_UNKNOWN", self.deliver)
        saved = remote.status(str(self.state), self.key)
        self.assertEqual(saved["status"], "uploading")
        self.assertEqual(saved["last_error"], "REMOTE_UNKNOWN")
        self.store.unknown = False
        result = self.deliver()
        self.assertEqual(self.store.uploads, 1)
        self.assertEqual(result["status"], "link-verified")

    def test_remote_conflict_when_object_differs(self):
        self.store.conflicting = True
        self.failure("REMOTE_CONFLICT", self.deliver)
        self.assertEqual(self.store.uploads, 0)

    def test_checkpoint_failure_preserves_bundle(self):
        def stop(stage):
            if stage == "after_pack":
                raise OSError("boom")
        self.failure("IO_ERROR", lambda: self.deliver(checkpoint=stop))
        saved = remote.status(str(self.state), self.key)
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(saved["last_error"], "IO_ERROR")
        bundle = self.state / "remote-bundles" / saved["task_id"]
        self.assertTrue((bundle / "password.txt").exists())
        result = self.deliver()
        self.assertEqual(Path(result["password_file"]).parent, bundle)

    def test_tampered_handoff_is_state_invalid(self):
        result = self.deliver()
        handoff = Path(result["handoff_file"])
        raw = handoff.read_bytes()
        payload = json.loads(raw)
        payload["url"] = "https://attacker.example.com/?token=changed"
        handoff.write_text(json.dumps(payload))
        handoff.chmod(0o600)
        self.failure("STATE_INVALID", self.deliver)
        self.assertEqual(json.loads(handoff.read_text())["url"], payload["url"])
        handoff.write_bytes(raw)

    def test_expired_link_is_link_expired_without_renewal(self):
        result = self.deliver()
        with unittest.mock.patch("time.time", return_value=result["expires_at"] + 2):
            uploads, signs = self.store.uploads, self.store.signs
            self.failure("LINK_EXPIRED", self.deliver)
            self.assertEqual(self.store.uploads, uploads)
            self.assertEqual(self.store.signs, signs)

    def test_status_unknown_key(self):
        self.failure("TASK_NOT_FOUND", lambda: remote.status(str(self.state), "absent"))


@unittest.skipUnless(_optional("pyzipper", "qiniu"), "optional dependencies missing")
class RevokeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()
        (self.root / "报告.txt").write_text("交付内容", encoding="utf-8")
        self.config = self.base / "qiniu.json"
        self.write_config()
        self.state = self.base / "state"
        self.store = SyntheticStore(self.base / "objects")
        self.key = "revoke-1"
        self.result = remote.deliver(
            paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
            config_path=str(self.config), key=self.key, store=self.store)

    def write_config(self, changes=None):
        values = dict(access_key="synthetic-access-key", secret_key="synthetic-secret-key",
                      bucket="fd-test", region="z0",
                      download_domain="https://files.example.com/")
        self.config.write_text(json.dumps({**values, **(changes or {})}))
        self.config.chmod(0o600)

    def failure(self, code, fn):
        with self.assertRaises(errors.DeliveryError) as ctx:
            fn()
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        self.assertNotIn("secret-diagnostic-marker", str(ctx.exception))
        return ctx.exception

    def revoke(self, **kwargs):
        args = dict(state_dir=str(self.state), config_path=str(self.config),
                    key=self.key, store=self.store)
        args.update(kwargs)
        return remote.revoke(**args)

    def retention_expiry(self):
        saved = remote.status(str(self.state), self.key)
        return saved["retention_expires_at"]

    def test_revoke_reports_deleted_and_independent_link(self):
        bundle = self.state / "remote-bundles" / self.result["task_id"]
        archive_bytes = (bundle / "archive.zip").read_bytes()
        password_bytes = (bundle / "password.txt").read_bytes()
        handoff_bytes = Path(self.result["handoff_file"]).read_bytes()
        self.store.probe_result = {"status": "link-accessible", "http_status": 200}
        revoked = self.revoke()
        self.assertEqual(revoked["schema_version"], 1)
        self.assertEqual(revoked["status"], "object-deleted")
        self.assertEqual(revoked["key"], self.key)
        self.assertEqual(revoked["task_id"], self.result["task_id"])
        self.assertEqual(revoked["object_key"], self.result["object_key"])
        self.assertEqual(revoked["link_status"], "link-accessible")
        self.assertEqual(revoked["link_http_status"], 200)
        self.assertEqual(self.store.deletions, 1)
        self.assertEqual(self.store.probes, 1)
        # local material is retained byte-for-byte
        self.assertEqual((bundle / "archive.zip").read_bytes(), archive_bytes)
        self.assertEqual((bundle / "password.txt").read_bytes(), password_bytes)
        self.assertEqual(Path(self.result["handoff_file"]).read_bytes(), handoff_bytes)
        saved = remote.status(str(self.state), self.key)
        self.assertEqual(saved["status"], "object-deleted")

    def test_revoke_without_persisted_handoff_reports_unknown_link(self):
        # simulate a task that never reached handoff publication: the URL
        # digest may already be bound, but no handoff was ever persisted
        Path(self.result["handoff_file"]).unlink()
        import sqlite3
        conn = sqlite3.connect(str(self.state / "remote.sqlite3"))
        conn.execute("UPDATE tasks SET handoff_path = NULL")
        conn.commit()
        conn.close()
        revoked = self.revoke()
        self.assertEqual(revoked["status"], "object-deleted")
        self.assertEqual(revoked["link_status"], "link-unknown")
        self.assertIsNone(revoked["link_http_status"])
        self.assertEqual(self.store.probes, 0)
        self.assertEqual(self.store.deletions, 1)

    def test_revoke_missing_previously_persisted_handoff_state_invalid(self):
        Path(self.result["handoff_file"]).unlink()
        self.failure("STATE_INVALID", self.revoke)
        self.assertEqual(self.store.probes, 0)
        self.assertEqual(self.store.deletions, 0)
        self.assertEqual(remote.status(str(self.state), self.key)["status"],
                         "link-verified")

    def test_revoke_tampered_handoff_state_invalid_before_delete(self):
        handoff = Path(self.result["handoff_file"])
        payload = json.loads(handoff.read_text())
        payload["url"] = "https://attacker.example.com/file?token=x"
        handoff.write_text(json.dumps(payload))
        handoff.chmod(0o600)
        self.failure("STATE_INVALID", self.revoke)
        self.assertEqual(self.store.deletions, 0)
        self.assertEqual(remote.status(str(self.state), self.key)["status"],
                         "link-verified")

    def test_revoke_identity_mismatch_zero_provider_calls(self):
        self.write_config({"bucket": "other-bucket"})
        stats, deletions = self.store.stats, self.store.deletions
        self.failure("IDEMPOTENCY_CONFLICT", self.revoke)
        self.assertEqual(self.store.stats, stats)
        self.assertEqual(self.store.deletions, deletions)
        self.write_config({"secret_key": "rotated"})  # secret rotation is allowed
        self.assertEqual(self.revoke()["status"], "object-deleted")

    def test_revoke_legacy_row_without_metadata_rejected(self):
        import sqlite3
        conn = sqlite3.connect(str(self.state / "remote.sqlite3"))
        conn.execute("UPDATE tasks SET provider_identity = NULL, "
                     "retention_days = NULL, first_upload_at = NULL, "
                     "retention_expires_at = NULL")
        conn.commit()
        conn.close()
        self.failure("STATE_INVALID", self.revoke)
        self.assertEqual(self.store.deletions, 0)

    def test_revoke_delete_unknown_stops_without_retry(self):
        self.store.delete_unknown = True
        self.failure("REMOTE_UNKNOWN", self.revoke)
        self.assertEqual(self.store.deletions, 1)
        self.assertEqual(remote.status(str(self.state), self.key)["status"], "revoking")
        self.store.delete_unknown = False
        # next explicit revoke reconciles with a stat first; object still
        # exists so it deletes exactly once more
        revoked = self.revoke()
        self.assertEqual(revoked["status"], "object-deleted")
        self.assertEqual(self.store.deletions, 2)

    def test_revoke_absent_object_needs_no_delete(self):
        self.store._object(self.result["object_key"]).unlink()
        revoked = self.revoke()
        self.assertEqual(revoked["status"], "object-deleted")
        self.assertEqual(self.store.deletions, 0)

    def test_revoke_works_after_inputs_are_gone(self):
        import shutil
        shutil.rmtree(self.root)
        self.assertEqual(self.revoke()["status"], "object-deleted")

    def test_revoked_key_rejected_new_key_available(self):
        self.revoke()
        uploads, signs = self.store.uploads, self.store.signs
        self.failure("TASK_REVOKED", lambda: remote.deliver(
            paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
            config_path=str(self.config), key=self.key, store=self.store))
        self.assertEqual(self.store.uploads, uploads)
        self.assertEqual(self.store.signs, signs)
        fresh = remote.deliver(
            paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
            config_path=str(self.config), key="revoke-2", store=self.store)
        self.assertEqual(fresh["status"], "link-verified")

    def test_revoked_key_rejected_even_after_link_expiry(self):
        self.revoke()
        with unittest.mock.patch("time.time", return_value=self.result["expires_at"] + 2):
            self.failure("TASK_REVOKED", lambda: remote.deliver(
                paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
                config_path=str(self.config), key=self.key, store=self.store))

    def test_cleanup_dry_run_read_only_without_config(self):
        baseline = (self.store.stats, self.store.deletions, self.store.probes)
        preview = remote.cleanup(str(self.state), now=self.retention_expiry() - 1)
        self.assertEqual(preview["schema_version"], 1)
        self.assertEqual(preview["status"], "cleanup-dry-run")
        self.assertEqual(len(preview["items"]), 1)
        item = preview["items"][0]
        self.assertEqual(item["key"], self.key)
        self.assertEqual(item["task_id"], self.result["task_id"])
        self.assertEqual(item["object_key"], self.result["object_key"])
        self.assertFalse(item["eligible"])
        self.assertEqual(item["reason"], "not-due")
        # zero provider calls during the dry run itself (the setUp deliver
        # already issued its own stat, so compare the delta)
        self.assertEqual(
            (self.store.stats, self.store.deletions, self.store.probes), baseline)
        self.assertEqual(remote.status(str(self.state), self.key)["status"],
                         "link-verified")

    def test_cleanup_dry_run_due_and_already_revoked(self):
        self.revoke()
        second = remote.deliver(
            paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
            config_path=str(self.config), key="revoke-2", store=self.store,
            ttl_seconds=3600, retention_days=1)
        preview = remote.cleanup(str(self.state))
        reasons = {item["key"]: item["reason"] for item in preview["items"]}
        self.assertEqual(reasons[self.key], "already-revoked")
        self.assertEqual(reasons["revoke-2"], "not-due")
        due = remote.cleanup(
            str(self.state),
            now=remote.status(str(self.state), "revoke-2")["retention_expires_at"])
        flagged = {i["key"]: i for i in due["items"]}
        self.assertTrue(flagged["revoke-2"]["eligible"])
        self.assertEqual(flagged["revoke-2"]["reason"], "due")
        self.assertEqual(self.store.deletions, 1)  # only the explicit revoke

    def test_cleanup_now_seam_validation(self):
        for bad in (-1, True, 1.5, "10"):
            self.failure("CONFIG_INVALID", lambda bad=bad: remote.cleanup(
                str(self.state), now=bad))

    def test_cleanup_execute_deletes_only_due_owned_tasks(self):
        # a longer retention keeps the second task deterministically not-due
        # at the first task's deadline even if both were created in the
        # same second
        remote.deliver(
            paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
            config_path=str(self.config), key="revoke-2", store=self.store,
            ttl_seconds=3600, retention_days=3650)
        deletions = self.store.deletions
        report = remote.cleanup(str(self.state), str(self.config), dry_run=False,
                                store=self.store, now=self.retention_expiry())
        self.assertEqual(report["status"], "cleanup-complete")
        outcomes = {i["key"]: i for i in report["items"]}
        self.assertEqual(outcomes[self.key]["status"], "object-deleted")
        self.assertEqual(outcomes[self.key]["link_status"], "link-accessible")
        self.assertFalse(outcomes["revoke-2"]["eligible"])
        self.assertEqual(outcomes["revoke-2"]["reason"], "not-due")
        self.assertEqual(self.store.deletions, deletions + 1)
        self.assertEqual(remote.status(str(self.state), self.key)["status"],
                         "object-deleted")

    def test_cleanup_execute_requires_config(self):
        self.failure("CONFIG_INVALID", lambda: remote.cleanup(
            str(self.state), dry_run=False, store=self.store))

    def test_cleanup_execute_identity_mismatch_skipped(self):
        self.write_config({"bucket": "other-bucket"})
        baseline = (self.store.stats, self.store.deletions)
        report = remote.cleanup(str(self.state), str(self.config), dry_run=False,
                                store=self.store, now=self.retention_expiry())
        self.assertEqual(report["status"], "cleanup-complete")
        item = report["items"][0]
        self.assertFalse(item["eligible"])
        self.assertEqual(item["reason"], "identity-mismatch")
        # zero provider calls for the mismatched task (delta from setUp)
        self.assertEqual((self.store.stats, self.store.deletions), baseline)

    def test_cleanup_execute_partial_on_task_failure(self):
        remote.deliver(
            paths=[str(self.root)], root=str(self.root), state_dir=str(self.state),
            config_path=str(self.config), key="revoke-2", store=self.store)
        self.store.delete_unknown = True
        report = remote.cleanup(str(self.state), str(self.config), dry_run=False,
                                store=self.store, now=self.retention_expiry() + 10**9)
        self.assertEqual(report["status"], "cleanup-partial")
        failed = [i for i in report["items"] if i.get("status") == "failed"]
        self.assertEqual(len(failed), 2)
        self.assertTrue(all(i["error"] == "REMOTE_UNKNOWN" for i in failed))
        self.assertEqual(self.store.deletions, 2)

    def test_cleanup_legacy_row_reported_metadata_missing(self):
        import sqlite3
        conn = sqlite3.connect(str(self.state / "remote.sqlite3"))
        conn.execute("UPDATE tasks SET provider_identity = NULL, "
                     "retention_days = NULL, first_upload_at = NULL, "
                     "retention_expires_at = NULL")
        conn.commit()
        conn.close()
        preview = remote.cleanup(str(self.state))
        self.assertEqual(preview["items"][0]["reason"], "metadata-missing")
        self.assertFalse(preview["items"][0]["eligible"])
        report = remote.cleanup(str(self.state), str(self.config), dry_run=False,
                                store=self.store, now=10**12)
        self.assertEqual(report["status"], "cleanup-complete")
        self.assertEqual(report["items"][0]["reason"], "metadata-missing")
        self.assertEqual(self.store.deletions, 0)

    def test_revoke_unknown_key(self):
        self.failure("TASK_NOT_FOUND", lambda: self.revoke(key="absent"))


if __name__ == "__main__":
    unittest.main()
