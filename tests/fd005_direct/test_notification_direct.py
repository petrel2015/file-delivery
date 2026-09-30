"""SMTP privacy and recovery regressions found during direct completion."""
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest import mock
from file_delivery import archive, contacts, errors, notification
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('notification_fixture',ROOT/'tests/fd005/test_fd005_notification.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class DirectRegressions(unittest.TestCase):
    def setUp(self):
        self.t=fixture.NotificationTests();self.t.setUp();self.addCleanup(self.t.doCleanups)
    def test_private_database_modes_and_symlinks(self):
        t=self.t;t.send();db=t.f.state/'notifications.sqlite3';db.chmod(0o644)
        t.failure('STATE_INVALID',t.status);t.failure('STATE_INVALID',t.send)
        self.assertEqual(db.stat().st_mode&0o777,0o644)
        db.chmod(0o600);outside=t.f.base/'saved-db';db.rename(outside);db.symlink_to(outside)
        t.failure('STATE_INVALID',t.status);t.failure('STATE_INVALID',t.send)
    def test_remote_database_and_private_artifact_parents(self):
        t=self.t;db=t.f.state/'remote.sqlite3';saved=t.f.base/'saved-remote';db.rename(saved);db.symlink_to(saved)
        t.failure('STATE_INVALID',t.send);db.unlink();saved.rename(db)
        for parent in (t.f.state/'remote-handoffs',Path(t.remote['password_file']).parent):
            parent.chmod(0o755)
            try:t.failure('STATE_INVALID',t.send)
            finally:parent.chmod(0o700)
        self.assertEqual(t.count('prepare'),0)
    def test_lock_symlink_rejected_without_modifying_target(self):
        t=self.t;outside=t.f.base/'outside-lock';outside.write_text('preserve');outside.chmod(0o644)
        (t.f.state/'locks'/('notification-'+t.key+'.lock')).symlink_to(outside)
        t.failure('STATE_INVALID',t.send)
        self.assertEqual(outside.read_text(),'preserve');self.assertEqual(outside.stat().st_mode&0o777,0o644)
    def test_invalid_record_cannot_leak_or_resubmit(self):
        t=self.t;t.send()
        for field,value in [('state','invalid-state'),('last_error',fixture.SENTINEL),('message_id',fixture.SENTINEL)]:
            with sqlite3.connect(t.f.state/'notifications.sqlite3') as db:
                old=db.execute('SELECT '+field+' FROM notifications').fetchone()[0]
                db.execute('UPDATE notifications SET '+field+'=?',(value,))
            t.failure('STATE_INVALID',t.status);t.failure('STATE_INVALID',t.send)
            with sqlite3.connect(t.f.state/'notifications.sqlite3') as db:db.execute('UPDATE notifications SET '+field+'=?',(old,))
        self.assertEqual(t.count('submit'),1)
    def test_valid_replacement_bundle_must_match_remote_digest(self):
        t=self.t;other=t.f.base/'other-bundle';archive.pack([str(t.f.root)],str(t.f.root),other)
        bundle=Path(t.remote['password_file']).parent
        for name in (archive.ARCHIVE_NAME,archive.MANIFEST_NAME,archive.PASSWORD_NAME):shutil.copyfile(other/name,bundle/name)
        self.assertEqual(archive.verify(bundle)['status'],'verified')
        t.failure('STATE_INVALID',t.send);self.assertEqual(t.count('prepare'),0)
    def test_commit_failure_after_data_never_resends(self):
        t=self.t;original=notification._update_state
        def update(conn,key,state,*a,**kw):
            if state in (notification.STATE_ACCEPTED,notification.STATE_UNKNOWN):raise sqlite3.OperationalError(fixture.SENTINEL)
            return original(conn,key,state,*a,**kw)
        with mock.patch.object(notification,'_update_state',side_effect=update):t.failure('SMTP_UNKNOWN',t.send)
        self.assertEqual(t.count('submit'),1)
        t.failure('SMTP_UNKNOWN',t.send);self.assertEqual(t.count('submit'),1)
        self.assertEqual(t.status()['status'],'unknown')
    def test_dns_labels_and_ip_config(self):
        self.assertEqual(contacts.resolve('A@mail-host.example'),'A@mail-host.example')
        self.assertIsNone(contacts.parse_addr_spec('a@'+('a'*64)+'.test'))
        self.t.write_config(host='::1');self.assertEqual(notification.load_smtp_config(self.t.config)['host'],'::1')
        self.t.write_config(host='invalid..host');self.t.failure('CONFIG_INVALID',lambda:notification.load_smtp_config(self.t.config))

    def test_unknown_smtp_code_cannot_leak_raw_record(self):
        t=self.t;t.send()
        with sqlite3.connect(t.f.state/'notifications.sqlite3') as db:
            db.execute("UPDATE notifications SET state='unknown',last_error='SMTP_UNKNOWN',smtp_code=?",(fixture.SENTINEL,))
        t.failure('STATE_INVALID',t.status);t.failure('STATE_INVALID',t.send)
        self.assertEqual(t.count('submit'),1)
