"""Frozen controller remote orchestration checks; no live provider access.

The file-backed provider survives fork/exit and records real side-effect counts.
Business imports stay inside methods so discovery does not require implementation.
"""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import quote

PROJECT = Path(__file__).resolve().parents[2]
AK = 'controller-synthetic-access-key'
SK = 'controller-synthetic-secret-key'
MARKER = 'private-diagnostic-token-do-not-leak'


class FileStore:
    """API-compatible synthetic provider, durable across actual process exits."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_file = self.directory / 'events.jsonl'
        self.unknown = False
        self.public = False
        self.conflicting = False
        self.fail_verify = False
        self.delay = 0

    def event(self, op, **fields):
        with self.events_file.open('a') as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.write(json.dumps(dict(op=op, **fields))+'\n')
            out.flush(); os.fsync(out.fileno())

    def events(self):
        return [json.loads(s) for s in self.events_file.read_text().splitlines()] if self.events_file.exists() else []

    def count(self, op):
        return sum(e['op'] == op for e in self.events())

    def object_path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest()+'.object')

    def ensure_private(self):
        from file_delivery.errors import DeliveryError
        self.event('private')
        if self.public:
            raise DeliveryError('BUCKET_NOT_PRIVATE', MARKER)

    def stat(self, key):
        import qiniu
        self.event('stat', key=key)
        if self.conflicting:
            return dict(key=key, etag='different-etag', size=999)
        p = self.object_path(key)
        return dict(key=key, etag=qiniu.etag(str(p)), size=p.stat().st_size) if p.exists() else None

    def upload(self, path, key, retention_days=30):
        import qiniu
        from file_delivery.errors import DeliveryError
        self.event('upload', key=key, filename=Path(path).name, retention=retention_days)
        time.sleep(self.delay)
        data = Path(path).read_bytes()
        try:
            with self.object_path(key).open('xb') as out:
                out.write(data); out.flush(); os.fsync(out.fileno())
        except FileExistsError:
            raise DeliveryError('REMOTE_CONFLICT', MARKER) from None
        if self.unknown:
            raise DeliveryError('REMOTE_UNKNOWN', MARKER)
        return dict(status='uploaded', key=key, etag=qiniu.etag(str(path)), size=len(data))

    def verify_download(self, key, expected_sha256, expected_size):
        from file_delivery.errors import DeliveryError
        self.event('verify', key=key)
        data = self.object_path(key).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if self.fail_verify or (digest, len(data)) != (expected_sha256, expected_size):
            raise DeliveryError('REMOTE_INTEGRITY', MARKER)
        return dict(status='link-verified', sha256=digest, size=len(data))

    def signed_url(self, key, ttl_seconds=604800):
        deadline = int(time.time()) + ttl_seconds
        self.event('sign', key=key, ttl=ttl_seconds, deadline=deadline)
        return dict(url='https://files.example.com/'+quote(key,safe='/')+'?e='+str(deadline)+'&token=synthetic-signature', expires_at=deadline)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base/'inputs'; self.root.mkdir()
        self.input = self.root/'报告.txt'; self.input.write_bytes('交付内容\n'.encode())
        self.config = self.base/'qiniu.json'
        self.values = dict(access_key=AK, secret_key=SK, bucket='fd-test', region='z0', download_domain='https://files.example.com/')
        self.write_config()
        self.state = self.base/'state'
        self.store = FileStore(self.base/'objects')
        self.key = 'remote-key'
        self.env = dict(os.environ, PYTHONPATH=str(PROJECT/'src'), PYTHONDONTWRITEBYTECODE='1')
        network = mock.patch('socket.socket.connect', side_effect=AssertionError('live network forbidden'))
        network.start(); self.addCleanup(network.stop)
        dns = mock.patch('socket.getaddrinfo', side_effect=AssertionError('live DNS forbidden'))
        dns.start(); self.addCleanup(dns.stop)

    def write_config(self, changes=None, path=None):
        p = path or self.config
        p.write_text(json.dumps({**self.values, **(changes or {})})); p.chmod(0o600)
        return p

    def deliver(self, **kwargs):
        from file_delivery import remote
        args = dict(paths=[str(self.root)],root=str(self.root),state_dir=str(self.state),config_path=str(self.config),key=self.key,store=self.store)
        args.update(kwargs)
        return remote.deliver(**args)

    def status(self, state=None, key=None):
        from file_delivery import remote
        return remote.status(str(state or self.state), key or self.key)

    def failure(self, code, fn):
        from file_delivery.errors import DeliveryError
        with self.assertRaises(DeliveryError) as ctx:
            fn()
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        for secret in [AK,SK,MARKER,'synthetic-signature']:
            self.assertNotIn(secret, str(ctx.exception))
        return ctx.exception

    def bundle(self, result, state=None):
        return (state or self.state)/'remote-bundles'/result['task_id']

    def assert_result(self, result, state=None):
        self.assertEqual(result['schema_version'],1)
        self.assertEqual(result['status'],'link-verified')
        self.assertEqual(result['key'],self.key)
        self.assertEqual(result['file_count'],1)
        self.assertEqual(result['total_bytes'],self.input.stat().st_size)
        self.assertIsInstance(result['reused'],bool)
        self.assertEqual(result['object_key'],'file-delivery/'+result['task_id']+'.zip')
        bundle = self.bundle(result,state)
        data = (bundle/'archive.zip').read_bytes()
        self.assertEqual(result['archive_sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(result['archive_size'],len(data))
        self.assertEqual(Path(result['password_file']),bundle/'password.txt')
        self.assertEqual(Path(result['handoff_file']),(state or self.state)/'remote-handoffs'/(result['task_id']+'.json'))
        self.assertIsInstance(result['expires_at'],int)

    def cli(self, args, expected=0):
        script = "import sys,runpy\ndef audit(event,args):\n if event in ('socket.connect','socket.getaddrinfo'): raise RuntimeError('live network forbidden')\nsys.addaudithook(audit)\nsys.argv=['file-delivery']+sys.argv[1:]\nrunpy.run_module('file_delivery',run_name='__main__')"
        p = subprocess.run([sys.executable,'-B','-c',script,*args],env=self.env,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,expected,p.stdout+p.stderr)
        self.assertEqual(p.stderr,'')
        return json.loads(p.stdout)

    def fork(self, fn, name):
        output = self.base/(name+'.json')
        pid = os.fork()
        if pid == 0:
            try:
                result = fn()
                output.write_text(json.dumps(dict(result=result)))
                os._exit(0)
            except BaseException as exc:
                output.write_text(json.dumps(dict(error=type(exc).__name__,code=getattr(exc,'code',None),message=str(exc))))
                os._exit(91)
        return pid, output

    def join(self, child, expected=0):
        pid, output = child
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            finished, value = os.waitpid(pid,os.WNOHANG)
            if finished:
                status = os.waitstatus_to_exitcode(value)
                content = json.loads(output.read_text()) if output.exists() else {}
                self.assertEqual(status,expected,content)
                return content.get('result')
            time.sleep(.02)
        os.kill(pid,signal.SIGKILL); os.waitpid(pid,0)
        self.fail('child did not terminate within20seconds')

    def test_api(self):
        from file_delivery import remote, cli
        self.assertTrue(callable(remote.deliver)); self.assertTrue(callable(remote.status))
        result = self.deliver(); self.assert_result(result)
        # The public CLI must call the core; injection remains API-only.
        with mock.patch.object(remote,'deliver',return_value=result) as call:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(['deliver-qiniu',str(self.root),'--root',str(self.root),'--state-dir',str(self.state),'--config',str(self.config),'--key',self.key,'--json'])
            self.assertIn(code,(0,None)); self.assertEqual(json.loads(output.getvalue())['task_id'],result['task_id']); call.assert_called_once()
        status = self.cli(['status-qiniu','--state-dir',str(self.state),'--key',self.key,'--json'])
        self.assertEqual(status['status'],'link-verified')
        invalid = self.cli(['deliver-qiniu',str(self.root),'--root',str(self.root),'--state-dir',str(self.state),'--config',str(self.config),'--key','../bad','--json'],2)
        self.assertEqual(invalid['error']['code'],'INVALID_KEY')
        self.assertEqual(self.cli(['status-qiniu','--state-dir',str(self.state),'--key','absent','--json'],2)['error']['code'],'TASK_NOT_FOUND')

    def test_identity(self):
        from file_delivery.qiniu_store import QiniuStore
        identity = QiniuStore.from_file(self.config).identity()
        self.assertEqual(identity,dict(provider='qiniu',bucket='fd-test',region='z0',download_domain='https://files.example.com',account_id=hashlib.sha256(AK.encode()).hexdigest()))
        original = self.deliver()
        bundle = self.bundle(original); password = (bundle/'password.txt').read_bytes(); archive=(bundle/'archive.zip').read_bytes()
        again = self.deliver(paths=[str(self.input),str(self.root),str(self.input)])
        self.assertTrue(again['reused']); self.assertEqual(again['task_id'],original['task_id']); self.assertEqual(self.store.count('upload'),1)
        self.assertEqual((bundle/'archive.zip').read_bytes(),archive); self.assertEqual((bundle/'password.txt').read_bytes(),password)
        # Destination comes from actual config, never an untrusted injected identity.
        self.store.identity=lambda: identity
        for change in [dict(bucket='other-bucket'),dict(region='z1'),dict(download_domain='https://other.example.com'),dict(access_key='different-account')]:
            self.write_config(change); self.failure('IDEMPOTENCY_CONFLICT',lambda:self.deliver())
        self.write_config(dict(secret_key='rotated-secret',download_domain='https://files.example.com'))
        self.assertEqual(self.deliver()['task_id'],original['task_id'])
        for change in [dict(ttl_seconds=60),dict(retention_days=31)]:
            self.failure('IDEMPOTENCY_CONFLICT',lambda change=change:self.deliver(**change))
        self.input.write_bytes(b'changed'); self.failure('IDEMPOTENCY_CONFLICT',lambda:self.deliver())
        self.input.write_bytes('交付内容\n'.encode())
        other = self.deliver(state_dir=str(self.base/'other-state'))
        self.assertNotEqual(other['task_id'],original['task_id']); self.assertNotEqual(other['object_key'],original['object_key'])
        self.write_config(dict(download_domain='http://unsafe.example.com'))
        before=len(self.store.events()); self.failure('CONFIG_INVALID',lambda:self.deliver(key='new-key')); self.assertEqual(len(self.store.events()),before)

    def test_delivery(self):
        from file_delivery import archive
        result=self.deliver(); self.assert_result(result)
        archive.verify(str(self.bundle(result)))
        events=self.store.events(); upload=next(i for i,e in enumerate(events) if e['op']=='upload')
        self.assertTrue(any(e['op']=='stat' for e in events[:upload]))
        self.assertTrue(any(e['op']=='private' for e in events))
        self.assertEqual([e['filename'] for e in events if e['op']=='upload'],['archive.zip'])
        self.assertEqual(next(e['retention'] for e in events if e['op']=='upload'),30)
        private=self.store.count('private'); verify=self.store.count('verify')
        self.deliver(); self.assertGreater(self.store.count('private'),private); self.assertGreater(self.store.count('verify'),verify)
        self.assertEqual(self.store.count('upload'),1)
        self.store.public=True; self.failure('BUCKET_NOT_PRIVATE',lambda:self.deliver()); self.store.public=False
        self.store.fail_verify=True; self.failure('REMOTE_INTEGRITY',lambda:self.deliver()); self.store.fail_verify=False
        conflicted=FileStore(self.base/'conflict-objects'); conflicted.conflicting=True
        self.failure('REMOTE_CONFLICT',lambda:self.deliver(state_dir=str(self.base/'conflict-state'),store=conflicted))
        self.assertEqual(conflicted.count('upload'),0)
        # Damaged pending bundle must survive; retries cannot erase secrets and reencrypt.
        state=self.base/'bad-pending'
        def stop(stage):
            if stage=='after_pack': raise OSError(MARKER)
        self.failure('IO_ERROR',lambda:self.deliver(state_dir=str(state),checkpoint=stop))
        bundle=next((state/'remote-bundles').iterdir()); secret=(bundle/'password.txt').read_bytes()
        (bundle/'archive.zip').write_bytes(b'corrupted')
        self.failure(None,lambda:self.deliver(state_dir=str(state)))
        self.assertEqual((bundle/'archive.zip').read_bytes(),b'corrupted'); self.assertEqual((bundle/'password.txt').read_bytes(),secret)

    def test_recovery(self):
        # Real fork exits demonstrate uncommitted-transition recovery, not handled exceptions alone.
        for stage,expected_state in [('after_pack','pending'),('after_upload','uploading'),('after_link','uploaded')]:
            with self.subTest(stage=stage):
                state=self.base/('crash-'+stage); store=FileStore(self.base/('objects-'+stage))
                def crash(name):
                    if name==stage: os._exit(73)
                child=self.fork(lambda:self.deliver(state_dir=str(state),store=store,checkpoint=crash),stage)
                self.join(child,73)
                saved=self.status(state); self.assertEqual(saved['status'],expected_state)
                bundle=state/'remote-bundles'/saved['task_id']; password=(bundle/'password.txt').read_bytes(); data=(bundle/'archive.zip').read_bytes()
                handoffs=list((state/'remote-handoffs').glob('*.json')) if (state/'remote-handoffs').exists() else []
                before=handoffs[0].read_bytes() if handoffs else None
                recovered=self.deliver(state_dir=str(state),store=store); self.assert_result(recovered,state)
                self.assertEqual(recovered['task_id'],saved['task_id']); self.assertEqual(Path(recovered['password_file']).read_bytes(),password); self.assertEqual((bundle/'archive.zip').read_bytes(),data)
                self.assertEqual(store.count('upload'),1)
                if before is not None: self.assertEqual(Path(recovered['handoff_file']).read_bytes(),before)
        state=self.base/'unknown-state'; store=FileStore(self.base/'unknown-objects'); store.unknown=True
        self.failure('REMOTE_UNKNOWN',lambda:self.deliver(state_dir=str(state),store=store))
        self.assertEqual(store.count('upload'),1); saved=self.status(state); self.assertEqual(saved['last_error'],'REMOTE_UNKNOWN')
        password=next(state.rglob('password.txt')).read_bytes(); store.unknown=False
        result=self.deliver(state_dir=str(state),store=store); self.assertEqual(Path(result['password_file']).read_bytes(),password); self.assertEqual(store.count('upload'),1)
        state=self.base/'concurrent'; store=FileStore(self.base/'concurrent-objects'); store.delay=.15
        children=[self.fork(lambda:self.deliver(state_dir=str(state),store=store),'concurrent-'+str(i)) for i in range(2)]
        results=[self.join(child) for child in children]
        self.assertEqual(results[0]['task_id'],results[1]['task_id']); self.assertEqual(store.count('upload'),1)
        state=self.base/'callback-state'
        def fault(stage):
            if stage=='after_pack': raise OSError(MARKER)
        self.failure('IO_ERROR',lambda:self.deliver(state_dir=str(state),checkpoint=fault))
        self.assertEqual(self.status(state)['last_error'],'IO_ERROR')

    def test_handoff(self):
        result=self.deliver(); handoff=Path(result['handoff_file']); raw=handoff.read_bytes(); payload=json.loads(raw)
        for field in ['task_id','object_key','archive_sha256','archive_size','expires_at','password_file']:
            self.assertEqual(payload[field],result[field])
        self.assertIn('token=synthetic-signature',payload['url'])
        secret=Path(result['password_file']).read_text().strip()
        with sqlite3.connect(self.state/'remote.sqlite3') as db: dump='\n'.join(db.iterdump())
        for text in [json.dumps(result),json.dumps(self.status()),dump]:
            for value in [secret,AK,SK,payload['url'],'synthetic-signature']:
                self.assertNotIn(value,text)
        self.deliver(); self.assertEqual(handoff.read_bytes(),raw)
        # status is an offline persisted view even without inputs/config.
        moved=self.base/'moved'; self.root.rename(moved); self.config.unlink()
        self.assertEqual(self.status()['task_id'],result['task_id'])
        moved.rename(self.root); self.write_config()
        for field,value in [('url','https://attacker.example.com/?token=changed'),('object_key','file-delivery/wrong.zip')]:
            changed={**payload,field:value}; handoff.write_text(json.dumps(changed)); handoff.chmod(0o600)
            self.failure('STATE_INVALID',lambda:self.deliver()); self.assertEqual(json.loads(handoff.read_text())[field],value)
            handoff.write_bytes(raw)
        with mock.patch('time.time',return_value=result['expires_at']+2):
            uploads=self.store.count('upload'); signs=self.store.count('sign'); self.failure('LINK_EXPIRED',lambda:self.deliver()); self.assertEqual(self.store.count('upload'),uploads); self.assertEqual(self.store.count('sign'),signs)
        self.assertEqual(handoff.read_bytes(),raw)
        for path in self.state.rglob('*'):
            if path.is_file(): self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600,str(path))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode),0o700)

    def test_safety(self):
        from file_delivery import remote
        before=len(self.store.events())
        for kwargs in [dict(ttl_seconds=True),dict(ttl_seconds=0),dict(ttl_seconds=604801),dict(retention_days=True),dict(retention_days=0),dict(retention_days=3651),dict(ttl_seconds=100000,retention_days=1)]:
            self.failure('CONFIG_INVALID',lambda kwargs=kwargs:self.deliver(**kwargs))
        self.failure('INVALID_KEY',lambda:self.deliver(key='../bad'))
        for state in [self.root/'state',self.base]:
            self.failure('STATE_INVALID',lambda state=state:self.deliver(state_dir=str(state)))
        unsafe=self.base/'unsafe'; unsafe.mkdir(mode=0o755); unsafe.chmod(0o755)
        self.failure('STATE_INVALID',lambda:self.deliver(state_dir=str(unsafe)))
        link=self.base/'link'; link.symlink_to(self.root,target_is_directory=True)
        self.failure('STATE_INVALID',lambda:self.deliver(state_dir=str(link/'..'/'escaped')))
        inner=self.write_config(path=self.root/'qiniu.json')
        self.failure('CONFIG_INVALID',lambda:self.deliver(config_path=str(inner)))
        inner.unlink(); self.assertEqual(len(self.store.events()),before)
        state=self.base/'bad-db'; state.mkdir(mode=0o700); db=state/'remote.sqlite3'; db.write_bytes(b'not sqlite'); db.chmod(0o600)
        self.failure('STATE_INVALID',lambda:self.deliver(state_dir=str(state))); self.failure('STATE_INVALID',lambda:remote.status(str(state),self.key))
        result=self.deliver()
        # Discover lock artifact names rather than prescribe implementation naming.
        locks=[p for p in self.state.rglob('*') if p.is_file() and ('lock' in p.name.lower() or 'lock' in p.parent.name.lower())]
        targets=[self.state/'remote.sqlite3',self.bundle(result)/'archive.zip',Path(result['handoff_file'])]+locks[:1]
        for target in targets:
            saved=target.with_name(target.name+'.saved'); target.rename(saved); target.symlink_to(saved)
            try:
                self.failure('STATE_INVALID',lambda:self.deliver())
                if target.name=='remote.sqlite3': self.failure('STATE_INVALID',lambda:self.status())
            finally: target.unlink(); saved.rename(target)
        # Lock held by a separate process must yield within the promised deadline.
        if not locks:
            print('FD004B_COVERAGE_GAP: lock artifact naming unknown; direct lock symlink/deadline probe not performed', file=sys.stderr)
            return
        lock=locks[0]
        with lock.open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            def blocked():
                started=time.monotonic()
                try:self.deliver()
                except Exception as e:return dict(code=getattr(e,'code',None),elapsed=time.monotonic()-started)
                return dict(code='UNEXPECTED_SUCCESS',elapsed=time.monotonic()-started)
            # Child must close inherited descriptor or it owns parent's lock too.
            def contender():
                handle.close()
                return blocked()
            outcome=self.join(self.fork(contender,'lock-contention'))
            self.assertEqual(outcome['code'],'BUSY'); self.assertLess(outcome['elapsed'],6.0)
