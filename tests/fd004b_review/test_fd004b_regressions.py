"""Controller regression preservation after independently accepted FD004B."""
import json, os, pathlib, subprocess, sys, unittest
class RemoteReviewRegressions(unittest.TestCase):
    def test_independent_handoff_path_and_error_regressions(self):
        root=pathlib.Path(__file__).resolve().parents[2]
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        run=subprocess.run([sys.executable,'-B',str(pathlib.Path(__file__).with_name('check_regressions.py')),str(root)],capture_output=True,text=True,env=env,timeout=60)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        report=json.loads(run.stdout)
        self.assertEqual(report['tests_run'],7)
        self.assertEqual(report['passed'],7)
