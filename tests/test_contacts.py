"""Worker tests for contact alias resolution and mailbox validation."""

import json
import tempfile
import unittest
from pathlib import Path

from file_delivery import contacts, errors


class AddrSpecTests(unittest.TestCase):
    def test_valid_direct_mailbox_canonicalized(self):
        self.assertEqual(contacts.resolve("User.Name+Tag@Example.TEST"),
                         "User.Name+Tag@example.test")

    def test_invalid_mailboxes_rejected(self):
        for bad in (
            "x@example.test\r\nBcc:v@example.test",
            "A <a@example.test>",
            "a@example.test,b@example.test",
            "a@example.test\x00",
            "a@example.test\x7f",
            "a..b@example.test",
            ".a@example.test",
            "a.@example.test",
            "a@-bad.test",
            "a@bad-.test",
            "a@bad..test",
            "a @example.test",
            "a@é.test",
            '"quoted local"@example.test',
            "a@",
            "@example.test",
            "",
            "a@" + "b" * 250 + ".test",
            "local" * 20 + "@example.test",
        ):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(errors.DeliveryError) as caught:
                    contacts.resolve(bad)
                self.assertEqual(caught.exception.code, errors.RECIPIENT_INVALID)


class ContactsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.path = self.base / "contacts.json"

    def write(self, payload, mode=0o600):
        if isinstance(payload, str):
            self.path.write_text(payload, encoding="utf-8")
        else:
            self.path.write_text(json.dumps(payload, ensure_ascii=False),
                                 encoding="utf-8")
        self.path.chmod(mode)
        return self.path

    def test_alias_resolution_unicode_exact(self):
        self.write({"schema_version": 1, "contacts": {"打印店": "Shop@EXAMPLE.test"}})
        self.assertEqual(contacts.resolve("打印店", str(self.path)),
                         "Shop@example.test")
        # Aliases are case-sensitive and exact.
        with self.assertRaises(errors.DeliveryError) as caught:
            contacts.resolve("打印店 ", str(self.path))
        self.assertEqual(caught.exception.code, errors.CONTACT_NOT_FOUND)

    def test_alias_without_contacts_file(self):
        with self.assertRaises(errors.DeliveryError) as caught:
            contacts.resolve("打印店")
        self.assertEqual(caught.exception.code, errors.CONTACTS_INVALID)

    def test_unknown_alias(self):
        self.write({"schema_version": 1, "contacts": {"打印店": "Shop@example.test"}})
        with self.assertRaises(errors.DeliveryError) as caught:
            contacts.resolve("missing", str(self.path))
        self.assertEqual(caught.exception.code, errors.CONTACT_NOT_FOUND)

    def test_duplicate_keys_rejected(self):
        self.write('{"schema_version":1,"contacts":{"x":"x@example.test",'
                   '"x":"y@example.test"}}')
        with self.assertRaises(errors.DeliveryError) as caught:
            contacts.resolve("x", str(self.path))
        self.assertEqual(caught.exception.code, errors.CONTACTS_INVALID)

    def test_unknown_fields_and_shapes_rejected(self):
        for payload in (
            {"schema_version": 1, "contacts": {}, "extra": 1},
            {"schema_version": 1},
            {"schema_version": 2, "contacts": {}},
            {"schema_version": 1, "contacts": ["a@example.test"]},
            {"schema_version": 1, "contacts": {"a": ["a@example.test"]}},
            {"schema_version": 1, "contacts": {"a": "not-an-address"}},
            {"schema_version": 1, "contacts": {"": "a@example.test"}},
            {"schema_version": 1, "contacts": {"a\x00": "a@example.test"}},
            "[]",
            "not json",
        ):
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(errors.DeliveryError) as caught:
                    contacts.resolve("a", str(self.path))
                self.assertEqual(caught.exception.code, errors.CONTACTS_INVALID)

    def test_permissions_and_symlinks_rejected(self):
        self.write({"schema_version": 1, "contacts": {"a": "a@example.test"}},
                   mode=0o644)
        with self.assertRaises(errors.DeliveryError) as caught:
            contacts.resolve("a", str(self.path))
        self.assertEqual(caught.exception.code, errors.CONTACTS_INVALID)
        self.write({"schema_version": 1, "contacts": {"a": "a@example.test"}})
        outside = self.base / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        outside.chmod(0o600)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(errors.DeliveryError) as caught:
            contacts.resolve("a", str(self.path))
        self.assertEqual(caught.exception.code, errors.CONTACTS_INVALID)


if __name__ == "__main__":
    unittest.main()
