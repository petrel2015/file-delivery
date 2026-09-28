"""Controller-owned independent regressions retained after acceptance."""
import json,os,subprocess,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
class LifecycleReviewRegressions(unittest.TestCase):
    def check(self,name,count):
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
        p=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name(name)),str(ROOT)],env=env,capture_output=True,text=True,timeout=45)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        r=json.loads(p.stdout);self.assertEqual(r['tests_run'],count);self.assertEqual(r['passed'],count)
    def test_independent_revoke_probe_regressions(self):self.check('check_regressions.py',8)
    def test_missing_metadata_and_recovery_matrix(self):self.check('check_metadata_matrix.py',18)
