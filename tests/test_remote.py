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
        self.unknown = False
        self.conflicting = False

    def _object(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".object")

    def ensure_private(self):
        pass

    def stat(self, key):
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


if __name__ == "__main__":
    unittest.main()
