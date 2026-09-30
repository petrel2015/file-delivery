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
