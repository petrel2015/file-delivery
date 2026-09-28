"""Frozen controller lifecycle tests for FD004C.
Pure synthetic stores/transports. No external provider operations.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('_fd004b_fixture', PROJECT/'tests/fd004b/test_fd004b_remote.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
AK, SK, MARKER = fixture.AK, fixture.SK, fixture.MARKER

class Store(fixture.FileStore):
    def __init__(self, directory):
        super().__init__(directory)
        self.delete_unknown = set()
        self.crash_delete = False
        self.before_delete = None
        self.probe_status = 'link-accessible'
        self.probe_http = 200

    def delete(self, key):
        from file_delivery.errors import DeliveryError
        if self.before_delete: self.before_delete(key)
        self.event('delete', key=key)
        self.object_path(key).unlink(missing_ok=True)
        if self.crash_delete: os._exit(73)
        if key in self.delete_unknown: raise DeliveryError('REMOTE_UNKNOWN', MARKER)
        return dict(status='object-deleted', key=key)

    def probe_link(self, url):
        self.event('probe')  # Never log signed URL, even in test event ledger.
        return dict(status=self.probe_status,http_status=self.probe_http)

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.root=self.base/'inputs';self.root.mkdir()
        (self.root/'报告.txt').write_text('synthetic delivery input')
        self.state=self.base/'state';self.config=self.base/'qiniu.json'
        self.values=dict(access_key=AK,secret_key=SK,bucket='fd-test',region='z0',download_domain='https://files.example.com/')
        self.config_write();self.store=Store(self.base/'objects')
        for name in ['socket.socket.connect','socket.getaddrinfo']:
            patch=mock.patch(name,side_effect=AssertionError('live network forbidden'));patch.start();self.addCleanup(patch.stop)

    def config_write(self,changes=None):
        self.config.write_text(json.dumps({**self.values,**(changes or {})}));self.config.chmod(0o600)

    def deliver(self,key='one',**kw):
        from file_delivery import remote
        return remote.deliver([str(self.root)],str(self.root),str(self.state),str(self.config),key,store=self.store,**kw)

    def revoke(self,key='one'):
        from file_delivery import remote
        return remote.revoke(str(self.state),str(self.config),key,store=self.store)

    def cleanup(self,**kw):
        from file_delivery import remote
        args=dict(store=self.store);args.update(kw)
        return remote.cleanup(str(self.state),**args)

    def failure(self,code,fn):
        from file_delivery.errors import DeliveryError
        with self.assertRaises(DeliveryError) as caught:fn()
        self.assertEqual(caught.exception.code,code)
        for secret in [AK,SK,MARKER,'synthetic-signature']:self.assertNotIn(secret,str(caught.exception))

    def artifacts(self):
        return {str(p.relative_to(self.state)):p.read_bytes() for directory in ['remote-bundles','remote-handoffs'] for p in (self.state/directory).rglob('*') if p.is_file()}

    def test_api(self):
        from file_delivery import remote,cli
        self.assertTrue(callable(remote.revoke));self.assertTrue(callable(remote.cleanup))
        report=dict(schema_version=1,status='cleanup-dry-run',items=[])
        with mock.patch.object(remote,'cleanup',return_value=report) as call:
            out=io.StringIO()
            with contextlib.redirect_stdout(out):code=cli.main(['cleanup-qiniu','--state-dir',str(self.state),'--json'])
            self.assertIn(code,(None,0));self.assertEqual(json.loads(out.getvalue()),report);call.assert_called_once()
            self.assertIsNot(call.call_args.kwargs.get('dry_run'),False)
        result=self.deliver();output=dict(schema_version=1,status='object-deleted',key='one',task_id=result['task_id'],object_key=result['object_key'],link_status='link-accessible',link_http_status=200)
        with mock.patch.object(remote,'revoke',return_value=output) as call:
            out=io.StringIO()
            with contextlib.redirect_stdout(out):code=cli.main(['revoke-qiniu','--state-dir',str(self.state),'--config',str(self.config),'--key','one','--json'])
            self.assertIn(code,(None,0));self.assertEqual(json.loads(out.getvalue()),output);call.assert_called_once()
        self.failure('TASK_NOT_FOUND',lambda:self.revoke('missing'))
        self.failure('CONFIG_INVALID',lambda:remote.cleanup(str(self.state),dry_run=False))

    def test_ownership(self):
        from file_delivery import remote
        clock=[int(time.time())]
        def delayed_pack(stage):
            if stage=='after_pack':clock[0]+=3600
        with mock.patch('time.time',side_effect=lambda:clock[0]):
            result=self.deliver(retention_days=2,ttl_seconds=86400,checkpoint=delayed_pack)
        self.assertEqual(remote.status(str(self.state),'one')['retention_expires_at'],clock[0]+2*86400)
        self.store.identity=lambda:dict(provider='qiniu',bucket='fd-test')
        for changes in [dict(bucket='other'),dict(region='z1'),dict(access_key='other-account'),dict(download_domain='https://other.example.com')]:
            self.config_write(changes);before=len(self.store.events());self.failure('IDEMPOTENCY_CONFLICT',self.revoke);self.assertEqual(len(self.store.events()),before)
        self.config_write()
        corrupted=self.deliver('corrupted-owner')
        with sqlite3.connect(self.state/'remote.sqlite3') as db:
            db.execute('UPDATE tasks SET object_key=? WHERE key=?',('unowned-key','corrupted-owner'))
        before=len(self.store.events());self.failure('STATE_INVALID',lambda:self.revoke('corrupted-owner'));self.assertEqual(len(self.store.events()),before)
        self.config_write(dict(secret_key='rotated-secret'))
        self.assertEqual(self.revoke()['status'],'object-deleted')
        # Authoritative legacy fixture is the FD004B tasks schema, without C metadata.
        # This fixture must be checked against accepted B before final C freeze.
        legacy=self.base/'legacy';legacy.mkdir(mode=0o700)
        dbpath=legacy/'remote.sqlite3'
        with sqlite3.connect(dbpath) as db:
            db.execute('CREATE TABLE tasks (key TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE,object_key TEXT NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,archive_sha256 TEXT,archive_size INTEGER,bundle_path TEXT,password_file TEXT,handoff_path TEXT,file_count INTEGER,total_bytes INTEGER,expires_at INTEGER,url_sha256 TEXT,last_error TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO tasks(key,task_id,object_key,fingerprint,state) VALUES(?,?,?,?,?)',('legacy','owned-legacy','file-delivery/owned-legacy.zip','legacy-fingerprint','uploaded'))
        dbpath.chmod(0o600);before=len(self.store.events())
        self.failure('STATE_INVALID',lambda:remote.revoke(str(legacy),str(self.config),'legacy',store=self.store))
        report=remote.cleanup(str(legacy),store=self.store)
        self.assertTrue(any(i['reason']=='metadata-missing' for i in report['items']));self.assertEqual(len(self.store.events()),before)

    def test_revoke(self):
        from file_delivery import remote
        result=self.deliver();before=self.artifacts()
        def intent(key):self.assertEqual(remote.status(str(self.state),'one')['status'],'revoking')
        self.store.before_delete=intent;self.store.delete_unknown.add(result['object_key'])
        self.failure('REMOTE_UNKNOWN',self.revoke);self.assertEqual(self.store.count('delete'),1)
        self.assertEqual(remote.status(str(self.state),'one')['status'],'revoking')
        uploads=self.store.count('upload');self.failure('TASK_REVOKED',self.deliver);self.assertEqual(self.store.count('upload'),uploads)
        self.store.delete_unknown.clear();start=len(self.store.events());done=self.revoke()
        self.assertEqual(done['status'],'object-deleted');self.assertEqual(self.store.count('delete'),1)
        self.assertTrue(any(e['op']=='stat' for e in self.store.events()[start:]))
        self.assertEqual(self.artifacts(),before);self.failure('TASK_REVOKED',self.deliver)
        self.assertEqual(self.deliver('new-key')['status'],'link-verified')
        # Delete happened but process died: persisted intent + stat reconcile prevent repeat.
        crash=self.deliver('crash');self.store.before_delete=None;self.store.crash_delete=True
        pid=os.fork()
        if pid==0:
            try:self.revoke('crash')
            except BaseException:os._exit(91)
            os._exit(92)
        deadline=time.monotonic()+20
        while True:
            finished,value=os.waitpid(pid,os.WNOHANG)
            if finished:break
            if time.monotonic()>deadline:
                os.kill(pid,signal.SIGKILL);os.waitpid(pid,0);self.fail('delete crash child exceeded20seconds')
            time.sleep(.02)
        self.assertEqual(os.waitstatus_to_exitcode(value),73)
        self.store.crash_delete=False;deleted=self.store.count('delete');self.assertEqual(self.revoke('crash')['status'],'object-deleted');self.assertEqual(self.store.count('delete'),deleted)

    def test_probe(self):
        from file_delivery.qiniu_store import QiniuStore
        import requests
        class Response(requests.Response):
            def __init__(self,status):super().__init__();self.status_code=status;self._content=b'';self._content_consumed=True;self.closed=False
            def close(self):self.closed=True
            def iter_content(self,*a,**k):raise AssertionError('probe must not download body')
        class Session:
            def __init__(self,result):self.result=result;self.calls=[]
            def request(self,method,url,**kw):
                self.calls.append((method,url,kw))
                if isinstance(self.result,Exception):raise self.result
                return self.result
        valid='https://files.example.com/file-delivery/example.zip?e=123&token=synthetic-signature'
        for code,expected in [(200,'link-accessible'),(401,'link-unavailable'),(403,'link-unavailable'),(404,'link-unavailable'),(612,'link-unavailable'),(302,'link-unknown'),(500,'link-unknown'),(206,'link-unknown')]:
            response=Response(code);session=Session(response);store=QiniuStore.from_file(self.config,session=session);actual=store.probe_link(valid)
            self.assertEqual(actual,dict(status=expected,http_status=code));self.assertTrue(response.closed);self.assertEqual(len(session.calls),1)
            method,url,kw=session.calls[0];self.assertEqual(method.upper(),'GET');self.assertTrue(kw['stream']);self.assertIs(kw['allow_redirects'],False);self.assertTrue(0<kw['timeout']<=120)
            self.assertFalse(any(k.lower()=='authorization' and v for k,v in kw.get('headers',{}).items()));self.assertFalse(kw.get('auth'));self.assertFalse(kw.get('cookies'))
        session=Session(requests.ConnectionError(MARKER));store=QiniuStore.from_file(self.config,session=session)
        self.assertEqual(store.probe_link(valid),dict(status='link-unknown',http_status=None))
        for bad in ['http://files.example.com/a','//files.example.com/a','https://evil.example.com/a','https://files.example.com.evil.test/a','https://files.example.com:444/a','https://user:pass@files.example.com/a','https://files.example.com/a#fragment','https://files.example.com/a\n','https://[bad','https://files.example.com']:
            session=Session(Response(200));store=QiniuStore.from_file(self.config,session=session);self.failure('CONFIG_INVALID',lambda:store.probe_link(bad));self.assertEqual(session.calls,[])
        session=Session(Response(404));store=QiniuStore.from_file(self.config,session=session)
        self.assertEqual(store.probe_link(valid.replace('files.example.com/','files.example.com:443/'))['status'],'link-unavailable')
        # Observe the prepared wire request, including session-level defaults.
        # A custom adapter intercepts send completely; no sockets are opened.
        class CaptureAdapter(requests.adapters.BaseAdapter):
            def __init__(self):self.seen=[]
            def send(self,request,**kwargs):
                self.seen.append(request)
                response=Response(404);response.request=request;response.url=request.url
                return response
            def close(self):pass
        transport=requests.Session();self.addCleanup(transport.close)
        transport.trust_env=False
        transport.headers['Authorization']='Bearer synthetic-session-management-secret'
        transport.cookies.set('session_cookie','synthetic-cookie-secret',domain='files.example.com',path='/')
        adapter=CaptureAdapter();transport.mount('https://',adapter)
        store=QiniuStore.from_file(self.config,session=transport)
        self.assertEqual(store.probe_link(valid)['status'],'link-unavailable')
        self.assertEqual(len(adapter.seen),1,'probe must use configured injected transport exactly once')
        prepared=adapter.seen[0]
        self.assertNotIn('Authorization',prepared.headers)
        self.assertNotIn('Cookie',prepared.headers)

    def test_link(self):
        from file_delivery import remote
        result=self.deliver();handoff=Path(result['handoff_file']);raw=handoff.read_bytes();before=self.artifacts()
        changed=json.loads(raw);changed['url']='https://evil.example.com/file?token=changed';handoff.write_text(json.dumps(changed));handoff.chmod(0o600)
        calls=len(self.store.events());self.failure('STATE_INVALID',self.revoke);self.assertEqual(len(self.store.events()),calls);handoff.write_bytes(raw)
        self.root.rename(self.base/'input-gone');signs=self.store.count('sign')
        with mock.patch('time.time',return_value=result['expires_at']+2):done=self.revoke()
        self.assertEqual(done['status'],'object-deleted');self.assertEqual(done['link_status'],'link-accessible');self.assertEqual(done['link_http_status'],200)
        self.assertEqual(self.store.count('sign'),signs);self.assertEqual(self.artifacts(),before)
        self.store.probe_status='link-unknown';self.store.probe_http=None
        self.assertEqual(self.revoke()['link_status'],'link-unknown')
        self.assertEqual(remote.status(str(self.state),'one')['status'],'object-deleted')
        self.store.probe_status='link-unavailable';self.store.probe_http=404
        self.assertEqual(self.revoke()['link_status'],'link-unavailable');self.assertEqual(self.store.count('delete'),1)
        secret=Path(result['password_file']).read_text().strip()
        with sqlite3.connect(self.state/'remote.sqlite3') as db:dump='\n'.join(db.iterdump())
        for data in [json.dumps(done),json.dumps(remote.status(str(self.state),'one')),dump]:
            for secret_value in [secret,AK,SK,'synthetic-signature',json.loads(raw)['url']]:self.assertNotIn(secret_value,data)

    def test_cleanup(self):
        from file_delivery import remote
        start=int(time.time())
        with mock.patch('time.time',return_value=start):
            due=self.deliver('due',ttl_seconds=60,retention_days=1)
            unknown=self.deliver('unknown',ttl_seconds=60,retention_days=1)
            later=self.deliver('later',ttl_seconds=60,retention_days=30)
        self.store.object_path('unowned-key').write_bytes(b'do-not-delete')
        before=self.artifacts();database=(self.state/'remote.sqlite3').read_bytes();calls=len(self.store.events())
        early=self.cleanup(now=start+120);self.assertFalse(any(i['eligible'] for i in early['items']))
        dry=self.cleanup(now=start+86400);self.assertEqual(dry['status'],'cleanup-dry-run');self.assertEqual({i['key'] for i in dry['items'] if i['eligible']},{'due','unknown'})
        self.assertEqual(len(self.store.events()),calls);self.assertEqual((self.state/'remote.sqlite3').read_bytes(),database)
        self.config_write(dict(bucket='other'));report=self.cleanup(config_path=str(self.config),dry_run=False,now=start+86400)
        self.assertTrue(any(i['reason']=='identity-mismatch' for i in report['items']));self.assertEqual(len(self.store.events()),calls)
        self.config_write();self.store.delete_unknown.add(unknown['object_key'])
        report=self.cleanup(config_path=str(self.config),dry_run=False,now=start+86400)
        self.assertEqual(report['status'],'cleanup-partial');self.assertEqual(remote.status(str(self.state),'due')['status'],'object-deleted')
        self.assertEqual(remote.status(str(self.state),'unknown')['status'],'revoking');self.assertTrue(self.store.object_path(later['object_key']).exists())
        self.assertTrue(self.store.object_path('unowned-key').exists());self.assertEqual(self.artifacts(),before)
        self.store.delete_unknown.clear();deletes=self.store.count('delete');again=self.cleanup(config_path=str(self.config),dry_run=False,now=start+86400)
        self.assertEqual(again['status'],'cleanup-complete');self.assertEqual(self.store.count('delete'),deletes)
        for invalid in [True,-1,1.5]:self.failure('CONFIG_INVALID',lambda: self.cleanup(now=invalid))

if __name__=='__main__':unittest.main(verbosity=2)
