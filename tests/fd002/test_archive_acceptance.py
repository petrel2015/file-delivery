"""Controller-owned FD002 executable contract; fixtures contain no real secrets."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]

class ArchiveAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'inputs'; self.root.mkdir()
        (self.root / '报告 空格.txt').write_bytes(b'private payload alpha')
        (self.root / 'empty.txt').touch()
        (self.root / 'nested').mkdir()
        (self.root / 'nested' / 'two.bin').write_bytes(bytes(range(256)))
        self.out = self.base / 'bundle'

    def cli(self, *args, code=0):
        env = dict(os.environ, PYTHONPATH=str(PROJECT/'src'), PYTHONDONTWRITEBYTECODE='1')
        p = subprocess.run([sys.executable,'-m','file_delivery',*map(str,args),'--json'],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,code,p.stdout+p.stderr)
        data=json.loads(p.stdout); self.assertEqual(p.stderr,'')
        return data

    def pack(self, out=None):
        return self.cli('pack',self.root,self.root/'报告 空格.txt','--root',self.root,'--output-dir',out or self.out)

    def error(self,args,code):
        d=self.cli(*args,code=2); self.assertEqual(d['status'],'error'); self.assertEqual(d['error']['code'],code)

    def test_roundtrip_aes_and_secrets(self):
        import pyzipper
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=self.pack(); self.assertEqual(result['status'],'packaged'); self.assertEqual(result['schema_version'],1)
        self.assertEqual(result['file_count'],3); self.assertEqual(result['total_bytes'],277)
        for key,name in [('bundle_dir',''),('archive_path','archive.zip'),('manifest_path','manifest.json'),('password_file','password.txt')]:
            self.assertEqual(Path(result[key]),self.out/name)
        self.assertEqual(set(p.name for p in self.out.iterdir()),{'archive.zip','manifest.json','password.txt'})
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode),0o700)
        for p in self.out.iterdir(): self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
        password=(self.out/'password.txt').read_bytes().strip(); self.assertGreaterEqual(len(password),22)
        self.assertNotIn(password.decode(),json.dumps(result)); self.assertNotIn(b'private payload alpha',(self.out/'archive.zip').read_bytes())
        manifest=json.loads((self.out/'manifest.json').read_text())
        with pyzipper.AESZipFile(str(self.out/'archive.zip')) as z:
            self.assertEqual(z.namelist(),[f['path'] for f in manifest['files']])
            for info in z.infolist():
                self.assertEqual(info.wz_aes_strength,3); self.assertTrue(info.flag_bits&1)
                content=z.read(info.filename,pwd=password)
                self.assertEqual(content,(self.root/info.filename).read_bytes())
                self.assertEqual(info.CRC,0)
            with self.assertRaises(Exception): z.read(z.namelist()[0],pwd=b'wrong')
        verified=self.cli('verify',self.out); self.assertEqual(verified['status'],'verified')
        self.assertEqual(verified['archive_sha256'],hashlib.sha256((self.out/'archive.zip').read_bytes()).hexdigest())
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        other=self.base/'other'; self.pack(other)
        self.assertNotEqual(password,(other/'password.txt').read_bytes().strip())

    def test_output_rejections_and_planner_errors(self):
        args=['pack',self.root,'--root',self.root,'--output-dir']
        self.error([*args,self.root/'bundle'],'OUTPUT_NOT_ALLOWED')
        self.out.mkdir(); (self.out/'sentinel').write_text('keep')
        self.error([*args,self.out],'OUTPUT_EXISTS'); self.assertEqual((self.out/'sentinel').read_text(),'keep')
        self.error([*args,self.base/'missing'/'bundle'],'IO_ERROR')
        link=self.base/'link'; link.symlink_to(self.out); self.error([*args,link],'OUTPUT_EXISTS')
        (self.root/'.env').write_text('do not leak')
        self.error([*args,self.base/'bad'],'SENSITIVE_PATH'); self.assertFalse((self.base/'bad').exists())

    def test_wrong_password_corruption_manifest_and_unsafe_entries(self):
        import pyzipper
        self.pack(); secret=(self.out/'password.txt').read_bytes().strip()
        wrong=self.base/'wrong'; wrong.write_text('incorrect')
        self.error(['verify',self.out,'--password-file',wrong],'VERIFY_FAILED')
        archive=self.out/'archive.zip'; original=archive.read_bytes()
        archive.write_bytes(b'bad archive'); self.error(['verify',self.out],'VERIFY_FAILED'); archive.write_bytes(original)
        manifest=self.out/'manifest.json'; raw=manifest.read_bytes(); data=json.loads(raw)
        data['files'][0]['sha256']='0'*64; manifest.write_text(json.dumps(data)); self.error(['verify',self.out],'VERIFY_FAILED'); manifest.write_bytes(raw)
        for name in ['../escape.txt','/absolute.txt','extra.txt']:
            with pyzipper.AESZipFile(str(archive),'a',encryption=pyzipper.WZ_AES) as z:
                z.setpassword(secret); z.setencryption(pyzipper.WZ_AES,nbits=256); z.writestr(name,b'x')
            self.error(['verify',self.out],'VERIFY_FAILED'); archive.write_bytes(original)
        import zipfile
        with zipfile.ZipFile(archive,'w') as z:
            for entry in data['files']: z.writestr(entry['path'],(self.root/entry['path']).read_bytes())
        self.error(['verify',self.out],'VERIFY_FAILED')
        self.assertFalse((self.base/'escape.txt').exists())

    def test_observed_change_cleanup_and_no_network(self):
        from file_delivery import archive, planning
        original=planning.plan
        def mutate(*a,**k):
            result=original(*a,**k); (self.root/'报告 空格.txt').write_bytes(b'changed'); return result
        with mock.patch.object(planning,'plan',side_effect=mutate):
            with self.assertRaises(Exception) as caught: archive.pack([str(self.root)],str(self.root),str(self.out))
        self.assertEqual(getattr(caught.exception,'code',None),'INPUT_CHANGED')
        self.assertEqual({p.name for p in self.base.iterdir()},{'inputs'})
        script='''import sys,json
from file_delivery import archive
hits=[]
def audit(event,args):
 if event.startswith("socket."):
  hits.append(event); raise RuntimeError("network blocked")
sys.addaudithook(audit)
archive.pack([sys.argv[1]],sys.argv[1],sys.argv[2])
assert not hits,hits
'''
        env=dict(os.environ,PYTHONPATH=str(PROJECT/'src'))
        p=subprocess.run([sys.executable,'-c',script,str(self.root),str(self.out)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_sevenzip_compatibility(self):
        seven=shutil.which('7zz') or '/opt/homebrew/bin/7zz'
        self.assertTrue(Path(seven).exists(),'HARNESS_BUILD_TOOLS_MISSING: 7zz')
        self.pack(); password=(self.out/'password.txt').read_text().strip()
        # Synthetic test-only password, never a user secret. Quiet output retained only on error.
        destination=self.base/'unpacked'
        p=subprocess.run([seven,'x',str(self.out/'archive.zip'),'-p'+password,'-o'+str(destination),'-y'],capture_output=True,text=True,timeout=30)
        self.assertEqual(p.returncode,0,'7-Zip decryption failed')
        for source in self.root.rglob('*'):
            if source.is_file(): self.assertEqual(source.read_bytes(),(destination/source.relative_to(self.root)).read_bytes())

    def test_optional_dependency_and_public_api(self):
        import tomllib
        cfg=tomllib.loads((PROJECT/'pyproject.toml').read_text())
        self.assertIn('pyzipper==0.4.0',cfg['project']['optional-dependencies']['archive'])
        from file_delivery import archive
        self.assertTrue(callable(archive.pack)); self.assertTrue(callable(archive.verify))
        # Block third-party import in an otherwise normal Python interpreter.
        script='''import sys,importlib.abc
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name=="pyzipper" or name.startswith("pyzipper."): raise ModuleNotFoundError("blocked pyzipper")
sys.meta_path.insert(0,Block())
from file_delivery.cli import main
sys.exit(main(sys.argv[1:]))
'''
        p=subprocess.run([sys.executable,'-c',script,'pack',str(self.root),'--root',str(self.root),'--output-dir',str(self.out),'--json'],env=dict(os.environ,PYTHONPATH=str(PROJECT/'src')),capture_output=True,text=True)
        self.assertEqual(p.returncode,2,p.stderr); self.assertEqual(json.loads(p.stdout)['error']['code'],'DEPENDENCY_MISSING')
