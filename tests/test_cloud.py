import hashlib
import asyncio
import importlib.util
import io
import json
import sqlite3
import time
from pathlib import Path
import unittest
from unittest import mock

from file_delivery import cloud, errors, remote

spec = importlib.util.spec_from_file_location('cloud_fixture', Path(__file__).parent / 'fd004b/test_fd004b_remote.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.RemoteTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.result = remote.deliver([str(self.f.root)], str(self.f.root), self.f.state,
            self.f.config, self.f.key, store=self.f.store)
        self.f.store.signed_url_at = mock.Mock(side_effect=lambda key, deadline:
            {'url': f'https://files.example.com/{key}?e={deadline}&token=synthetic-renewed', 'expires_at': deadline})
        self.f.store.verify_link = mock.Mock(return_value={'sha256': self.result['archive_sha256'], 'size': self.result['archive_size']})

    def renew(self, **kwargs):
        return cloud.renew_link(self.f.state, self.f.config, self.f.key, 'link-v2',
            ttl_seconds=kwargs.pop('ttl_seconds', 3600), store=self.f.store, **kwargs)

    def test_renew_immutable_and_no_repack_upload(self):
        handoff = Path(self.result['handoff_file']).read_bytes()
        password = Path(self.result['password_file']).read_bytes()
        self.f.input.unlink()
        first = self.renew(); second = self.renew()
        self.assertFalse(first['reused']); self.assertTrue(second['reused'])
        self.assertEqual(first['handoff_file'], second['handoff_file'])
        self.assertNotEqual(first['handoff_file'], self.result['handoff_file'])
        self.assertEqual(Path(self.result['handoff_file']).read_bytes(), handoff)
        self.assertEqual(Path(self.result['password_file']).read_bytes(), password)
        self.assertEqual(Path(first['handoff_file']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.f.store.count('upload'), 1)
        self.assertEqual(self.f.store.signed_url_at.call_count, 1)
        with sqlite3.connect(self.f.state / cloud.LINKS_DB) as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn('synthetic-renewed', dump)
        self.assertNotIn(password.decode().strip(), dump)
        with self.assertRaises(errors.DeliveryError) as caught: self.renew(ttl_seconds=7200)
        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')

    def test_renew_recovery_after_handoff_write(self):
        original = remote._write_handoff
        def interrupt(path, payload):
            original(path, payload)
            raise OSError('synthetic failure after durable write')
        with mock.patch.object(remote, '_write_handoff', side_effect=interrupt):
            with self.assertRaises(errors.DeliveryError): self.renew()
        before = next((self.f.state / cloud.LINKS_DIR).rglob('link-v2.json')).read_bytes()
        result = self.renew()
        self.assertTrue(result['reused'])
        self.assertEqual(Path(result['handoff_file']).read_bytes(), before)
        self.assertEqual(self.f.store.count('upload'), 1)

    def test_renew_retention_revocation_and_artifact_tamper(self):
        with self.assertRaises(errors.DeliveryError) as caught: self.renew(ttl_seconds=True)
        self.assertEqual(caught.exception.code, 'CONFIG_INVALID')
        with sqlite3.connect(self.f.state / remote.DB_NAME) as db:
            db.execute('UPDATE tasks SET retention_expires_at=?', (int(time.time()) + 100,))
        with self.assertRaises(errors.DeliveryError) as caught: self.renew()
        self.assertEqual(caught.exception.code, 'CONFIG_INVALID')
        with sqlite3.connect(self.f.state / remote.DB_NAME) as db:
            db.execute('UPDATE tasks SET retention_expires_at=?', (int(time.time()) + 10000,))
        result = self.renew(); Path(result['handoff_file']).write_text('{}')
        with self.assertRaises(errors.DeliveryError) as caught: self.renew()
        self.assertEqual(caught.exception.code, 'STATE_INVALID')
        with sqlite3.connect(self.f.state / remote.DB_NAME) as db:
            db.execute("UPDATE tasks SET state='object-deleted'")
        with self.assertRaises(errors.DeliveryError) as caught: self.renew()
        self.assertEqual(caught.exception.code, 'TASK_REVOKED')

    def test_link_key_cannot_select_another_delivery(self):
        self.renew()
        task = dict(cloud.notification._load_remote_task(self.f.state, self.f.key))
        task['key'] = 'another-key'
        with self.assertRaises(errors.DeliveryError) as caught:
            cloud.apply_link_version(self.f.state, task, 'link-v2')
        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')

    def test_live_list_correlates_manifest_and_paginates(self):
        page = {'items': [{'object_key': self.result['object_key'], 'size': self.result['archive_size'],
                          'etag': 'synthetic', 'uploaded_at': 1},
                         {'object_key': 'external.txt', 'size': 3, 'etag': 'other', 'uploaded_at': 2}],
                'next_marker': 'next'}
        self.f.store.list_objects = mock.Mock(return_value=page)
        result = cloud.list_files(self.f.config, self.f.state, query='报告', store=self.f.store)
        self.assertEqual(result['source'], 'live-qiniu')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['delivery_key'], self.f.key)
        self.assertEqual(result['items'][0]['original_files'], ['报告.txt'])
        self.assertEqual(result['next_marker'], 'next')
        result = cloud.list_files(self.f.config, self.f.state, marker='next', store=self.f.store)
        self.assertEqual(result['items'][1]['ownership'], 'unmanaged')
        self.f.store.list_objects.assert_called_with(prefix='', marker='next', limit=100)

    def test_foreign_destination_does_not_claim_ownership(self):
        self.f.store.list_objects = mock.Mock(return_value={'items': [{'object_key': self.result['object_key']}], 'next_marker': ''})
        self.f.values['bucket'] = 'other-bucket'; self.f.write_config()
        result = cloud.list_files(self.f.config, self.f.state, store=self.f.store)
        self.assertEqual(result['items'][0]['ownership'], 'unmanaged')

    def test_manifest_tamper_stops_before_file_selection(self):
        self.f.store.list_objects = mock.Mock(return_value={
            'items': [{'object_key': self.result['object_key']}], 'next_marker': ''})
        path = Path(self.result['password_file']).parent / 'manifest.json'
        path.write_text('{}')
        with self.assertRaises(errors.DeliveryError) as caught:
            cloud.list_files(self.f.config, self.f.state, store=self.f.store)
        self.assertEqual(caught.exception.code, 'STATE_INVALID')
        self.f.store.list_objects.assert_called_once()

    def test_download_reuses_archive_without_source_and_never_overwrites(self):
        def stream(key, handle, sha, size):
            handle.write(self.f.store.object_path(key).read_bytes())
        self.f.store.download_to = mock.Mock(side_effect=stream)
        self.f.input.unlink()
        output = self.f.base / '下载.zip'
        result = cloud.download(self.f.state, self.f.config, self.f.key, output, store=self.f.store)
        self.assertEqual(result['status'], 'downloaded-verified')
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.result['archive_sha256'])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(errors.DeliveryError) as caught:
            cloud.download(self.f.state, self.f.config, self.f.key, output, store=self.f.store)
        self.assertEqual(caught.exception.code, 'OUTPUT_EXISTS')
        self.assertEqual(self.f.store.count('upload'), 1)
        self.assertEqual(self.f.store.download_to.call_count, 1)

    def test_download_bad_stream_leaves_no_partial_file(self):
        self.f.store.download_to = mock.Mock(side_effect=lambda key, handle, sha, size: handle.write(b'bad'))
        output = self.f.base / 'download.zip'
        with self.assertRaises(errors.DeliveryError) as caught:
            cloud.download(self.f.state, self.f.config, self.f.key, output, store=self.f.store)
        self.assertEqual(caught.exception.code, 'REMOTE_INTEGRITY')
        self.assertFalse(output.exists())
        self.assertFalse(list(output.parent.glob('.file-delivery-download-*')))

    def test_download_destination_symlink_and_wrong_config_rejected(self):
        self.f.store.download_to = mock.Mock()
        output = self.f.base / 'download.zip'; output.symlink_to(self.f.input)
        with self.assertRaises(errors.DeliveryError):
            cloud.download(self.f.state, self.f.config, self.f.key, output, store=self.f.store)
        output.unlink(); self.f.values['bucket'] = 'wrong-bucket'; self.f.write_config()
        with self.assertRaises(errors.DeliveryError) as caught:
            cloud.download(self.f.state, self.f.config, self.f.key, output, store=self.f.store)
        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')
        self.f.store.download_to.assert_not_called()


class ListProviderTests(unittest.TestCase):
    def test_list_transport_failure_has_list_stage_without_secret(self):
        from file_delivery.qiniu_store import QiniuStore
        from requests.exceptions import ReadTimeout
        session = mock.Mock()
        session.request.side_effect = ReadTimeout('private-token-sentinel')
        store = QiniuStore(access_key='ak', secret_key='sk', bucket='test-bucket', region='z1',
            download_domain='https://files.example.test', session=session)
        with self.assertRaises(errors.DeliveryError) as caught: store.list_objects()
        self.assertEqual(caught.exception.diagnostics['stage'], 'list')
        self.assertEqual(caught.exception.diagnostics['reason'], 'read_timeout')
        self.assertNotIn('private-token-sentinel', str(caught.exception))

    def test_download_stream_integrity_bounds_and_exact_url_origin(self):
        from file_delivery.qiniu_store import QiniuStore
        response = mock.Mock(status_code=200)
        response.iter_content.return_value = [b'ab', b'c']
        session = mock.Mock(); session.request.return_value = response
        store = QiniuStore(access_key='ak', secret_key='sk', bucket='test-bucket', region='z1',
            download_domain='https://files.example.test', session=session)
        expected = hashlib.sha256(b'abc').hexdigest()
        output = io.BytesIO()
        result = store.download_to('file-delivery/archive.zip', output, expected, 3)
        self.assertEqual(output.getvalue(), b'abc')
        self.assertEqual(result['sha256'], expected)
        response.close.assert_called_once()
        with self.assertRaises(errors.DeliveryError) as caught:
            store.download_to('file-delivery/archive.zip', io.BytesIO(), expected, 2)
        self.assertEqual(caught.exception.code, 'REMOTE_INTEGRITY')
        with self.assertRaises(errors.DeliveryError) as caught:
            store.verify_link('https://files.example.test/archive.zip?e=123&token=test', '0'*64, 3)
        self.assertEqual(caught.exception.code, 'REMOTE_INTEGRITY')
        count = session.request.call_count
        with self.assertRaises(errors.DeliveryError):
            store.verify_link('https://foreign.example.test/archive.zip?e=123&token=test', expected, 3)
        self.assertEqual(session.request.call_count, count)
        with mock.patch('file_delivery.qiniu_store.time.monotonic', side_effect=[0, 1000]):
            with self.assertRaises(errors.DeliveryError) as caught:
                store.download_to('file-delivery/archive.zip', io.BytesIO(), expected, 3)
        self.assertEqual(caught.exception.code, 'REMOTE_UNKNOWN')

    def test_provider_page_validation_and_url_encoding(self):
        from file_delivery.qiniu_store import QiniuStore
        response = mock.Mock(status_code=200)
        response.json.return_value = {'items': [{'key': '文件.zip', 'fsize': 2, 'hash': 'etag', 'putTime': 20_000_000}], 'marker': 'next'}
        session = mock.Mock(); session.request.return_value = response
        store = QiniuStore(access_key='ak', secret_key='sk', bucket='test-bucket', region='z1',
            download_domain='https://files.example.test', session=session)
        page = store.list_objects(prefix='报告&', marker='a+b', limit=2)
        self.assertEqual(page['items'][0]['uploaded_at'], 2)
        url = session.request.call_args.args[1]
        self.assertIn('prefix=%E6%8A%A5%E5%91%8A%26', url)
        self.assertIn('marker=a%2Bb', url)
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        for kwargs in ({'limit': True}, {'limit': 1001}, {'marker': '\n'}):
            with self.assertRaises(errors.DeliveryError): store.list_objects(**kwargs)
        response.json.return_value = {'items': [{'key': 'a', 'fsize': True, 'hash': 'etag', 'putTime': 1}]}
        with self.assertRaises(errors.DeliveryError) as caught: store.list_objects()
        self.assertEqual(caught.exception.code, 'REMOTE_ERROR')


class VersionedEmailTests(unittest.TestCase):
    def test_old_accepted_mail_and_new_version_unknown_never_resend(self):
        spec = importlib.util.spec_from_file_location('cloud_mail_fixture', Path(__file__).parent / 'fd005/test_fd005_notification.py')
        f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
        t = f.NotificationTests(); t.setUp(); self.addCleanup(t.doCleanups)
        t.send()
        t.f.store.signed_url_at = mock.Mock(side_effect=lambda key, deadline:
            {'url': f'https://files.example.com/{key}?e={deadline}&token=version-email', 'expires_at': deadline})
        t.f.store.verify_link = mock.Mock(return_value={'sha256': t.remote['archive_sha256'], 'size': t.remote['archive_size']})
        renewed = cloud.renew_link(t.f.state, t.f.config, t.f.key, 'mail-link', ttl_seconds=3600, store=t.f.store)
        self.assertTrue(t.send()['reused'])
        t.send(notification_key='mail-v2', link_key='mail-link')
        html = t.transport.messages[-1].get_body(preferencelist=('html',)).get_content()
        self.assertIn('version-email', html)
        self.assertTrue(t.send(notification_key='mail-v2', link_key='mail-link')['reused'])
        self.assertEqual(t.count('submit'), 2)
        failing = f.FakeTransport(t.events, fail='submit')
        t.failure('SMTP_UNKNOWN', lambda: t.send(notification_key='mail-unknown-v2', link_key='mail-link', transport=failing))
        count = t.count('submit')
        t.failure('SMTP_UNKNOWN', lambda: t.send(notification_key='mail-unknown-v2', link_key='mail-link'))
        self.assertEqual(t.count('submit'), count)
        self.assertEqual(t.f.store.count('upload'), 1)
        # New version expiration stops a new notification, never extends it.
        with mock.patch('file_delivery.notification.time.time', return_value=renewed['expires_at'] + 1):
            t.failure('LINK_EXPIRED', lambda: t.send(notification_key='expired-mail', link_key='mail-link'))


class CloudMCPTests(unittest.TestCase):
    def test_new_tools_schema_and_exact_core_routing(self):
        from mcp import Client
        from file_delivery.mcp_server import create_server
        async def scenario():
            cases = [
                ('list_qiniu', 'list_files', {'config_path': '/config'}),
                ('download_qiniu', 'download', {'state_dir': '/state', 'config_path': '/config', 'key': 'one', 'output_path': '/download.zip'}),
                ('renew_link', 'renew_link', {'state_dir': '/state', 'config_path': '/config', 'delivery_key': 'one', 'link_key': 'two'}),
                ('status_link', 'link_status', {'state_dir': '/state', 'delivery_key': 'one', 'link_key': 'two'})]
            async with Client(create_server()) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                self.assertEqual(len(tools), 15)
                self.assertIn('link_key', tools['send_email'].input_schema['properties'])
                for name, func, args in cases:
                    with mock.patch.object(cloud, func, return_value={'status': 'synthetic-ok'}) as spy:
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.is_error)
                        spy.assert_called_once()
                    self.assertFalse(tools[name].input_schema.get('additionalProperties', True))
                with mock.patch.object(cloud, 'renew_link') as spy:
                    result = await client.call_tool('renew_link', {**cases[2][2], 'ttl_seconds': True})
                    self.assertTrue(result.is_error); spy.assert_not_called()
                self.assertTrue(tools['list_qiniu'].annotations.read_only_hint)
                self.assertFalse(tools['download_qiniu'].annotations.read_only_hint)
        asyncio.run(scenario())
