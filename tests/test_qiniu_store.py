"""Unit tests for the Qiniu private-object store boundary (worker-owned).

Only synthetic configuration and an injected requests-compatible session are
used; no live provider access is authorized here.
"""

import base64
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import requests

from file_delivery import errors, qiniu_store

try:
    from qiniu import Auth
    from qiniu.utils import etag_stream
except ImportError:  # controller environment provides qiniu
    Auth = None

QINIU_AVAILABLE = Auth is not None

CONFIG = {
    "access_key": "ak-test-000000000000",
    "secret_key": "sk-test-000000000000",
    "bucket": "delivery-bucket",
    "region": "z0",
    "download_domain": "https://dl.example.com/",
}

CONTENT = b"encrypted bundle payload"
KEY = "bundles/2026/09/archive.zip"


class FakeResponse(requests.Response):
    def __init__(self, status, body=b""):
        super().__init__()
        self.status_code = status
        self._content = body
        self._content_consumed = True
        self.closed_count = 0

    def close(self):
        self.closed_count += 1


class FakeSession:
    """requests.Session.request-compatible transport double."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


def _seq_handler(responses):
    items = list(responses)

    def handler(method, url, kwargs):
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, FakeResponse):
            return item(method, url, kwargs)
        return item
    return handler


def _bucket_info(private=1):
    return FakeResponse(200, json.dumps({"private": private}).encode())


def _write_config(base, data=CONFIG, mode=0o600, name="qiniu.json"):
    path = Path(base) / name
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _store(session, data=CONFIG):
    return qiniu_store.QiniuStore(
        access_key=data["access_key"], secret_key=data["secret_key"],
        bucket=data["bucket"], region=data["region"],
        download_domain=data["download_domain"],
        timeout_seconds=data.get("timeout_seconds", 30), session=session)


@unittest.skipUnless(QINIU_AVAILABLE, "qiniu not installed")
class FromFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_valid_config_and_safe_repr(self):
        store = qiniu_store.QiniuStore.from_file(_write_config(self.base))
        self.assertEqual(store._bucket, CONFIG["bucket"])
        self.assertNotIn(CONFIG["access_key"], repr(store))
        self.assertNotIn(CONFIG["secret_key"], repr(store))
        self.assertIn("delivery-bucket", repr(store))

    def test_rejects_loose_permissions(self):
        path = _write_config(self.base, mode=0o644)
        with self.assertRaises(errors.DeliveryError) as ctx:
            qiniu_store.QiniuStore.from_file(path)
        self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)

    def test_rejects_symlink(self):
        path = _write_config(self.base)
        link = self.base / "link.json"
        link.symlink_to(path)
        with self.assertRaises(errors.DeliveryError) as ctx:
            qiniu_store.QiniuStore.from_file(link)
        self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)

    def test_rejects_control_characters_in_download_domain(self):
        session = FakeSession(lambda *a: None)  # rejection must happen before network
        raw = [
            "https://dl.example.com/\n",
            "https://dl.example.com/\r",
            "https://dl.example.com/\r\n",
            "https://dl.example\t.com/",
            "https://dl.example.com/\t",
            "https://dl.example.com/\x00",
            "https://dl.example.com/\x1f",
            "https://dl.example.com/\x7f",
        ]
        for domain in raw:
            data = {**CONFIG, "download_domain": domain}
            path = _write_config(self.base, data)
            with self.assertRaises(errors.DeliveryError, msg=repr(domain)) as ctx:
                qiniu_store.QiniuStore.from_file(path, session=session)
            self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)
        self.assertEqual(session.calls, [])

    def test_rejects_bad_fields(self):
        bad = [
            {**CONFIG, "region": "z9"},
            {**CONFIG, "bucket": "Bad_Bucket"},
            {**CONFIG, "bucket": "delivery-bucket\n"},
            {**CONFIG, "download_domain": "http://dl.example.com/"},
            {**CONFIG, "download_domain": "https://dl.example.com/path/"},
            {**CONFIG, "download_domain": "https://dl.example.com/?q=1"},
            {**CONFIG, "download_domain": "https://ak:sk@dl.example.com/"},
            {**CONFIG, "download_domain": "https://127.0.0.1/"},
            {**CONFIG, "download_domain": "https://localhost/"},
            {**CONFIG, "download_domain": "https://[bad/"},
            {**CONFIG, "access_key": ""},
            {k: v for k, v in CONFIG.items() if k != "secret_key"},
            {**CONFIG, "timeout_seconds": True},
            {**CONFIG, "timeout_seconds": 0},
            {**CONFIG, "timeout_seconds": 121},
            {**CONFIG, "timeout_seconds": None},
            {**CONFIG, "timeout_seconds": 1.5},
        ]
        for data in bad:
            with self.assertRaises(errors.DeliveryError, msg=json.dumps(data)[:80]) as ctx:
                qiniu_store.QiniuStore.from_file(_write_config(self.base, data))
            self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)


@unittest.skipUnless(QINIU_AVAILABLE, "qiniu not installed")
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.file = self.base / "bundle.zip"
        self.file.write_bytes(CONTENT)

    def _upload_responses(self):
        return [
            _bucket_info(),
            FakeResponse(200, json.dumps(
                {"key": KEY, "hash": etag_stream(io_bytes(CONTENT))}).encode()),
        ]

    def test_upload_uses_private_check_then_form_upload(self):
        session = FakeSession(_seq_handler(self._upload_responses()))
        result = _store(session).upload(str(self.file), KEY, retention_days=7)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["key"], KEY)
        self.assertEqual(result["size"], len(CONTENT))
        self.assertEqual(result["etag"], etag_stream(io_bytes(CONTENT)))
        bucket_call, upload_call = session.calls
        self.assertEqual(bucket_call["method"], "GET")
        self.assertIn("https://uc.qiniuapi.com/v2/bucketInfo?bucket=", bucket_call["url"])
        self.assertTrue(bucket_call["headers"]["Authorization"].startswith("QBox "))
        self.assertEqual(upload_call["method"], "POST")
        self.assertEqual(upload_call["url"], "https://up-z0.qiniup.com")
        self.assertEqual(upload_call["timeout"], 30)
        self.assertFalse(upload_call["allow_redirects"])
        self.assertEqual(upload_call["data"]["key"], KEY)
        policy = _decode_token(upload_call["data"]["token"])
        self.assertEqual(policy["scope"], f"{CONFIG['bucket']}:{KEY}")
        self.assertEqual(policy["insertOnly"], 1)
        self.assertEqual(policy["deleteAfterDays"], 7)
        self.assertEqual(policy["fileType"], 0)

    def test_upload_conflict_and_integrity(self):
        session = FakeSession(_seq_handler([_bucket_info(), FakeResponse(614, b"")]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(session).upload(str(self.file), KEY)
        self.assertEqual(ctx.exception.code, errors.REMOTE_CONFLICT)

        bad_key = FakeSession(_seq_handler(
            [_bucket_info(), FakeResponse(200, b'{"key": "other", "hash": "h"}')]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(bad_key).upload(str(self.file), KEY)
        self.assertEqual(ctx.exception.code, errors.REMOTE_INTEGRITY)

    def test_upload_argument_validation(self):
        session = FakeSession(lambda *a: None)
        store = _store(session)
        for key in ["", "/abs", "a/../b", ".", "a\\b", "a\x01b", "a//b", "x" * 1025]:
            with self.assertRaises(errors.DeliveryError) as ctx:
                store.upload(str(self.file), key)
            self.assertEqual(ctx.exception.code, errors.INVALID_OBJECT_KEY)
        for days in [0, 3651, True, "30", 30.0]:
            with self.assertRaises(errors.DeliveryError) as ctx:
                store.upload(str(self.file), KEY, retention_days=days)
            self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)
        for path in [str(self.base / "missing.zip")]:
            with self.assertRaises(errors.DeliveryError) as ctx:
                store.upload(path, KEY)
            self.assertEqual(ctx.exception.code, errors.IO_ERROR)
        link = self.base / "link.zip"
        link.symlink_to(self.file)
        with self.assertRaises(errors.DeliveryError) as ctx:
            store.upload(str(link), KEY)
        self.assertEqual(ctx.exception.code, errors.IO_ERROR)
        self.assertEqual(session.calls, [])  # validation happens before network

    def test_ensure_private_rejects_non_private(self):
        for body in [b'{"private": 0}', b'{"private": true}', b"{}", b"not json"]:
            session = FakeSession(_seq_handler([FakeResponse(200, body)]))
            with self.assertRaises(errors.DeliveryError) as ctx:
                _store(session).ensure_private()
            self.assertEqual(ctx.exception.code, errors.BUCKET_NOT_PRIVATE)

    def test_stat_mapping(self):
        ok = FakeSession(_seq_handler([FakeResponse(200, json.dumps(
            {"hash": "etag-1", "fsize": len(CONTENT)}).encode())]))
        self.assertEqual(_store(ok).stat(KEY),
                         {"key": KEY, "etag": "etag-1", "size": len(CONTENT)})
        missing = _store(FakeSession(_seq_handler([FakeResponse(612)])))
        self.assertIsNone(missing.stat(KEY))
        for status, code in [(401, errors.REMOTE_AUTH), (403, errors.REMOTE_AUTH),
                             (400, errors.REMOTE_ERROR), (500, errors.REMOTE_UNKNOWN)]:
            session = _store(FakeSession(_seq_handler([FakeResponse(status)])))
            with self.assertRaises(errors.DeliveryError) as ctx:
                session.stat(KEY)
            self.assertEqual(ctx.exception.code, code)
        malformed = _store(FakeSession(_seq_handler([FakeResponse(200, b"[]")])))
        with self.assertRaises(errors.DeliveryError) as ctx:
            malformed.stat(KEY)
        self.assertEqual(ctx.exception.code, errors.REMOTE_ERROR)

    def test_signed_url_offline(self):
        session = FakeSession(lambda *a: None)
        result = _store(session).signed_url(KEY, ttl_seconds=120)
        self.assertEqual(session.calls, [])
        self.assertTrue(result["url"].startswith("https://dl.example.com/bundles/2026/09/archive.zip?e="))
        self.assertIn("e=", result["url"])
        self.assertIn("token=", result["url"])
        self.assertGreater(result["expires_at"], 0)
        for ttl in [0, 604801, True, "60"]:
            with self.assertRaises(errors.DeliveryError) as ctx:
                _store(session).signed_url(KEY, ttl_seconds=ttl)
            self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)

    def test_verify_download_roundtrip(self):
        digest = hashlib.sha256(CONTENT).hexdigest()

        def handler(method, url, kwargs):
            if "e=" not in url and "token=" not in url:
                return FakeResponse(401)
            return FakeResponse(200, CONTENT)

        session = FakeSession(handler)
        result = _store(session).verify_download(KEY, digest, len(CONTENT))
        self.assertEqual(result, {"status": "link-verified", "sha256": digest, "size": len(CONTENT)})
        self.assertNotIn("url", result)
        anon_call, signed_call = session.calls
        self.assertEqual(anon_call["url"], f"https://dl.example.com/{KEY}")
        self.assertTrue(anon_call["stream"])
        self.assertIn("token=", signed_call["url"])
        self.assertTrue(signed_call["stream"])

    def test_verify_download_rejects_public_and_mismatch(self):
        digest = hashlib.sha256(CONTENT).hexdigest()
        public = FakeSession(_seq_handler([FakeResponse(200, CONTENT)]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(public).verify_download(KEY, digest, len(CONTENT))
        self.assertEqual(ctx.exception.code, errors.BUCKET_NOT_PRIVATE)

        wrong = FakeSession(_seq_handler([FakeResponse(403), FakeResponse(200, b"tampered")]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(wrong).verify_download(KEY, digest, len(CONTENT))
        self.assertEqual(ctx.exception.code, errors.REMOTE_INTEGRITY)

        larger = FakeSession(_seq_handler(
            [FakeResponse(403), FakeResponse(200, CONTENT + b"extra")]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(larger).verify_download(KEY, digest, len(CONTENT))
        self.assertEqual(ctx.exception.code, errors.REMOTE_INTEGRITY)

        for sha, size in [("XYZ", len(CONTENT)), (digest, -1), (digest, True),
                          (digest + "\n", len(CONTENT)), (digest.upper(), len(CONTENT))]:
            session = FakeSession(lambda *a: None)
            with self.assertRaises(errors.DeliveryError) as ctx:
                _store(session).verify_download(KEY, sha, size)
            self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)
            self.assertEqual(session.calls, [])  # rejected before any request

    def test_verify_download_stream_interruption_is_sanitized(self):
        digest = hashlib.sha256(CONTENT).hexdigest()
        marker = "secret-token-marker"

        class BrokenStreamResponse(FakeResponse):
            def iter_content(self, chunk_size):
                raise requests.exceptions.ConnectionError(marker)

        def handler(method, url, kwargs):
            if "token=" not in url:
                return FakeResponse(403)
            return BrokenStreamResponse(200)

        session = FakeSession(handler)
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(session).verify_download(KEY, digest, len(CONTENT))
        self.assertEqual(ctx.exception.code, errors.REMOTE_UNKNOWN)
        self.assertNotIn(marker, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_non_utf8_key_rejected_before_network(self):
        session = FakeSession(lambda *a: None)
        store = _store(session)
        surrogate = "bundles/\udcff"
        for call in [
            lambda: store.upload(str(self.file), surrogate),
            lambda: store.stat(surrogate),
            lambda: store.signed_url(surrogate),
            lambda: store.delete(surrogate),
            lambda: store.verify_download(surrogate, "0" * 64, 1),
        ]:
            with self.assertRaises(errors.DeliveryError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, errors.INVALID_OBJECT_KEY)
        self.assertEqual(session.calls, [])

    def test_delete_confirms_via_stat(self):
        session = FakeSession(_seq_handler(
            [FakeResponse(200), FakeResponse(612)]))
        result = _store(session).delete(KEY)
        self.assertEqual(result, {"status": "object-deleted", "key": KEY})
        delete_call, stat_call = session.calls
        self.assertEqual(delete_call["method"], "POST")
        self.assertIn("rs-z0.qiniuapi.com/delete/", delete_call["url"])
        self.assertIn("rs-z0.qiniuapi.com/stat/", stat_call["url"])

        still = FakeSession(_seq_handler(
            [FakeResponse(200), FakeResponse(200, json.dumps({"hash": "h", "fsize": 1}).encode())]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(still).delete(KEY)
        self.assertEqual(ctx.exception.code, errors.REMOTE_DELETE_UNCONFIRMED)

    def test_transport_failure_semantics(self):
        session = FakeSession(_seq_handler([requests.exceptions.Timeout()]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(session).stat(KEY)
        self.assertEqual(ctx.exception.code, errors.REMOTE_UNKNOWN)

        server = FakeSession(_seq_handler([FakeResponse(503)]))
        with self.assertRaises(errors.DeliveryError) as ctx:
            _store(server).stat(KEY)
        self.assertEqual(ctx.exception.code, errors.REMOTE_UNKNOWN)

    def test_response_bodies_are_closed(self):
        responses = [_bucket_info(), FakeResponse(200, json.dumps(
            {"key": KEY, "hash": etag_stream(io_bytes(CONTENT))}).encode())]
        session = FakeSession(_seq_handler(responses))
        _store(session).upload(str(self.file), KEY)
        self.assertEqual(responses[0].closed_count, 1)
        self.assertEqual(responses[1].closed_count, 1)


@unittest.skipUnless(QINIU_AVAILABLE, "qiniu not installed")
class ProbeLinkTests(unittest.TestCase):
    SIGNED = ("https://dl.example.com/bundles/2026/09/archive.zip"
              "?e=4102444800&token=abc-signature")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def _probe(self, handler, data=None):
        session = FakeSession(handler)
        store = _store(session, data or CONFIG)
        return store, session, store.probe_link(self.SIGNED)

    def test_accessible_only_on_http_200(self):
        response = FakeResponse(200, b"never downloaded")

        def handler(method, url, kwargs):
            self.assertEqual(kwargs.get("stream"), True)
            self.assertEqual(kwargs.get("allow_redirects"), False)
            # A None header value means "remove this header" in requests, so
            # the effective value must be absent or falsy, never a secret.
            probe_headers = kwargs.get("headers") or {}
            self.assertFalse(probe_headers.get("Authorization"))
            self.assertFalse(probe_headers.get("Cookie"))
            return response

        _, session, result = self._probe(handler)
        self.assertEqual(result, {"status": "link-accessible", "http_status": 200})
        self.assertEqual(len(session.calls), 1)  # single attempt, no retry
        self.assertEqual(response.closed_count, 1)
        self.assertEqual(session.calls[0]["url"], self.SIGNED)

    def test_status_mapping(self):
        for status, expected in [
                (401, "link-unavailable"), (403, "link-unavailable"),
                (404, "link-unavailable"), (612, "link-unavailable"),
                (302, "link-unknown"), (500, "link-unknown"), (613, "link-unknown")]:
            _, _, result = self._probe(_seq_handler([FakeResponse(status)]))
            self.assertEqual(result, {"status": expected, "http_status": status})

    def test_network_exception_is_unknown_without_status(self):
        _, _, result = self._probe(
            _seq_handler([requests.exceptions.ConnectionError("boom")]))
        self.assertEqual(result, {"status": "link-unknown", "http_status": None})

    def test_unsafe_urls_rejected_before_request(self):
        session = FakeSession(lambda *a: None)
        store = _store(session)
        urls = [
            "http://dl.example.com/key.zip?token=x",     # wrong scheme
            "//dl.example.com/key.zip",                  # scheme-relative
            "https://evil.example.com/key.zip?token=x",  # other host
            "https://dl.example.com:8443/key.zip",       # other port
            "https://ak:sk@dl.example.com/key.zip",      # credentials
            "https://dl.example.com/",                   # empty object path
            "https://dl.example.com",                    # empty object path
            "https://dl.example.com/key.zip#frag",       # fragment
            "https://dl.example.com/key.zip?token=a\tb",  # raw control char
            "https://dl.example.com/key\x7f.zip",        # DEL
            "https://[invalid/key.zip",                  # invalid parsing
            "", None, 123,
        ]
        for url in urls:
            with self.assertRaises(errors.DeliveryError, msg=repr(url)) as ctx:
                store.probe_link(url)
            self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)
            self.assertNotIn("token", str(ctx.exception))
        self.assertEqual(session.calls, [])


def io_bytes(data):
    import io
    return io.BytesIO(data)


def _decode_token(token):
    payload = token.split(":", 2)[2]
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


if __name__ == "__main__":
    unittest.main()
