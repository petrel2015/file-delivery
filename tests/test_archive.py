"""Unit tests for the encrypted archive slice (worker-owned)."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from file_delivery import archive, errors

try:
    import pyzipper
except ImportError:  # controller environment provides pyzipper; keep importable otherwise
    pyzipper = None

PYZIPPER_AVAILABLE = pyzipper is not None

CONTENT = {
    "a b.txt": b"payload payload",
    "empty.dat": b"",
    "nested/ünïcode-文件.txt": "内容 with spaces\n".encode("utf-8"),
}


def _make_root(base: Path) -> Path:
    root = base / "root"
    (root / "nested").mkdir(parents=True)
    for rel, data in CONTENT.items():
        path = root / rel
        path.write_bytes(data)
    return root


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(PYZIPPER_AVAILABLE, "pyzipper not installed")
class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = _make_root(self.base)
        self.out = self.base / "bundle"

    def test_pack_creates_verified_bundle(self):
        result = archive.pack([str(self.root)], str(self.root), str(self.out))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "packaged")
        self.assertEqual(result["bundle_dir"], str(self.out))
        self.assertEqual(result["file_count"], len(CONTENT))
        self.assertEqual(result["total_bytes"], sum(len(d) for d in CONTENT.values()))
        self.assertEqual(sorted(os.listdir(self.out)), ["archive.zip", "manifest.json", "password.txt"])
        self.assertEqual(result["archive_sha256"], _sha((self.out / "archive.zip").read_bytes()))
        # No password anywhere in the result.
        password = (self.out / "password.txt").read_text().strip()
        self.assertGreaterEqual(len(password) * 6, 128)  # urlsafe chars carry 6 bits each
        self.assertNotIn(password, json.dumps(result))
        # No partial staging artifacts left behind.
        siblings = [p.name for p in self.base.iterdir()]
        self.assertNotIn(self.out.name + ".tmp-", "".join(siblings))

    def test_bundle_permissions(self):
        archive.pack([str(self.root)], str(self.root), str(self.out))
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        for name in ("archive.zip", "manifest.json", "password.txt"):
            mode = stat.S_IMODE((self.out / name).stat().st_mode)
            self.assertEqual(mode, 0o600, name)

    def test_manifest_matches_planner_output(self):
        from file_delivery import planning
        planned = planning.plan([str(self.root)], str(self.root))
        archive.pack([str(self.root), str(self.root / "nested")], str(self.root), str(self.out))
        stored = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(planned, stored)

    def test_independent_decryption_matches(self):
        result = archive.pack([str(self.root)], str(self.root), str(self.out))
        password = (self.out / "password.txt").read_text().strip()
        with pyzipper.AESZipFile(self.out / "archive.zip") as zf:
            zf.setpassword(password.encode())
            names = zf.namelist()
            self.assertEqual(set(names), set(CONTENT))
            for name, data in CONTENT.items():
                self.assertEqual(zf.read(name), data)
                info = zf.getinfo(name)
                # pyzipper decodes method 99 into the real compression method
                # and exposes AES strength instead (3 == 256-bit keys).
                self.assertEqual(getattr(info, "wz_aes_strength", None), 3)

    def test_archive_contains_no_plaintext(self):
        archive.pack([str(self.root)], str(self.root), str(self.out))
        blob = (self.out / "archive.zip").read_bytes()
        for data in CONTENT.values():
            if len(data) >= 8:
                self.assertNotIn(data, blob)

    def test_output_inside_root_rejected(self):
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.pack([str(self.root)], str(self.root), str(self.root / "out"))
        self.assertEqual(ctx.exception.code, errors.OUTPUT_NOT_ALLOWED)
        self.assertFalse((self.root / "out").exists())

    def test_output_equal_root_rejected(self):
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.pack([str(self.root)], str(self.root), str(self.root))
        self.assertEqual(ctx.exception.code, errors.OUTPUT_NOT_ALLOWED)

    def test_existing_output_rejected(self):
        self.out.mkdir()
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.pack([str(self.root)], str(self.root), str(self.out))
        self.assertEqual(ctx.exception.code, errors.OUTPUT_EXISTS)
        self.assertEqual(os.listdir(self.out), [])  # untouched

    def test_missing_parent_rejected(self):
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.pack([str(self.root)], str(self.root), str(self.base / "nope" / "bundle"))
        self.assertEqual(ctx.exception.code, errors.IO_ERROR)

    def test_input_changed_aborts_without_bundle(self):
        from file_delivery import planning
        good = planning.plan([str(self.root)], str(self.root))
        stale = json.loads(json.dumps(good))
        stale["files"][0]["sha256"] = "0" * 64
        with mock.patch.object(archive.planning, "plan", return_value=stale):
            with self.assertRaises(errors.DeliveryError) as ctx:
                archive.pack([str(self.root)], str(self.root), str(self.out))
        self.assertEqual(ctx.exception.code, errors.INPUT_CHANGED)
        self.assertFalse(self.out.exists())
        self.assertEqual([p.name for p in self.base.iterdir() if p.is_dir()], ["root"])

    def test_overlapping_inputs_deduplicated(self):
        result = archive.pack(
            [str(self.root / "a b.txt"), str(self.root)],
            str(self.root), str(self.out))
        self.assertEqual(result["file_count"], len(CONTENT))


@unittest.skipUnless(PYZIPPER_AVAILABLE, "pyzipper not installed")
class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = _make_root(self.base)
        self.out = self.base / "bundle"
        self.result = archive.pack([str(self.root)], str(self.root), str(self.out))
        self.password = (self.out / "password.txt").read_text().strip()

    def test_verify_success(self):
        result = archive.verify(str(self.out))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["file_count"], self.result["file_count"])
        self.assertEqual(result["total_bytes"], self.result["total_bytes"])
        self.assertEqual(result["archive_sha256"], self.result["archive_sha256"])

    def test_verify_custom_password_file(self):
        pf = self.base / "pw.txt"
        pf.write_text(self.password)
        result = archive.verify(str(self.out), password_file=str(pf))
        self.assertEqual(result["status"], "verified")

    def test_verify_wrong_password(self):
        pf = self.base / "wrong.txt"
        pf.write_text("not-the-password")
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.verify(str(self.out), password_file=str(pf))
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)
        self.assertNotIn(self.password, ctx.exception.message)

    def test_verify_corrupted_archive(self):
        blob = bytearray((self.out / "archive.zip").read_bytes())
        blob[len(blob) // 2] ^= 0xFF
        (self.out / "archive.zip").write_bytes(bytes(blob))
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.verify(str(self.out))
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)

    def test_verify_tampered_manifest_hash(self):
        path = self.out / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"][0]["sha256"] = "f" * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.verify(str(self.out))
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)

    def test_verify_missing_entry(self):
        # Rebuild a valid AES archive that omits one file.
        alt = self.base / "alt.zip"
        with pyzipper.AESZipFile(alt, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(self.password.encode())
            for name in list(CONTENT)[:-1]:
                zf.writestr(name, CONTENT[name])
        shutil.copy(alt, self.out / "archive.zip")
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.verify(str(self.out))
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)

    def test_verify_missing_bundle(self):
        with self.assertRaises(errors.DeliveryError) as ctx:
            archive.verify(str(self.base / "absent"))
        self.assertEqual(ctx.exception.code, errors.VERIFY_FAILED)

    def test_verify_does_not_alter_bundle(self):
        before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in self.out.iterdir()}
        archive.verify(str(self.out))
        after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                 for p in self.out.iterdir()}
        self.assertEqual(before, after)


class DependencyTests(unittest.TestCase):
    def test_missing_dependency_raises_dependency_missing(self):
        if pyzipper is None:
            self.skipTest("pyzipper absence would be real, not simulated")
        saved = __import__("sys").modules.get("pyzipper")
        __import__("sys").modules["pyzipper"] = None  # forces ImportError on import
        try:
            with self.assertRaises(errors.DeliveryError) as ctx:
                archive.pack(["x"], "/tmp", "/tmp/out")
            self.assertEqual(ctx.exception.code, errors.DEPENDENCY_MISSING)
        finally:
            if saved is None:
                __import__("sys").modules.pop("pyzipper", None)
            else:
                __import__("sys").modules["pyzipper"] = saved


if __name__ == "__main__":
    unittest.main()
