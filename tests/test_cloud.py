import hashlib
import importlib.util
import json
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

    def test_manifest_tamper_stops_before_list(self):
        self.f.store.list_objects = mock.Mock()
        path = Path(self.result['password_file']).parent / 'manifest.json'
        path.write_text('{}')
        with self.assertRaises(errors.DeliveryError) as caught:
            cloud.list_files(self.f.config, self.f.state, store=self.f.store)
        self.assertEqual(caught.exception.code, 'STATE_INVALID')
        self.f.store.list_objects.assert_not_called()

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
