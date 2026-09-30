"""Worker tests for the FD-005 notification core (synthetic transport only)."""

import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "fd005_worker_remote_fixture", PROJECT / "tests/fd004b/test_fd004b_remote.py")
_FIXTURE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE)


def _deps_available() -> bool:
    return importlib.util.find_spec("qiniu") is not None \
        and importlib.util.find_spec("pyzipper") is not None


class FakeTransport:
    def __init__(self, fail=None):
        self.fail = fail
        self.prepared = 0
        self.submitted = 0
        self.messages = []

    def prepare(self, config, from_addr, to_addr):
        self.prepared += 1
        if self.fail == "prepare":
            raise OSError("synthetic prepare failure")

    def submit(self, message, from_addr, to_addr):
        self.submitted += 1
        self.messages.append(message)
        if self.fail == "submit":
            raise TimeoutError("synthetic submit failure")
        return {"status": "channel-accepted", "smtp_code": 250}

    def close(self):
        pass


@unittest.skipUnless(_deps_available(), "qiniu/pyzipper not installed")
class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _FIXTURE.RemoteTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.remote = self.fixture.deliver()
        self.config = self.fixture.base / "smtp.json"
        self.password = self.fixture.base / "smtp-password.txt"
        self.password.write_text("worker-smtp-secret\n")
        self.password.chmod(0o600)
        self.config.write_text(json.dumps({
            "schema_version": 1, "host": "smtp.example.test", "port": 465,
            "tls": "implicit", "username": "sender@example.test",
            "password_file": self.password.name,
            "from_address": "sender@example.test", "timeout_seconds": 3}))
        self.config.chmod(0o600)
        self.contacts = self.fixture.base / "contacts.json"
        self.contacts.write_text(json.dumps(
            {"schema_version": 1, "contacts": {"打印店": "Shop@example.test"}},
            ensure_ascii=False), encoding="utf-8")
        self.contacts.chmod(0o600)
        self.transport = FakeTransport()

    def send(self, **changes):
        from file_delivery import notification
        args = dict(state_dir=str(self.fixture.state),
                    delivery_key=self.fixture.key,
                    smtp_config=str(self.config),
                    recipient="打印店",
                    notification_key="mail-1",
                    contacts_path=str(self.contacts),
                    transport=self.transport)
        args.update(changes)
        return notification.send(**args)

    def status(self):
        from file_delivery import notification
        return notification.status(str(self.fixture.state), "mail-1")

    def assert_failure(self, code, fn):
        from file_delivery.errors import DeliveryError
        with self.assertRaises(DeliveryError) as caught:
            fn()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn("worker-smtp-secret", str(caught.exception))
        return caught.exception

    def test_send_surface_and_message(self):
        result = self.send()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "channel-accepted")
        self.assertEqual(result["delivery_task_id"], self.remote["task_id"])
        self.assertFalse(result["reused"])
        self.assertEqual(result["receipt_status"], "unverified")
        self.assertEqual(result["read_status"], "unverified")
        message = self.transport.messages[0]
        self.assertEqual(message.get_all("To"), ["Shop@example.test"])
        self.assertEqual(message.get_all("From"), ["sender@example.test"])
        self.assertEqual(message["Message-ID"], result["message_id"])
        body = message.get_content()
        handoff = json.loads(Path(self.remote["handoff_file"]).read_text())
        self.assertIn(handoff["url"], body)
        self.assertIn(str(self.remote["archive_size"]), body)
        with sqlite3.connect(self.fixture.state / "notifications.sqlite3") as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn("worker-smtp-secret", dump)
        self.assertNotIn(handoff["url"], dump)
        self.assertEqual((self.fixture.state / "notifications.sqlite3")
                         .stat().st_mode & 0o777, 0o600)

    def test_same_key_reuse_zero_transport(self):
        first = self.send()
        again = self.send()
        self.assertTrue(again["reused"])
        self.assertEqual(first["message_id"], again["message_id"])
        self.assertEqual(self.transport.submitted, 1)

    def test_conflict_on_different_recipient(self):
        self.send()
        self.assert_failure("IDEMPOTENCY_CONFLICT",
                            lambda: self.send(recipient="other@example.test"))

    def test_prepare_failure_retryable(self):
        self.transport.fail = "prepare"
        self.assert_failure("IO_ERROR", self.send)
        saved = self.status()
        self.assertEqual(saved["status"], "failed-before-send")
        self.assertTrue(saved["retryable"])
        self.transport.fail = None
        self.assertEqual(self.send()["status"], "channel-accepted")
        self.assertEqual(self.transport.submitted, 1)

    def test_submit_failure_unknown_never_resends(self):
        self.transport.fail = "submit"
        self.assert_failure("SMTP_UNKNOWN", self.send)
        self.transport.fail = None
        self.assert_failure("SMTP_UNKNOWN", self.send)
        self.assertEqual(self.transport.submitted, 1)
        self.assertEqual(self.transport.prepared, 1)
        saved = self.status()
        self.assertEqual(saved["status"], "unknown")
        self.assertFalse(saved["retryable"])

    def test_invalid_config_before_transport(self):
        values = json.loads(self.config.read_text())
        values["port"] = 0
        self.config.write_text(json.dumps(values))
        self.assert_failure("CONFIG_INVALID", self.send)
        self.assertEqual(self.transport.prepared, 0)

    def test_unknown_delivery_key(self):
        self.assert_failure("TASK_NOT_FOUND",
                            lambda: self.send(delivery_key="absent"))

    def test_invalid_notification_key(self):
        self.assert_failure("INVALID_KEY",
                            lambda: self.send(notification_key="bad key!"))

    def test_status_offline_and_missing(self):
        self.send()
        self.config.unlink()
        self.contacts.unlink()
        self.assertEqual(self.status()["status"], "channel-accepted")
        from file_delivery import notification
        with self.assertRaises(Exception) as caught:
            notification.status(str(self.fixture.state), "missing")
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
