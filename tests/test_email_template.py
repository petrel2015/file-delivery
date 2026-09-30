"""FD-007 deterministic MIME, mobile content and escaped-data regressions."""
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest

from file_delivery import email_template, notification


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'a':
            self.links.append(dict(attrs))


class TemplateTests(unittest.TestCase):
    def test_mime_roundtrip_and_no_attachments(self):
        url = 'https://files.example.test/archive.zip?e=1790777108&token=synthetic'
        msg = notification._build_message('<test@example.test>', 'test',
            'sender@example.test', 'reader@example.test', {'url': url},
            'Synthetic-password', {'archive_size': 243, 'expires_at': 1790777108})
        parsed = BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
        self.assertEqual(parsed.get_content_type(), 'multipart/alternative')
        self.assertEqual([part.get_content_type() for part in parsed.iter_parts()],
                         ['text/plain', 'text/html'])
        self.assertFalse(list(parsed.iter_attachments()))
        self.assertEqual(parsed['Message-ID'], '<test@example.test>')
        for kind in ('plain', 'html'):
            body = parsed.get_body(preferencelist=(kind,)).get_content()
            for value in ('Synthetic-password', '243', '2026-09-30 14:05:08 UTC'):
                self.assertIn(value, body)
        self.assertIn(url, parsed.get_body(preferencelist=('plain',)).get_content())

    def test_html_escapes_text_and_link_attributes(self):
        url = 'https://files.example.test/a?token=x&note=" onclick="bad'
        password = '<script>alert("x")</script>&secret'
        _, html = email_template.render(url, password, 123, 1790777108)
        parser = Links(); parser.feed(html)
        self.assertEqual(len(parser.links), 2)
        for link in parser.links:
            self.assertEqual(link['href'], url)
            self.assertNotIn('onclick', link)
        self.assertNotIn('script', parser.tags)
        self.assertNotIn('img', parser.tags)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('word-break:break-all', html)

    def test_original_send_privacy_and_headers_with_html(self):
        # Preserve all assertions from the frozen FD-005 message test,
        # adapting only its superseded top-level plain MIME assumption.
        spec = importlib.util.spec_from_file_location('fd007_fixture',
            Path(__file__).parent / 'fd005/test_fd005_notification.py')
        fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
        test = fixture.NotificationTests(); test.setUp(); self.addCleanup(test.doCleanups)
        result = test.send()
        self.assertEqual(result['status'], 'channel-accepted')
        for field in ('notification_id','key','delivery_task_id','message_id','reused'):
            self.assertIn(field, result)
        self.assertFalse(result['reused'])
        self.assertEqual(result['delivery_task_id'], test.remote['task_id'])
        self.assertEqual(result['receipt_status'], 'unverified')
        self.assertEqual(result['read_status'], 'unverified')
        msg = test.transport.messages[0]
        self.assertEqual(msg.get_all('To'), ['Shop@example.test'])
        self.assertEqual(msg.get_all('From'), ['sender@example.test'])
        self.assertEqual(msg['Message-ID'], result['message_id'])
        self.assertFalse(list(msg.iter_attachments()))
        handoff = json.loads(Path(test.remote['handoff_file']).read_text())
        password = Path(test.remote['password_file']).read_text().strip()
        body = msg.get_body(preferencelist=('plain',)).get_content()
        for value in (handoff['url'], password, str(test.remote['archive_size']), str(test.remote['expires_at'])):
            self.assertIn(value, body)
        with sqlite3.connect(test.f.state / 'notifications.sqlite3') as db:
            dump = '\n'.join(db.iterdump())
        for text in (json.dumps(result), json.dumps(test.status()), dump):
            for value in (handoff['url'], password, fixture.SENTINEL):
                self.assertNotIn(value, text)
        self.assertEqual((test.f.state / 'notifications.sqlite3').stat().st_mode & 0o777, 0o600)
        self.assertTrue(test.send()['reused'])
        self.assertEqual(test.count('submit'), 1)


if __name__ == '__main__':
    unittest.main()
