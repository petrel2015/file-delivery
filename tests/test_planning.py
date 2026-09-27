"""Unit tests for the planning module (worker-owned)."""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from file_delivery import errors, planning  # noqa: E402


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "outbox"
        self.root.mkdir()

    def plan(self, paths, root=None):
        return planning.plan(paths, root or self.root)

    def assert_error(self, paths, code, root=None):
        with self.assertRaises(errors.DeliveryError) as ctx:
            self.plan(paths, root)
        self.assertEqual(ctx.exception.code, code)

    def test_manifest_content_and_sorting(self):
        nested = self.root / "dir two"
        nested.mkdir()
        (nested / "b.txt").write_bytes(b"bb")
        (nested / "a.txt").write_bytes(b"aaa")
        top = self.root / "顶层 文件.bin"
        top.write_bytes(bytes(range(64)))
        result = self.plan([nested, top, top])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["status"], "planned")
        self.assertEqual([f["path"] for f in result["files"]],
                         ["dir two/a.txt", "dir two/b.txt", "顶层 文件.bin"])
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(result["total_bytes"], 3 + 2 + 64)
        for entry in result["files"]:
            data = (self.root / entry["path"]).read_bytes()
            self.assertEqual(entry["size_bytes"], len(data))
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())

    def test_overlapping_inputs_deduplicated(self):
        f = self.root / "f.txt"
        f.write_bytes(b"x")
        result = self.plan([f, self.root, f])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["files"][0]["path"], "f.txt")

    def test_empty_file_hashed(self):
        f = self.root / "empty"
        f.write_bytes(b"")
        result = self.plan([f])
        self.assertEqual(result["files"][0]["sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(result["total_bytes"], 0)

    def test_input_not_found(self):
        self.assert_error([self.root / "absent"], errors.INPUT_NOT_FOUND)

    def test_path_not_allowed(self):
        outside = self.root.parent / "elsewhere"
        outside.mkdir()
        f = outside / "f.txt"
        f.write_text("x")
        self.assert_error([f], errors.PATH_NOT_ALLOWED)
        self.assert_error([self.root / ".." / "elsewhere" / "f.txt"], errors.PATH_NOT_ALLOWED)

    def test_symlinks_rejected(self):
        f = self.root / "f.txt"
        f.write_text("data")
        link = self.root / "link"
        link.symlink_to(f)
        self.addCleanup(link.unlink)
        self.assert_error([link], errors.SYMLINK_NOT_ALLOWED)
        self.assert_error([self.root], errors.SYMLINK_NOT_ALLOWED)

    def test_unsupported_file_type_and_empty_selection(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        self.addCleanup(fifo.unlink)
        self.assert_error([fifo], errors.UNSUPPORTED_FILE_TYPE)
        empty = self.root / "empty-dir"
        empty.mkdir()
        self.assert_error([empty], errors.EMPTY_SELECTION)

    def test_sensitive_names_rejected(self):
        for name in [".env", ".env.prod", "host.pem", "server.key",
                     "credentials.yaml", "secret-thing"]:
            with self.subTest(name=name):
                p = self.root / name
                p.write_text("s")
                self.assert_error([self.root], errors.SENSITIVE_PATH)
                p.unlink()
        for name in [".git", ".ssh", ".aws", ".kube"]:
            with self.subTest(name=name):
                d = self.root / name
                d.mkdir()
                (d / "x.txt").write_text("s")
                self.assert_error([d / "x.txt"], errors.SENSITIVE_PATH)
                shutil.rmtree(d)

    def test_symlink_component_hidden_by_dotdot_rejected(self):
        f = self.root / "safe.txt"
        f.write_text("x")
        alias = self.root / "alias"
        alias.symlink_to(self.root)
        self.addCleanup(alias.unlink)
        self.assert_error([self.root / "alias" / ".." / "safe.txt"],
                          errors.SYMLINK_NOT_ALLOWED)
        self.assert_error([f], errors.INVALID_ROOT,
                          root=self.root.parent / "alias" / "..")

    def test_sensitive_root_component_rejected(self):
        sensitive_root = self.root.parent / ".ssh"
        sensitive_root.mkdir()
        self.addCleanup(shutil.rmtree, sensitive_root)
        f = sensitive_root / "ordinary.txt"
        f.write_text("x")
        self.assert_error([f], errors.SENSITIVE_PATH, root=sensitive_root)

    def test_invalid_roots(self):
        self.assert_error([self.root / "f"], errors.INVALID_ROOT, root=self.root / "missing")
        f = self.root / "f"
        f.write_text("x")
        self.assert_error([f], errors.INVALID_ROOT, root=f)
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root)
        self.addCleanup(alias.unlink)
        self.assert_error([f], errors.INVALID_ROOT, root=alias)

    def test_inputs_not_modified(self):
        f = self.root / "f.txt"
        f.write_bytes(b"stable")
        before = (f.stat().st_mtime_ns, f.stat().st_size, f.read_bytes())
        self.plan([f])
        self.assertEqual((f.stat().st_mtime_ns, f.stat().st_size, f.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
