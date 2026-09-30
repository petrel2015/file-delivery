"""Offline transport diagnostics and secret-boundary regressions."""
import json
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

import requests
from file_delivery import errors, remote
from file_delivery.qiniu_store import QiniuStore


class DiagnosticsTests(unittest.TestCase):
    def store(self, exception):
        class Session:
            calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                raise exception
        store = QiniuStore(access_key='synthetic-ak', secret_key='synthetic-sk',
                           bucket='test-bucket', region='z0',
                           download_domain='https://dl.example.com', session=Session())
        return store

    def test_transport_categories_survive_orchestration_without_secrets(self):
        for cls, reason in [(requests.exceptions.ConnectTimeout, 'connect_timeout'),
                            (requests.exceptions.ReadTimeout, 'read_timeout'),
                            (requests.exceptions.Timeout, 'timeout'),
                            (requests.exceptions.ConnectionError, 'connection_error'),
                            (requests.exceptions.SSLError, 'tls_error'),
                            (requests.exceptions.ChunkedEncodingError, 'stream_interrupted'),
                            (RuntimeError, 'transport_error')]:
            with self.subTest(cls=cls):
                store = self.store(cls('SECRET https://private.example/?token=PRIVATE'))
                with patch('file_delivery.qiniu_store.time.perf_counter', side_effect=[100, 131.125]):
                    with self.assertRaises(errors.DeliveryError) as caught:
                        remote._provider('upload', store._request, 'POST', 'https://up-z0.qiniup.com')
                exc = caught.exception
                self.assertEqual(exc.code, 'REMOTE_UNKNOWN')
                self.assertEqual(exc.diagnostics['reason'], reason)
                self.assertEqual(exc.diagnostics['stage'], 'upload')
                self.assertEqual(exc.diagnostics['elapsed_seconds'], 31.125)
                self.assertEqual(exc.diagnostics['timeout_seconds'], 30)
                self.assertEqual(store._session.calls, 1)
                output = exc.message + json.dumps(exc.diagnostics)
                for secret in ('SECRET', 'PRIVATE', 'private.example'):
                    self.assertNotIn(secret, output)

    def test_arbitrary_provider_diagnostics_are_filtered(self):
        def fail():
            raise errors.DeliveryError('REMOTE_UNKNOWN', 'SECRET', diagnostics={
                'reason': 'SECRET', 'exception_type': 'SECRET', 'url': 'SECRET',
                'elapsed_seconds': 'SECRET', 'stage': 'upload'})
        with self.assertRaises(errors.DeliveryError) as caught:
            remote._provider('upload', fail)
        self.assertEqual(caught.exception.diagnostics, {'stage': 'upload'})
        self.assertNotIn('SECRET', caught.exception.message)

    def test_http_server_error(self):
        store = self.store(RuntimeError())
        exc = store._map_status(503, 'upload')
        self.assertEqual(exc.diagnostics, {'reason': 'http_server_error', 'http_status': 503})

    def test_cli_prints_diagnostics_and_retains_exit_code(self):
        from file_delivery import cli
        exc = errors.DeliveryError('REMOTE_UNKNOWN', 'upload failed: read_timeout',
                                   diagnostics={'reason': 'read_timeout', 'stage': 'upload'})
        output = io.StringIO()
        with patch('file_delivery.remote.deliver', side_effect=exc), redirect_stdout(output):
            code = cli.main(['deliver-qiniu', 'dummy', '--root', '/tmp/input',
                             '--state-dir', '/tmp/state', '--config', '/tmp/config', '--key', 'test'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['error']['diagnostics'], exc.diagnostics)
