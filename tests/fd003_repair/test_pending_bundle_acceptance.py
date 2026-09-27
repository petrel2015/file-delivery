"""Controller regression for retaining an already-published pending bundle."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest import mock
class PendingBundleAcceptance(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.b=Path(self.t.name).resolve()
        self.root=self.b/'input';self.root.mkdir();(self.root/'hello.txt').write_bytes(b'pending durable contents')
        self.state=self.b/'state';self.store=self.b/'objects'
    def deliver(self,checkpoint=None):
        from file_delivery import ledger
        return ledger.deliver_local([str(self.root)],str(self.root),str(self.state),str(self.store),'preserve',checkpoint=checkpoint)
    def test_verify_failure_preserves_then_recovers_same_bundle(self):
        from file_delivery import ledger,archive,errors
        def fault(stage):
            if stage=='after_pack':raise OSError('injected checkpoint')
        with self.assertRaises(errors.DeliveryError):self.deliver(fault)
        before_status=ledger.status(str(self.state),'preserve')
        password_file=next(self.state.rglob('password.txt'));bundle=password_file.parent
        before={p.name:p.read_bytes() for p in bundle.iterdir()};self.assertFalse(list(self.store.glob('*.zip')))
        with mock.patch.object(archive,'verify',side_effect=errors.DeliveryError(errors.VERIFY_FAILED,'temporary read failure')):
            with self.assertRaises(errors.DeliveryError) as caught:self.deliver()
        self.assertEqual(caught.exception.code,errors.VERIFY_FAILED)
        self.assertEqual(before,{p.name:p.read_bytes() for p in bundle.iterdir()})
        self.assertFalse(list(self.store.glob('*.zip')))
        result=self.deliver();self.assertEqual(result['status'],'stored-local');self.assertEqual(result['task_id'],before_status['task_id']);self.assertTrue(result['reused'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in bundle.iterdir()})
        self.assertEqual(result['archive_sha256'],hashlib.sha256(before['archive.zip']).hexdigest())
    def test_fresh_task_can_create_first_bundle(self):
        result=self.deliver();self.assertEqual(result['status'],'stored-local');self.assertFalse(result['reused'])
        again=self.deliver();self.assertTrue(again['reused']);self.assertEqual(result['archive_sha256'],again['archive_sha256'])
