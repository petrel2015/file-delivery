"""Supplement existing AC-5/AC-6; import no business code during discovery."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name('runtime_probe.py')


class RuntimeAcceptance(unittest.TestCase):
    def probe(self, mode):
        with tempfile.TemporaryDirectory(prefix='fd-acceptance-') as temporary:
            root = Path(temporary).resolve()
            inputs = root / 'outbox'
            inputs.mkdir()
            target = inputs / 'sample.bin'
            payload = bytes(range(256)) * 64
            target.write_bytes(payload)
            report = root / 'probe.json'
            env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            command = [sys.executable, '-I', '-B', str(PROBE), '--project', str(PROJECT),
                       '--input', str(target), '--report', str(report), '--mode', mode]
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=20)
            self.assertTrue(report.exists(), 'Controller probe did not complete: ' + result.stderr)
            observation = json.loads(report.read_text())
            self.assertEqual(observation['network_events'], [], 'AC-5: network operation attempted')
            return result, observation, payload

    def assert_changed(self, mode):
        result, observation, _ = self.probe(mode)
        self.assertIn(result.returncode, (0, 2), 'CLI_EXECUTION_FAILED: ' + result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(observation['read_observed'],
                        'HARNESS_READ_NOT_OBSERVED: investigate reader compatibility; not a business defect verdict')
        self.assertTrue(observation['mutation_performed'], 'HARNESS_MUTATION_NOT_INJECTED')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(value['status'], 'error')
        self.assertEqual(value['error']['code'], 'INPUT_CHANGED')
        self.assertNotIn('files', value)

    def test_ac6_detects_size_change_during_read(self):
        self.assert_changed('grow')

    def test_ac6_detects_same_size_mtime_change_during_read(self):
        self.assert_changed('same-size')

    def test_ac6_unchanged_input_succeeds(self):
        result, observation, payload = self.probe('unchanged')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(observation['mutation_performed'])
        value = json.loads(result.stdout)
        self.assertEqual(value['status'], 'planned')
        self.assertEqual(value['files'][0]['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(value['files'][0]['size_bytes'], len(payload))

    def test_ac5_no_network_operations(self):
        result, observation, _ = self.probe('no-network')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'planned')
        self.assertEqual(observation['network_events'], [])


class ProbeHarnessTests(unittest.TestCase):
    def test_read_injection_supports_equivalent_readers(self):
        for reader in ('open', 'pathlib', 'readinto', 'file-digest', 'descriptor', 'fileio'):
            for mode in ('grow', 'same-size', 'unchanged'):
                with self.subTest(reader=reader, mode=mode), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary).resolve()
                    target = root / 'input'
                    target.write_bytes(b'abc' * 4096)
                    before = target.stat()
                    report = root / 'probe.json'
                    r = subprocess.run([sys.executable, '-I', '-B', str(PROBE), '--project', str(PROJECT),
                        '--input', str(target), '--report', str(report), '--mode', mode,
                        '--self-reader', reader], capture_output=True, text=True, timeout=10)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    observed = json.loads(report.read_text())
                    self.assertTrue(observed['read_observed'])
                    self.assertEqual(observed['mutation_performed'], mode != 'unchanged')
                    self.assertEqual(target.stat().st_size == before.st_size, mode != 'grow')
                    self.assertEqual(target.stat().st_mtime_ns == before.st_mtime_ns, mode == 'unchanged')
