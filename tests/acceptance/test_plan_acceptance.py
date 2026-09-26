"""Controller-owned, frozen CLI acceptance for FD-001. No business implementation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]


class PlanAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'outbox'
        self.root.mkdir()
        self.file = self.root / '报告 one.txt'
        self.file.write_bytes(b'example\x00payload')

    def call(self, paths, root=None):
        env = dict(os.environ, PYTHONPATH=str(PROJECT / 'src'), PYTHONDONTWRITEBYTECODE='1')
        args = [sys.executable, '-B', '-m', 'file_delivery', 'plan', *map(str, paths), '--root', str(root or self.root), '--json']
        r = subprocess.run(args, cwd=PROJECT, env=env, capture_output=True, text=True, timeout=15)
        self.assertTrue(r.stdout.strip(), (r.returncode, r.stderr))
        result = json.loads(r.stdout)  # Reject extra logs or multiple JSON objects.
        return r, result

    def error(self, paths, code, root=None):
        r, result = self.call(paths, root)
        self.assertEqual(r.returncode, 2, (r.stdout, r.stderr))
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], code)
        self.assertIsInstance(result['error']['message'], str)
        self.assertNotIn('files', result)
        self.assertNotIn('example\x00payload', r.stdout + r.stderr)

    def test_ac2_manifest_exact_content_and_overlap(self):
        nested = self.root / 'nested'
        nested.mkdir()
        (nested / 'a.bin').write_bytes(bytes(range(256)))
        (nested / 'zero').write_bytes(b'')
        r, result = self.call([self.file, self.root, self.file])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['status'], 'planned')
        expected = []
        for p in sorted([self.file, nested / 'a.bin', nested / 'zero'], key=lambda p: p.relative_to(self.root).as_posix()):
            data = p.read_bytes()
            expected.append(dict(path=p.relative_to(self.root).as_posix(), size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        self.assertEqual(result['files'], expected)
        self.assertEqual(result['file_count'], 3)
        self.assertEqual(result['total_bytes'], sum(e['size_bytes'] for e in expected))

    def test_ac3_missing(self):
        self.error([self.file, self.root / 'absent'], 'INPUT_NOT_FOUND')

    def test_ac3_outside_and_prefix_collision(self):
        outside = self.root.parent / 'outbox-other'
        outside.mkdir()
        f = outside / 'safe.txt'
        f.write_text('outside')
        self.error([f], 'PATH_NOT_ALLOWED')
        self.error([self.root / '..' / 'outbox-other' / 'safe.txt'], 'PATH_NOT_ALLOWED')

    def test_ac3_symlinks(self):
        for name, target in [('file-link', self.file), ('broken', self.root / 'absent'), ('dir-link', self.root)]:
            p = self.root / name
            p.symlink_to(target)
            self.error([p], 'SYMLINK_NOT_ALLOWED')
            self.error([self.root], 'SYMLINK_NOT_ALLOWED')
            p.unlink()

    def test_ac3_symlink_parent(self):
        d = self.root / 'real'
        d.mkdir()
        f = d / 'safe.txt'
        f.write_text('data')
        link = self.root / 'alias'
        link.symlink_to(d)
        self.error([link / 'safe.txt'], 'SYMLINK_NOT_ALLOWED')

    def test_ac3_special_and_empty(self):
        p = self.root / 'pipe'
        os.mkfifo(p)
        self.error([p], 'UNSUPPORTED_FILE_TYPE')
        p.unlink()
        empty = self.root / 'empty'
        empty.mkdir()
        self.error([empty], 'EMPTY_SELECTION')

    def test_ac4_sensitive_names_and_directories(self):
        for name in ['.env', '.ENV.production', 'private.PEM', 'private.Key', 'Credentials.json', 'SECRET-token']:
            with self.subTest(name=name):
                p = self.root / name
                p.write_text('sensitive')
                self.error([self.root], 'SENSITIVE_PATH')
                p.unlink()
        for name in ['.git', '.SSH', '.aws', '.kube']:
            with self.subTest(name=name):
                p = self.root / name
                p.mkdir()
                (p / 'safe.txt').write_text('sensitive')
                self.error([p / 'safe.txt'], 'SENSITIVE_PATH')
                (p / 'safe.txt').unlink()
                p.rmdir()

    def test_ac4_invalid_roots(self):
        for root in [self.root / 'missing', self.file]:
            self.error([self.file], 'INVALID_ROOT', root)
        alias = self.root.parent / 'alias'
        alias.symlink_to(self.root)
        self.error([alias / self.file.name], 'INVALID_ROOT', alias)
        child = self.root / 'child'
        child.mkdir()
        self.error([alias / 'child'], 'INVALID_ROOT', alias / 'child')

    def test_ac6_io_error(self):
        # CPython audit hooks cover builtins.open, pathlib and os.open without
        # constraining which of those equivalent implementations the worker uses.
        script = """import os, runpy, sys
blocked = os.path.abspath(sys.argv[1])
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)) and os.path.abspath(os.fsdecode(args[0])) == blocked:
        raise PermissionError('controller injected unreadable input')
sys.addaudithook(audit)
sys.argv = ['file-delivery', 'plan', blocked, '--root', sys.argv[2], '--json']
runpy.run_module('file_delivery', run_name='__main__')
"""
        env = dict(os.environ, PYTHONPATH=str(PROJECT / 'src'), PYTHONDONTWRITEBYTECODE='1')
        r = subprocess.run([sys.executable, '-B', '-c', script, str(self.file), str(self.root)], env=env, cwd=PROJECT, capture_output=True, text=True, timeout=15)
        self.assertEqual(r.returncode, 2, r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'IO_ERROR')

    def test_ac5_no_input_modification(self):
        def snapshot():
            return [(p.relative_to(self.root).as_posix(), p.stat().st_mode, p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in sorted(self.root.rglob('*'))]
        before = snapshot()
        r, _ = self.call([self.root])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(snapshot(), before)


if __name__ == '__main__':
    unittest.main()
