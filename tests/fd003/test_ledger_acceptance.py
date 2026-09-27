"""Trusted local ledger checks, no external provider calls."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[2]

class LedgerAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve(); self.root=self.base/'inputs'; self.root.mkdir()
        (self.root/'报告.txt').write_bytes(b'local ledger payload')
        self.state=self.base/'state'; self.store=self.base/'store'; self.key='test-key'
        self.env=dict(os.environ,PYTHONPATH=str(PROJECT/'src'),PYTHONDONTWRITEBYTECODE='1')

    def argv(self,key=None,store=None):
        return [sys.executable,'-m','file_delivery','deliver-local',str(self.root),'--root',str(self.root),'--state-dir',str(self.state),'--store-dir',str(store or self.store),'--key',key or self.key,'--json']

    def run_cli(self,argv=None,code=0):
        p=subprocess.run(argv or self.argv(),env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,code,p.stdout+p.stderr); self.assertEqual(p.stderr,''); return json.loads(p.stdout)

    def status_cli(self,key=None,code=0):
        return self.run_cli([sys.executable,'-m','file_delivery','status','--state-dir',str(self.state),'--key',key or self.key,'--json'],code)

    def assert_stored(self,d):
        self.assertEqual(d['status'],'stored-local'); self.assertEqual(d['schema_version'],1)
        self.assertEqual(d['key'],self.key); self.assertEqual(d['file_count'],1); self.assertEqual(d['total_bytes'],20)
        self.assertEqual(Path(d['artifact_path']),self.store/(d['task_id']+'.zip'))
        bundle=self.state/'bundles'/d['task_id']; self.assertEqual(Path(d['password_file']),bundle/'password.txt')
        self.assertEqual((bundle/'archive.zip').read_bytes(),Path(d['artifact_path']).read_bytes())
        self.assertEqual(d['archive_sha256'],hashlib.sha256(Path(d['artifact_path']).read_bytes()).hexdigest())
        self.assertIsInstance(d['reused'],bool)

    def test_roundtrip_idempotent_status_and_secret_permissions(self):
        d=self.run_cli(); self.assert_stored(d); self.assertFalse(d['reused'])
        secret=Path(d['password_file']).read_bytes().strip(); before=Path(d['artifact_path']).read_bytes()
        again=self.run_cli(); self.assert_stored(again); self.assertTrue(again['reused']); self.assertEqual(d['task_id'],again['task_id'])
        self.assertEqual(secret,Path(again['password_file']).read_bytes().strip()); self.assertEqual(before,Path(again['artifact_path']).read_bytes())
        status=self.status_cli(); self.assertEqual(status['task_id'],d['task_id']); self.assertEqual(status['status'],'stored-local')
        self.assertNotIn(secret.decode(),json.dumps([d,again,status])); self.assertTrue((self.state/'ledger.sqlite3').exists())
        with sqlite3.connect(self.state/'ledger.sqlite3') as db:
            self.assertNotIn(secret.decode(),'\n'.join(db.iterdump()))
        for parent in [self.state,self.store]:
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode),0o700)
            for p in parent.rglob('*'):
                if p.is_file(): self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600,str(p))
        self.assertEqual(self.status_cli('absent',code=2)['error']['code'],'TASK_NOT_FOUND')

    def test_conflicts_preserve_old_artifacts(self):
        d=self.run_cli(); artifact=Path(d['artifact_path']); original=artifact.read_bytes(); secret=Path(d['password_file']).read_bytes()
        (self.root/'报告.txt').write_bytes(b'changed')
        e=self.run_cli(code=2); self.assertEqual(e['error']['code'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual(artifact.read_bytes(),original); self.assertEqual(Path(d['password_file']).read_bytes(),secret)
        (self.root/'报告.txt').write_bytes(b'local ledger payload')
        e=self.run_cli(self.argv(store=self.base/'another-store'),code=2); self.assertEqual(e['error']['code'],'IDEMPOTENCY_CONFLICT')
        artifact.write_bytes(b'corrupted local artifact')
        e=self.run_cli(code=2); self.assertEqual(e['error']['code'],'LOCAL_STORE_CONFLICT'); self.assertEqual(artifact.read_bytes(),b'corrupted local artifact')
        artifact.unlink(); restored=self.run_cli(); self.assert_stored(restored); self.assertEqual(restored['task_id'],d['task_id']); self.assertEqual(artifact.read_bytes(),original)

    def test_handled_failure_recovery_at_both_boundaries(self):
        from file_delivery import ledger
        for stage in ['after_pack','after_store']:
            with self.subTest(stage=stage):
                state=self.base/('state-'+stage); store=self.base/('store-'+stage)
                def fault(name):
                    if name==stage: raise OSError('sensitive callback diagnostic must not leak')
                with self.assertRaises(Exception) as caught:
                    ledger.deliver_local([str(self.root)],str(self.root),str(state),str(store),self.key,checkpoint=fault)
                self.assertEqual(getattr(caught.exception,'code',None),'IO_ERROR')
                self.assertNotIn('sensitive callback diagnostic',str(caught.exception))
                status=ledger.status(str(state),self.key); self.assertNotEqual(status['status'],'stored-local'); self.assertTrue(status.get('last_error'))
                passwords=list(state.rglob('password.txt')); self.assertEqual(len(passwords),1); before=passwords[0].read_bytes()
                d=ledger.deliver_local([str(self.root)],str(self.root),str(state),str(store),self.key)
                self.assertEqual(d['status'],'stored-local'); self.assertEqual(d['task_id'],status['task_id']); self.assertEqual(Path(d['password_file']).read_bytes(),before)
                self.assertEqual(len(list(store.glob('*.zip'))),1)

    def test_process_exit_recovery_and_concurrent_duplicate(self):
        script='''import os,sys
from file_delivery import ledger
def checkpoint(stage):
 if stage==sys.argv[4]: os._exit(73)
ledger.deliver_local([sys.argv[1]],sys.argv[1],sys.argv[2],sys.argv[3],"test-key",checkpoint=checkpoint)
'''
        for stage in ['after_pack','after_store']:
            state=self.base/('crash-'+stage); store=self.base/('objects-'+stage)
            p=subprocess.run([sys.executable,'-c',script,str(self.root),str(state),str(store),stage],env=self.env,capture_output=True,text=True,timeout=30)
            self.assertEqual(p.returncode,73,p.stderr)
            passwords=list(state.rglob('password.txt')); self.assertEqual(len(passwords),1); before=passwords[0].read_bytes()
            args=self.argv(); args[args.index('--state-dir')+1]=str(state); args[args.index('--store-dir')+1]=str(store)
            d=self.run_cli(args); self.assertEqual(d['status'],'stored-local'); self.assertEqual(Path(d['password_file']).read_bytes(),before)
        workers=[subprocess.Popen(self.argv(),env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        results=[]
        for p in workers:
            stdout,stderr=p.communicate(timeout=30); self.assertEqual(p.returncode,0,stdout+stderr); results.append(json.loads(stdout))
        self.assertEqual(results[0]['task_id'],results[1]['task_id']); self.assertEqual(len(list(self.store.glob('*.zip'))),1)

    def test_private_paths_keys_and_network(self):
        invalid=self.argv(key='../bad'); self.assertEqual(self.run_cli(invalid,code=2)['error']['code'],'INVALID_KEY')
        for state,store in [(self.root/'state',self.store),(self.state,self.state/'store')]:
            args=self.argv();args[args.index('--state-dir')+1]=str(state);args[args.index('--store-dir')+1]=str(store)
            self.assertEqual(self.run_cli(args,code=2)['error']['code'],'STATE_INVALID')
        self.state.mkdir(exist_ok=True); self.state.chmod(0o755)
        self.assertEqual(self.run_cli(code=2)['error']['code'],'STATE_INVALID'); self.state.chmod(0o700)
        script='''import sys
from file_delivery import ledger
hits=[]
def audit(event,args):
 if event.startswith("socket."): hits.append(event);raise RuntimeError("network blocked")
sys.addaudithook(audit)
ledger.deliver_local([sys.argv[1]],sys.argv[1],sys.argv[2],sys.argv[3],"test-key")
assert not hits,hits
'''
        p=subprocess.run([sys.executable,'-c',script,str(self.root),str(self.state),str(self.store)],env=self.env,capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
