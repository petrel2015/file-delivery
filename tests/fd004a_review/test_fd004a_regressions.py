"""Controller-owned offline regressions from FD-004A independent reviews."""
import sys
from pathlib import Path
import hashlib, json, tempfile, unittest
import requests
CONFIG = dict(access_key='synthetic-ak', secret_key='synthetic-sk', bucket='fd-test', region='z0', download_domain='https://files.example.com')
class Response(requests.Response):
    def __init__(self, code, broken=False):
        super().__init__(); self.status_code=code; self._content=b''; self._content_consumed=True; self.closed=False; self.broken=broken
    def close(self): self.closed=True
    def iter_content(self, *args, **kwargs):
        if self.broken: raise requests.ConnectionError('synthetic-signed-url-token-marker')
        yield b''
class Session:
    def __init__(self,replies=()): self.replies=list(replies); self.calls=0
    def request(self,*args,**kwargs):
        self.calls+=1
        return self.replies.pop(0)
class Regressions(unittest.TestCase):
    def setUp(self):
        from file_delivery.qiniu_store import QiniuStore
        from file_delivery.errors import DeliveryError
        self.Store = QiniuStore
        self.Error = DeliveryError
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
    def store(self,session,changes=None):
        path=self.root/'config.json'; path.write_text(json.dumps({**CONFIG,**(changes or {})})); path.chmod(0o600)
        return self.Store.from_file(path,session=session)
    def check_error(self,code,fn):
        with self.assertRaises(self.Error) as ctx: fn()
        self.assertEqual(ctx.exception.code,code)
        self.assertNotIn('synthetic-signed-url-token-marker',str(ctx.exception))
    def test_stream_failure(self):
        body=Response(200,True); session=Session([Response(403),body]); store=self.store(session)
        try:self.check_error('REMOTE_UNKNOWN',lambda:store.verify_download('a',hashlib.sha256(b'').hexdigest(),0))
        finally:self.assertTrue(body.closed)
        self.assertEqual(session.calls,2)
    def test_config_domain(self):
        s=Session(); self.check_error('CONFIG_INVALID',lambda:self.store(s,{'download_domain':'https://[bad'})); self.assertEqual(s.calls,0)
    def test_config_bucket_newline(self):
        s=Session(); self.check_error('CONFIG_INVALID',lambda:self.store(s,{'bucket':'bucket\n'})); self.assertEqual(s.calls,0)
    def test_config_timeout_null(self):
        s=Session(); self.check_error('CONFIG_INVALID',lambda:self.store(s,{'timeout_seconds':None})); self.assertEqual(s.calls,0)
    def test_surrogate_key(self):
        s=Session(); store=self.store(s); self.check_error('INVALID_OBJECT_KEY',lambda:store.signed_url(chr(0xd800))); self.assertEqual(s.calls,0)
    def test_sha_trailing_newline(self):
        s=Session([Response(403),Response(200)]); store=self.store(s); self.check_error('CONFIG_INVALID',lambda:store.verify_download('a','0'*64+'\n',0)); self.assertEqual(s.calls,0)
    def test_domain_controls_rejected_before_network(self):
        for domain in ['https://files.example.com\n', 'https://fi\tles.example.com', 'https://files.example.com\r']:
            with self.subTest(domain=repr(domain)):
                session=Session()
                self.check_error('CONFIG_INVALID',lambda:self.store(session,{'download_domain':domain}))
                self.assertEqual(session.calls,0)
if __name__=='__main__': unittest.main(verbosity=2)
