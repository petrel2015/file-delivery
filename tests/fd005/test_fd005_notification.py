"""Controller-owned frozen FD005 acceptance against accepted FD004C.

Frozen path tests/fd005/test_fd005_notification.py. No real network.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]
SENTINEL = 'synthetic-smtp-secret-do-not-leak'
SPEC = importlib.util.spec_from_file_location('fd005_remote_fixture', PROJECT/'tests/fd004b/test_fd004b_remote.py')
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class FakeTransport:
    def __init__(self, events, fail=None):
        self.path = Path(events)
        self.fail = fail
        self.messages = []

    def event(self, name):
        with self.path.open('a') as out:
            out.write(name+'\n');out.flush();os.fsync(out.fileno())

    def prepare(self, config, from_addr, to_addr):
        self.event('prepare')
        if self.fail == 'prepare':
            raise RuntimeError(SENTINEL)
        if self.fail == 'prepare-io':
            raise OSError(SENTINEL)

    def submit(self, message, from_addr, to_addr):
        self.event('submit')
        self.messages.append(message)
        if self.fail == 'submit':
            raise TimeoutError(SENTINEL)
        if self.fail == 'malformed':
            return {'status':'sent'}
        return {'status':'channel-accepted','smtp_code':250}

    def close(self):
        self.event('close')
        if self.fail == 'close':
            raise RuntimeError(SENTINEL)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        # Reuse committed controller-only remote fixture; all networking blocked.
        self.f = FIXTURE.RemoteTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.remote = self.f.deliver()
        self.config = self.f.base/'smtp.json'
        self.password = self.f.base/'smtp-password.txt'
        self.password.write_text(SENTINEL+'\n');self.password.chmod(0o600)
        self.values = dict(schema_version=1,host='smtp.example.test',port=465,tls='implicit',
                           username='sender@example.test',password_file=self.password.name,
                           from_address='sender@example.test',timeout_seconds=3)
        self.write_config()
        self.contacts = self.f.base/'contacts.json'
        self.contacts.write_text(json.dumps({'schema_version':1,'contacts':{'打印店':'Shop@EXAMPLE.TEST','同址':'Shop@example.test'}},ensure_ascii=False));self.contacts.chmod(0o600)
        self.events = self.f.base/'smtp-events'
        self.transport = FakeTransport(self.events)
        self.key = 'mail-1'

    def write_config(self, **changes):
        self.config.write_text(json.dumps({**self.values,**changes}));self.config.chmod(0o600)

    def send(self, **changes):
        from file_delivery import notification
        args = dict(state_dir=str(self.f.state),delivery_key=self.f.key,smtp_config=str(self.config),
                    recipient='打印店',notification_key=self.key,contacts_path=str(self.contacts),transport=self.transport)
        args.update(changes)
        return notification.send(**args)

    def status(self):
        from file_delivery import notification
        return notification.status(str(self.f.state),self.key)

    def count(self, op):
        return self.events.read_text().splitlines().count(op) if self.events.exists() else 0

    def failure(self, code, fn):
        from file_delivery.errors import DeliveryError
        with self.assertRaises(DeliveryError) as caught:fn()
        if code is not None:self.assertEqual(caught.exception.code,code)
        self.assertNotIn(SENTINEL,str(caught.exception));self.assertNotIn(SENTINEL,caught.exception.code)
        return caught.exception.code

    @contextlib.contextmanager
    def smtp_constructor(self, notification, client, implicit=True):
        """Patch stdlib and equivalent bound imports, never require import style."""
        original = smtplib.SMTP_SSL if implicit else smtplib.SMTP
        name = 'SMTP_SSL' if implicit else 'SMTP'
        with contextlib.ExitStack() as stack:
            factory = stack.enter_context(mock.patch.object(smtplib, name, return_value=client))
            for alias, value in tuple(vars(notification).items()):
                if value is original:
                    stack.enter_context(mock.patch.object(notification, alias, factory))
            yield factory

    def test_surface_message_and_privacy(self):
        result=self.send();self.assertEqual(result['status'],'channel-accepted')
        for field in ('notification_id','key','delivery_task_id','message_id','reused'):self.assertIn(field,result)
        self.assertEqual(result['receipt_status'],'unverified');self.assertEqual(result['read_status'],'unverified')
        self.assertFalse(result['reused']);self.assertEqual(result['delivery_task_id'],self.remote['task_id'])
        message=self.transport.messages[0]
        self.assertEqual(message.get_all('To'),['Shop@example.test']);self.assertEqual(message.get_all('From'),['sender@example.test'])
        self.assertEqual(message['Message-ID'],result['message_id']);self.assertFalse(list(message.iter_attachments()))
        body=message.get_content();handoff=json.loads(Path(self.remote['handoff_file']).read_text())
        archive_password=Path(self.remote['password_file']).read_text().strip()
        for token in (handoff['url'],archive_password,str(self.remote['archive_size']),str(self.remote['expires_at'])):self.assertIn(token,body)
        with sqlite3.connect(self.f.state/'notifications.sqlite3') as db:dump='\n'.join(db.iterdump())
        for text in (json.dumps(result),json.dumps(self.status()),dump):
            for secret in (SENTINEL,handoff['url'],archive_password):self.assertNotIn(secret,text)
        self.assertEqual((self.f.state/'notifications.sqlite3').stat().st_mode&0o777,0o600)

    def test_contacts_and_injection(self):
        from file_delivery import contacts
        self.assertEqual(contacts.resolve('打印店',str(self.contacts)),'Shop@example.test')
        self.assertEqual(contacts.resolve('Shop@EXAMPLE.TEST'),'Shop@example.test')
        self.failure('CONTACT_NOT_FOUND',lambda:contacts.resolve('missing',str(self.contacts)))
        self.failure('CONTACTS_INVALID',lambda:contacts.resolve('打印店'))
        for address in ('x@example.test\r\nBcc:v@example.test','A <a@example.test>','a@example.test,b@example.test',
                        'a@example.test\x00','a..b@example.test','.a@example.test','a@-bad.test','a@bad..test','a @example.test','a@é.test'):
            with self.subTest(address=repr(address)):
                self.failure('RECIPIENT_INVALID',lambda:self.send(recipient=address))
        self.assertEqual(self.count('prepare'),0)
        self.contacts.write_text('{"schema_version":1,"contacts":{"x":"x@example.test","x":"y@example.test"}}')
        self.failure('CONTACTS_INVALID',lambda:contacts.resolve('x',str(self.contacts)))

    def test_alias_control_characters_rejected_before_transport(self):
        from file_delivery import contacts
        original=self.contacts.read_bytes()
        for control in ('\x00','\t','\n','\r','\x1f','\x7f'):
            with self.subTest(control=ord(control)):
                # Even a valid file must not normalize raw alias controls away.
                self.failure(None,lambda: self.send(recipient='打印店'+control))
                self.assertEqual(self.count('prepare'),0)
                bad={'schema_version':1,'contacts':{'打印店'+control:'Shop@example.test'}}
                self.contacts.write_text(json.dumps(bad,ensure_ascii=False))
                self.failure('CONTACTS_INVALID',lambda: contacts.resolve('打印店',str(self.contacts)))
                self.contacts.write_bytes(original)
        self.assertEqual(self.count('prepare'),0)

    def test_config_rejects_before_transport(self):
        for change in ({'host':'smtp.example.test\nX'}, {'from_address':'a@example.test\r\nBcc:x@example.test'},
                       {'tls':'none'},{'port':True},{'port':0},{'timeout_seconds':True},{'timeout_seconds':0}):
            with self.subTest(change=change):
                self.write_config(**change);self.failure('CONFIG_INVALID',self.send)
        self.write_config();self.password.chmod(0o644);self.failure('CONFIG_INVALID',self.send)
        self.assertEqual(self.count('prepare'),0)

    def test_idempotency_alias_and_rotation(self):
        first=self.send();again=self.send(recipient='同址')
        self.assertTrue(again['reused']);self.assertEqual(first['message_id'],again['message_id']);self.assertEqual(self.count('submit'),1)
        self.failure('IDEMPOTENCY_CONFLICT',lambda:self.send(recipient='other@example.test'))
        self.password.write_text('rotated-private-secret');self.assertTrue(self.send()['reused']);self.assertEqual(self.count('submit'),1)
        self.contacts.write_text(json.dumps({'schema_version':1,'contacts':{'打印店':'retarget@example.test'}}));self.failure('IDEMPOTENCY_CONFLICT',self.send)

    def test_prepare_failure_explicit_retry_secret_rotation(self):
        self.transport.fail='prepare';self.failure(None,self.send)
        self.assertEqual(self.count('prepare'),1);self.assertEqual(self.count('submit'),0)
        status=self.status();self.assertEqual(status['status'],'failed-before-send');self.assertTrue(status['retryable'])
        self.password.write_text('rotated-private-secret');self.transport.fail=None
        self.assertEqual(self.send()['status'],'channel-accepted');self.assertEqual(self.count('submit'),1)

    def test_oserror_before_sending_boundary_is_safe_io_error(self):
        self.transport.fail='prepare-io'
        self.failure('IO_ERROR',self.send)
        self.assertEqual(self.count('submit'),0)
        status=self.status()
        self.assertEqual(status['status'],'failed-before-send')
        self.assertEqual(status['last_error'],'IO_ERROR')
        self.assertTrue(status['retryable'])
        self.transport.fail=None
        self.assertEqual(self.send()['status'],'channel-accepted')
        self.assertEqual(self.count('submit'),1)

    def test_unknown_never_resends(self):
        self.transport.fail='submit';self.failure('SMTP_UNKNOWN',self.send)
        self.transport.fail=None;self.failure('SMTP_UNKNOWN',self.send)
        self.assertEqual(self.count('submit'),1);self.assertEqual(self.count('prepare'),1)
        status=self.status();self.assertEqual(status['status'],'unknown');self.assertFalse(status['retryable'])

    def test_malformed_submit_is_unknown(self):
        self.transport.fail='malformed';self.failure('SMTP_UNKNOWN',self.send)
        self.transport.fail=None;self.failure('SMTP_UNKNOWN',self.send);self.assertEqual(self.count('submit'),1)

    def test_close_failure_preserves_acceptance(self):
        self.transport.fail='close';first=self.send();self.assertEqual(first['status'],'channel-accepted')
        self.assertTrue(self.send()['reused']);self.assertEqual(self.count('submit'),1)

    def test_handoff_validation_and_lifecycle(self):
        p=Path(self.remote['handoff_file']);raw=p.read_bytes();payload=json.loads(raw)
        for field,value in [('url','https://wrong.example.test/'),('expires_at',payload['expires_at']+1),('password_file',str(self.password)),('archive_sha256','0'*64)]:
            with self.subTest(field=field):
                p.write_text(json.dumps({**payload,field:value}));self.failure('STATE_INVALID',self.send);p.write_bytes(raw)
        for state in ('revoking','object-deleted','uploaded'):
            with sqlite3.connect(self.f.state/'remote.sqlite3') as db:db.execute('UPDATE tasks SET state=? WHERE key=?',(state,self.f.key))
            self.failure('DELIVERY_NOT_READY',self.send)
        self.assertEqual(self.count('prepare'),0)

    def test_changed_bundle_password_or_archive_is_not_mailed(self):
        password=Path(self.remote['password_file'])
        archive=password.parent/'archive.zip'
        for path,bad in [(password,b'changed-but-readable-password'),(archive,b'broken-encrypted-archive')]:
            with self.subTest(path=path.name):
                original=path.read_bytes()
                path.write_bytes(bad)
                try:
                    self.failure('STATE_INVALID',self.send)
                    self.assertEqual(self.count('prepare'),0)
                    self.assertEqual(path.read_bytes(),bad,'validation must not repair or replace the original bundle')
                finally:path.write_bytes(original)
        self.assertEqual(self.send()['status'],'channel-accepted')
        self.assertEqual(self.count('submit'),1)

    def test_handoff_paths_and_expiry(self):
        parent=self.f.state/'remote-handoffs';outside=self.f.base/'outside-handoffs';parent.rename(outside);parent.symlink_to(outside,target_is_directory=True)
        self.failure('STATE_INVALID',self.send);parent.unlink();outside.rename(parent)
        p=Path(self.remote['password_file']);saved=p.with_name('saved-password');p.rename(saved);p.symlink_to(saved)
        self.failure('STATE_INVALID',self.send);p.unlink();saved.rename(p)
        with mock.patch('time.time',return_value=self.remote['expires_at']+1):self.failure('LINK_EXPIRED',self.send)
        self.assertEqual(self.count('prepare'),0)

    def test_status_offline_and_safe(self):
        self.send();self.config.unlink();self.contacts.unlink();self.password.unlink()
        Path(self.remote['handoff_file']).unlink();Path(self.remote['password_file']).unlink()
        self.assertEqual(self.status()['status'],'channel-accepted')
        from file_delivery import notification
        self.failure('TASK_NOT_FOUND',lambda:notification.status(str(self.f.state),'missing'))

    def test_real_fork_crash_boundaries(self):
        context=multiprocessing.get_context('fork')
        for stage in ('before_submit','after_submit'):
            with self.subTest(stage=stage):
                key='crash-'+stage;before=self.count('submit')
                def child():
                    def checkpoint(value):
                        if value==stage:os._exit(71)
                    self.send(notification_key=key,checkpoint=checkpoint)
                process=context.Process(target=child);process.start();process.join(15)
                if process.is_alive():process.kill();process.join();self.fail('checkpoint worker hung')
                self.assertEqual(process.exitcode,71)
                self.failure('SMTP_UNKNOWN',lambda:self.send(notification_key=key))
                self.assertEqual(self.count('submit')-before,0 if stage=='before_submit' else 1)

    def test_concurrent_same_key_submits_once(self):
        context=multiprocessing.get_context('fork')
        def child():self.send()
        processes=[context.Process(target=child) for _ in range(2)]
        for process in processes:process.start()
        for process in processes:
            process.join(15)
            if process.is_alive():process.kill();process.join();self.fail('concurrent worker hung')
            self.assertEqual(process.exitcode,0)
        self.assertEqual(self.count('submit'),1)

    def test_production_tls_and_data_boundary(self):
        from file_delivery import notification
        for mode in ('implicit','starttls'):
            with self.subTest(mode=mode):
                client=mock.MagicMock();events=[]
                replies={'ehlo':250,'starttls':220,'login':235,'mail':250,'rcpt':250}
                for method,reply in replies.items():
                    getattr(client,method).side_effect=lambda *a,_name=method,_code=reply,**kw:(events.append(_name) or (_code,b'ok'))
                wire=[]
                def data(payload):
                    # Match smtplib.data's ASCII conversion for str; bytes permit MIME UTF8.
                    raw=payload.encode('ascii') if isinstance(payload,str) else payload
                    self.assertIsInstance(raw,bytes)
                    wire.append(raw);events.append('data')
                    return (250,b'accepted')
                client.data.side_effect=data
                with self.smtp_constructor(notification,client,implicit=mode=='implicit') as factory:
                    transport=notification.SmtpTransport()
                    config={**self.values,'tls':mode,'password':SENTINEL}
                    transport.prepare(config,'sender@example.test','to@example.test')
                    self.assertNotIn('data',events);self.assertIn('mail',events);self.assertIn('rcpt',events)
                    if mode=='starttls':
                        self.assertLess(events.index('starttls'),events.index('login'));ctx=client.starttls.call_args.kwargs['context']
                    else:ctx=factory.call_args.kwargs['context']
                    self.assertEqual(ctx.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(ctx.check_hostname)
                    from email.message import EmailMessage
                    message=EmailMessage();message['From']='sender@example.test';message['To']='to@example.test';message.set_content('文件交付：中文内容 café')
                    result=transport.submit(message,'sender@example.test','to@example.test');transport.close()
                    self.assertEqual(result['status'],'channel-accepted');self.assertEqual(events.count('data'),1)
                    self.assertFalse(client.sendmail.called);self.assertFalse(client.send_message.called)
                    from email import policy
                    from email.parser import BytesParser
                    parsed=BytesParser(policy=policy.default).parsebytes(wire[0])
                    self.assertIn('文件交付：中文内容 café',parsed.get_content())

    def test_starttls_failure_cannot_login_or_submit(self):
        from file_delivery import notification
        client=mock.MagicMock();client.ehlo.return_value=(250,b'ok');client.starttls.side_effect=ssl.SSLError(SENTINEL)
        with self.smtp_constructor(notification,client,implicit=False):
            transport=notification.SmtpTransport()
            with self.assertRaises(Exception):transport.prepare({**self.values,'tls':'starttls','password':SENTINEL},'sender@example.test','to@example.test')
        self.assertFalse(client.login.called);self.assertFalse(client.mail.called);self.assertFalse(client.data.called)

    def test_accepted_history_survives_expiry(self):
        self.send()
        with mock.patch('time.time',return_value=self.remote['expires_at']+1):
            self.assertTrue(self.send()['reused'])
        self.assertEqual(self.count('submit'),1)

    def test_submit_checkpoint_exception_is_safe_unknown(self):
        def checkpoint(stage):
            if stage=='after_submit':raise RuntimeError(SENTINEL)
        self.failure('SMTP_UNKNOWN',lambda:self.send(checkpoint=checkpoint))
        self.failure('SMTP_UNKNOWN',self.send);self.assertEqual(self.count('submit'),1)

    def test_production_recipient_rejection_is_before_data(self):
        from file_delivery import notification
        client=mock.MagicMock()
        for method,reply in [('ehlo',250),('login',235),('mail',250)]:getattr(client,method).return_value=(reply,b'ok')
        client.rcpt.return_value=(550,b'synthetic refusal')
        with self.smtp_constructor(notification,client,implicit=True):
            transport=notification.SmtpTransport()
            with self.assertRaises(Exception):transport.prepare({**self.values,'password':SENTINEL},'sender@example.test','to@example.test')
        self.assertFalse(client.data.called)

    def test_corrupt_notification_database_status_is_state_invalid(self):
        self.send()
        database=self.f.state/'notifications.sqlite3'
        database.write_bytes(b'corrupted notification sqlite fixture')
        self.failure('STATE_INVALID',self.status)

    def test_corrupt_required_remote_database_blocks_prepare(self):
        database=self.f.state/'remote.sqlite3'
        database.write_bytes(b'corrupted remote sqlite fixture')
        self.failure('STATE_INVALID',self.send)
        self.assertEqual(self.count('prepare'),0)
        self.assertEqual(self.count('submit'),0)

    def test_injected_uncontrolled_error_code_is_normalized(self):
        from file_delivery.errors import DeliveryError
        def bad_prepare(config,from_addr,to_addr):
            self.transport.event('prepare')
            raise DeliveryError('SMTP_AUTH:'+SENTINEL,SENTINEL)
        self.transport.prepare=bad_prepare
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=self.failure(None,self.send)
        self.assertIn(code,{'SMTP_CONNECT','SMTP_TLS','SMTP_AUTH','SMTP_REJECTED','SMTP_PREPARE','IO_ERROR'})
        self.assertEqual(self.count('submit'),0)
        status=self.status()
        self.assertNotIn(SENTINEL,json.dumps(status)+out.getvalue()+err.getvalue())
        database=self.f.state/'notifications.sqlite3'
        with sqlite3.connect(database) as db:dump='\n'.join(db.iterdump())
        self.assertNotIn(SENTINEL,dump)
        self.assertNotIn(SENTINEL.encode(),database.read_bytes())

    def test_busy_deadline_while_another_process_prepares(self):
        from file_delivery import notification  # fail discovery execution promptly if unimplemented
        context=multiprocessing.get_context('fork')
        entered=context.Event();release=context.Event();results=context.Queue()
        def child():
            original=self.transport.prepare
            def blocking_prepare(config,from_addr,to_addr):
                original(config,from_addr,to_addr)
                entered.set()
                if not release.wait(15):raise RuntimeError('controller release deadline exceeded')
            self.transport.prepare=blocking_prepare
            try:results.put({'result':self.send()})
            except BaseException as exc:results.put({'error_type':type(exc).__name__,'code':getattr(exc,'code',None)})
        process=context.Process(target=child);process.start()
        try:
            self.assertTrue(entered.wait(5),'first process did not reach prepare')
            started=time.monotonic()
            self.failure('BUSY',self.send)
            self.assertLess(time.monotonic()-started,7.0)
            self.assertEqual(self.count('submit'),0)
            self.assertEqual(self.count('prepare'),1,'contender must not prepare another SMTP session')
        finally:
            release.set();process.join(10)
            if process.is_alive():process.kill();process.join();self.fail('released sender did not terminate')
        self.assertEqual(process.exitcode,0)
        result=results.get(timeout=2)
        self.assertIn('result',result)
        self.assertEqual(result['result']['status'],'channel-accepted')
        self.assertEqual(self.count('submit'),1)
        results.close();results.join_thread()

    def test_cli_help_and_offline_status(self):
        env={**os.environ,'PYTHONPATH':str(PROJECT/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
        for cmd in ('send-email','status-email'):
            done=subprocess.run([sys.executable,'-m','file_delivery',cmd,'--help'],capture_output=True,text=True,env=env,timeout=10)
            self.assertEqual(done.returncode,0,done.stderr)
        self.send()
        done=subprocess.run([sys.executable,'-m','file_delivery','status-email','--state-dir',str(self.f.state),'--key',self.key,'--json'],capture_output=True,text=True,env=env,timeout=10)
        self.assertEqual(done.returncode,0,done.stderr);self.assertEqual(json.loads(done.stdout)['status'],'channel-accepted');self.assertNotIn(SENTINEL,done.stdout+done.stderr)

if __name__=='__main__':unittest.main()
