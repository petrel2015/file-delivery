"""Additional controller reproductions; original FD002 tests stay frozen."""
import json,os,subprocess,sys,tempfile,unittest,zlib
from pathlib import Path
from unittest import mock
PROJECT=Path(__file__).resolve().parents[2]
class ArchiveErrorAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); inputs=self.root/'inputs';inputs.mkdir();(inputs/'a.txt').write_text('test payload')
        self.bundle=self.root/'bundle'
        from file_delivery import archive
        archive.pack([str(inputs)],str(inputs),str(self.bundle))
    def test_invalid_utf8_files_return_json(self):
        for name in ['manifest.json','password.txt']:
            path=self.bundle/name; original=path.read_bytes();path.write_bytes(b'\xff')
            p=subprocess.run([sys.executable,'-B','-m','file_delivery','verify',str(self.bundle),'--json'],env=dict(os.environ,PYTHONPATH=str(PROJECT/'src'),PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
            path.write_bytes(original)
            self.assertEqual(p.returncode,2,p.stderr);self.assertEqual(p.stderr,'');self.assertEqual(json.loads(p.stdout)['error']['code'],'VERIFY_FAILED')
    def test_decompression_error_is_safe(self):
        import pyzipper
        from file_delivery import archive
        with mock.patch.object(pyzipper.AESZipFile,'read',side_effect=zlib.error('private decoder payload')):
            with self.assertRaises(Exception) as caught:archive.verify(str(self.bundle))
        self.assertEqual(getattr(caught.exception,'code',None),'VERIFY_FAILED')
        self.assertNotIn('private decoder payload',str(caught.exception))
    def test_worker_cli_field_regression(self):
        p=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_cli.py'],cwd=PROJECT,env=dict(os.environ,PYTHONPATH=str(PROJECT/'src'),PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
