"""Frozen controller checks. Synthetic transport only; not live Qiniu evidence."""
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit, unquote

AK = 'synthetic-access-key'
SK = 'synthetic-secret-key'
CONTENT = b'encrypted-archive-test-fixture'


def response(code=200, payload=None, body=None):
    import requests
    class TrackedResponse(requests.Response):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    r = TrackedResponse()
    r.status_code = code
    data = body if body is not None else json.dumps(payload or {}).encode()
    r._content = data
    r.raw = io.BytesIO(data)
    return r


class Session:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.responses = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert url.startswith('https://'), 'HTTPS required'
        assert kw.get('allow_redirects') is False, 'redirect disabled explicitly'
        timeout = kw.get('timeout')
        values = timeout if isinstance(timeout, tuple) else (timeout,)
        assert all(isinstance(t, (float, int)) and 0 < t <= 120 for t in values)
        if not self.replies:
            raise AssertionError('Unexpected network operation or retry')
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        self.responses.append(reply)
        return reply


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / 'qiniu.json'
        self.values = {'access_key':AK, 'secret_key':SK, 'bucket':'fd-test',
                       'region':'z0', 'download_domain':'https://files.example.com',
                       'timeout_seconds':7}
        self.write_config()
        self.file = self.root / 'archive.zip'
        self.file.write_bytes(CONTENT)

    def write_config(self):
        self.config.write_text(json.dumps(self.values))
        self.config.chmod(0o600)

    def store(self, session):
        from file_delivery.qiniu_store import QiniuStore
        return QiniuStore.from_file(self.config, session=session)

    def failure(self, code, fn):
        from file_delivery.errors import DeliveryError
        with self.assertRaises(DeliveryError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn(SK, str(ctx.exception))
        self.assertNotIn('token=', str(ctx.exception))
        return ctx.exception

    def test_config(self):
        s = Session([])
        obj = self.store(s)
        self.assertNotIn(SK, repr(obj))
        self.assertNotIn(AK, repr(obj))
        self.assertEqual(s.calls, [])
        for field, bad in [('download_domain','http://example.com'),
                           ('download_domain','https://x:y@example.com'),
                           ('download_domain','https://example.com/?token=secret'),
                           ('download_domain','https://127.0.0.1'),
                           ('region','z0.evil.example'), ('timeout_seconds',True),
                           ('bucket','fd-test\nsecret'), ('access_key','')]:
            with self.subTest(field=field,bad=bad):
                old = self.values[field]
                self.values[field] = bad
                self.write_config()
                self.failure('CONFIG_INVALID', lambda:self.store(s))
                self.values[field] = old
        self.write_config()
        self.config.chmod(0o644)
        self.failure('CONFIG_INVALID', lambda:self.store(s))
        self.config.chmod(0o600)
        link = self.root/'link.json'
        link.symlink_to(self.config)
        from file_delivery.qiniu_store import QiniuStore
        self.failure('CONFIG_INVALID',lambda:QiniuStore.from_file(link,session=s))
        self.assertEqual(s.calls, [])

    def test_private(self):
        for value in [0, None, True, '1']:
            s=Session([response(payload={'private':value})])
            self.failure('BUCKET_NOT_PRIVATE',lambda:self.store(s).upload(self.file,'safe.zip'))
            self.assertEqual(len(s.calls),1)
            self.assertIn('/v2/bucketInfo',s.calls[0][1])
        s=Session([response(payload={'private':1})])
        self.store(s).ensure_private()
        self.assertIn('fd-test',s.calls[0][1])

    def test_upload(self):
        import qiniu
        etag=qiniu.etag(str(self.file))
        s=Session([response(payload={'private':1}),response(payload={'key':'nested/安全.zip','hash':etag})])
        result=self.store(s).upload(self.file,'nested/安全.zip',retention_days=30)
        self.assertEqual((result['status'],result['key'],result['etag'],result['size']),
                         ('uploaded','nested/安全.zip',etag,len(CONTENT)))
        method,url,kw=s.calls[-1]
        self.assertEqual((method.upper(),url.rstrip('/')),('POST','https://up-z0.qiniup.com'))
        token=kw['data']['token']
        ak,signature,encoded=token.split(':')
        self.assertEqual(ak,AK)
        self.assertEqual(signature,base64.urlsafe_b64encode(hmac.new(SK.encode(),encoded.encode(),hashlib.sha1).digest()).decode())
        policy=json.loads(base64.urlsafe_b64decode(encoded))
        self.assertEqual(policy['scope'],'fd-test:nested/安全.zip')
        self.assertEqual(policy['insertOnly'],1)
        self.assertEqual(policy['deleteAfterDays'],30)
        self.assertFalse(any(k.startswith('callback') for k in policy))
        for bad in ['../secret','a/../b','/abs','a\\b','a//b','a\nb']:
            empty=Session([])
            self.failure('INVALID_OBJECT_KEY',lambda:self.store(empty).upload(self.file,bad))
            self.assertEqual(empty.calls,[])
        s=Session([response(payload={'private':1}),response(614,{'error':SK})])
        self.failure('REMOTE_CONFLICT',lambda:self.store(s).upload(self.file,'safe.zip'))
        s=Session([response(payload={'private':1}),response(payload={'key':'wrong','hash':etag})])
        self.failure('REMOTE_INTEGRITY',lambda:self.store(s).upload(self.file,'safe.zip'))

    def test_stat(self):
        s=Session([response(payload={'hash':'etag','fsize':12})])
        result=self.store(s).stat('a.zip')
        self.assertEqual((result['key'],result['etag'],result['size']),('a.zip','etag',12))
        self.assertIsNone(self.store(Session([response(612)])).stat('a.zip'))
        for code,want in [(401,'REMOTE_AUTH'),(403,'REMOTE_AUTH'),(404,'REMOTE_ERROR'),(500,'REMOTE_UNKNOWN')]:
            self.failure(want,lambda:self.store(Session([response(code,{'error':SK})])).stat('a.zip'))
        self.failure('REMOTE_ERROR',lambda:self.store(Session([response(payload={'hash':'x','fsize':-1})])).stat('a.zip'))

    def test_link(self):
        import time
        s=Session([])
        link=self.store(s).signed_url('nested/安全 #.zip',ttl_seconds=60)
        parsed=urlsplit(link['url'])
        self.assertEqual(unquote(parsed.path),'/nested/安全 #.zip')
        query=parse_qs(parsed.query)
        self.assertEqual(int(query['e'][0]),link['expires_at'])
        self.assertTrue(time.time()+55 <= link['expires_at'] <= time.time()+61)
        unsigned=link['url'].split('&token=')[0]
        signature=base64.urlsafe_b64encode(hmac.new(SK.encode(),unsigned.encode(),hashlib.sha1).digest()).decode()
        self.assertEqual(query['token'],[AK+':'+signature])
        self.assertEqual(s.calls,[])
        for ttl in [0,604801,True]:
            self.failure('CONFIG_INVALID',lambda:self.store(s).signed_url('a.zip',ttl_seconds=ttl))
        digest=hashlib.sha256(CONTENT).hexdigest()
        s=Session([response(403),response(body=CONTENT)])
        r=self.store(s).verify_download('a.zip',digest,len(CONTENT))
        self.assertEqual((r['status'],r['sha256'],r['size']),('link-verified',digest,len(CONTENT)))
        self.assertNotIn('token=',json.dumps(r))
        self.assertNotIn('token=',s.calls[0][1])
        self.assertIn('token=',s.calls[1][1])
        self.assertTrue(s.calls[1][2].get('stream'))
        self.failure('BUCKET_NOT_PRIVATE',lambda:self.store(Session([response(body=CONTENT)])).verify_download('a.zip',digest,len(CONTENT)))
        self.failure('REMOTE_INTEGRITY',lambda:self.store(Session([response(403),response(body=b'wrong')])).verify_download('a.zip',digest,len(CONTENT)))

    def test_delete(self):
        for code in [200,612]:
            s=Session([response(code),response(612)])
            r=self.store(s).delete('a.zip')
            self.assertEqual((r['status'],r['key']),('object-deleted','a.zip'))
            self.assertEqual(len(s.calls),2)
            self.assertIn('/delete/',s.calls[0][1])
            self.assertIn('/stat/',s.calls[1][1])
        self.failure('REMOTE_DELETE_UNCONFIRMED',lambda:self.store(Session([response(),response(payload={'hash':'x','fsize':3})])).delete('a.zip'))

    def test_transport(self):
        import requests
        for fault in [requests.Timeout(SK+' token=private'),requests.ConnectionError(SK),response(503,{'error':SK})]:
            s=Session([fault])
            self.failure('REMOTE_UNKNOWN',lambda:self.store(s).stat('a.zip'))
            self.assertEqual(len(s.calls),1)
        s=Session([response(302)])
        self.failure('REMOTE_ERROR',lambda:self.store(s).stat('a.zip'))
        s=Session([response(payload={'hash':'x','fsize':1})])
        self.store(s).stat('a.zip')
        self.assertTrue(s.responses[0].closed, 'response must be closed')
        auth=s.calls[0][2].get('headers',{}).get('Authorization') or s.calls[0][2].get('auth')
        self.assertTrue(auth,'Management request must be authenticated')
