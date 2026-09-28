"""Qiniu private-object store boundary for remote delivery (FD-004A).

All provider traffic goes through ``requests.Session.request`` on an
injectable session. Credentials live only in the owner-only JSON config and
never appear in results, error messages or logs. Endpoints are derived from
the frozen region list; the management token is produced by the qiniu SDK
Auth implementation, never sent to a configurable host.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from file_delivery import errors

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
UPLOAD_TOKEN_EXPIRES = 3600
DEFAULT_LINK_TTL = 604800
MAX_LINK_TTL = 604800
MAX_KEY_BYTES = 1024
CHUNK_SIZE = 65536

SUPPORTED_REGIONS = frozenset({"z0", "z1", "z2", "na0", "as0", "cn-east-2"})

UC_HOST = "uc.qiniuapi.com"

_REQUIRED_STRINGS = ("access_key", "secret_key", "bucket", "region", "download_domain")

_BUCKET_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")


def _invalid(detail: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.CONFIG_INVALID, "invalid qiniu config: " + detail)


def _load_qiniu():
    try:
        from qiniu import Auth
        from qiniu.utils import etag_stream, urlsafe_base64_encode
    except ImportError:
        raise errors.DeliveryError(
            errors.DEPENDENCY_MISSING,
            "optional dependency 'qiniu' is required: install with pip install 'file-delivery[qiniu]'",
        ) from None
    return Auth, urlsafe_base64_encode, etag_stream


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_download_domain(domain: str) -> None:
    if not isinstance(domain, str) or not domain.startswith("https://"):
        raise _invalid("download_domain must be an HTTPS origin")
    # urlsplit silently strips tab/CR/LF, so check the raw value before parsing
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in domain):
        raise _invalid("download_domain contains control characters")
    rest = domain[len("https://"):]
    if not rest or rest.startswith("/"):
        raise _invalid("download_domain must be an HTTPS origin")
    try:
        parts = urlsplit(domain)
        host = parts.hostname
        parts.port
    except ValueError:
        raise _invalid("download_domain is not a valid URL origin") from None
    if parts.scheme != "https" or parts.query or parts.fragment:
        raise _invalid("download_domain must not contain query or fragment")
    if parts.path not in ("", "/"):
        raise _invalid("download_domain must not contain a path")
    if parts.username is not None or parts.password is not None:
        raise _invalid("download_domain must not contain credentials")
    if not host or "." not in host:
        raise _invalid("download_domain must be a domain origin")
    host = host.rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise _invalid("download_domain must not be localhost")
    if _IPV4_RE.match(host):
        raise _invalid("download_domain must not be an IP literal")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _invalid("download_domain must not be an IP literal")
    for label in host.split("."):
        if not _LABEL_RE.match(label):
            raise _invalid("download_domain is not a valid domain")


def _validate_timeout(value) -> int:
    if not _is_int(value) or not 1 <= value <= MAX_TIMEOUT_SECONDS:
        raise _invalid("timeout_seconds must be an integer between 1 and 120")
    return value


def _read_config(path: Path) -> dict:
    for depth in range(len(path.parts), 0, -1):
        partial = Path(*path.parts[:depth])
        try:
            mode = os.lstat(partial).st_mode
        except OSError:
            raise _invalid("config path is not readable") from None
        if stat.S_ISLNK(mode):
            raise _invalid("config path must not contain symlinks")
    try:
        info = os.lstat(path)
    except OSError:
        raise _invalid("config file is not readable") from None
    if not stat.S_ISREG(info.st_mode):
        raise _invalid("config file must be a regular file")
    perms = info.st_mode & 0o777
    if perms & 0o077 or perms > 0o600:
        raise _invalid("config file must be owner-only (mode 0600 or stricter)")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise _invalid("config file is not valid JSON") from None
    if not isinstance(data, dict):
        raise _invalid("config must be a JSON object")
    for field in _REQUIRED_STRINGS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise _invalid(f"{field} must be a non-empty string")
    if not _BUCKET_RE.match(data["bucket"]) or len(data["bucket"]) > 63:
        raise _invalid("bucket is not DNS-safe")
    if data["region"] not in SUPPORTED_REGIONS:
        raise _invalid("region is not a supported Qiniu region")
    _validate_download_domain(data["download_domain"])
    if "timeout_seconds" in data:
        data["timeout_seconds"] = _validate_timeout(data["timeout_seconds"])
    else:
        data["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
    return data


def _production_session():
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    session.trust_env = False
    adapter = HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class QiniuStore:
    """Boundary to a private Qiniu bucket; every operation is single-attempt."""

    def __init__(self, *, access_key, secret_key, bucket, region, download_domain,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS, session=None) -> None:
        Auth, _, _ = _load_qiniu()
        self._auth = Auth(access_key, secret_key)
        self._access_key = access_key
        self._bucket = bucket
        self._region = region
        self._domain = download_domain.rstrip("/")
        self._timeout = timeout_seconds
        self._session = session if session is not None else _production_session()

    @classmethod
    def from_file(cls, path, *, session=None) -> "QiniuStore":
        data = _read_config(Path(path))
        return cls(
            access_key=data["access_key"],
            secret_key=data["secret_key"],
            bucket=data["bucket"],
            region=data["region"],
            download_domain=data["download_domain"],
            timeout_seconds=data["timeout_seconds"],
            session=session,
        )

    def __repr__(self) -> str:  # never expose credentials
        return (f"QiniuStore(bucket={self._bucket!r}, region={self._region!r}, "
                f"timeout_seconds={self._timeout})")

    def identity(self) -> dict:
        """Stable non-secret identity of this store's destination bucket."""
        return {
            "provider": "qiniu",
            "bucket": self._bucket,
            "region": self._region,
            "download_domain": self._domain,
            "account_id": hashlib.sha256(self._access_key.encode("utf-8")).hexdigest(),
        }

    # ---- transport ----------------------------------------------------

    def _request(self, method: str, url: str, **kwargs):
        if not url.startswith("https://"):
            raise errors.DeliveryError(errors.REMOTE_ERROR, "refusing non-HTTPS provider URL")
        try:
            response = self._session.request(
                method, url, allow_redirects=False, verify=True,
                timeout=self._timeout, **kwargs)
        except Exception:
            raise errors.DeliveryError(
                errors.REMOTE_UNKNOWN, "provider request failed (outcome unknown)") from None
        return response

    def _map_status(self, status: int, action: str) -> errors.DeliveryError:
        if status in (401, 403):
            return errors.DeliveryError(errors.REMOTE_AUTH, f"{action} rejected by provider auth (HTTP {status})")
        if 500 <= status < 600:
            return errors.DeliveryError(errors.REMOTE_UNKNOWN, f"{action} failed (HTTP {status}, outcome unknown)")
        return errors.DeliveryError(errors.REMOTE_ERROR, f"{action} failed with HTTP {status}")

    def _management_headers(self, url: str) -> dict:
        token = self._auth.token_of_request(url)
        return {"Authorization": f"QBox {token}"}

    def _rs_url(self, action: str, key: str) -> str:
        _, urlsafe_b64, _ = _load_qiniu()
        resource = urlsafe_b64(f"{self._bucket}:{key}".encode("utf-8"))
        return f"https://rs-{self._region}.qiniuapi.com/{action}/{resource}"

    def _origin_url(self, key: str) -> str:
        return f"{self._domain}/{quote(key, safe='/')}"

    # ---- argument validation ------------------------------------------

    def _validate_key(self, key) -> str:
        if not isinstance(key, str) or not key or key.startswith("/") or "\\" in key:
            raise errors.DeliveryError(errors.INVALID_OBJECT_KEY, "object key is not a safe relative path")
        try:
            key_bytes = key.encode("utf-8")
        except UnicodeEncodeError:
            raise errors.DeliveryError(
                errors.INVALID_OBJECT_KEY, "object key is not valid UTF-8 text") from None
        if len(key_bytes) > MAX_KEY_BYTES:
            raise errors.DeliveryError(errors.INVALID_OBJECT_KEY, "object key exceeds 1024 UTF-8 bytes")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
            raise errors.DeliveryError(errors.INVALID_OBJECT_KEY, "object key contains control characters")
        pure = PurePosixPath(key)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in key.split("/")):
            raise errors.DeliveryError(errors.INVALID_OBJECT_KEY, "object key is not a safe relative path")
        return key

    @staticmethod
    def _validate_ttl(ttl) -> int:
        if not _is_int(ttl) or not 1 <= ttl <= MAX_LINK_TTL:
            raise errors.DeliveryError(errors.CONFIG_INVALID, "ttl_seconds must be an integer between 1 and 604800")
        return ttl

    # ---- provider operations ------------------------------------------

    def ensure_private(self) -> None:
        """Require integer private=1 from the real Qiniu bucketInfo API."""
        url = f"https://{UC_HOST}/v2/bucketInfo?bucket={self._bucket}"
        response = self._request("GET", url, headers=self._management_headers(url))
        try:
            if response.status_code != 200:
                raise self._map_status(response.status_code, "bucket info query")
            try:
                data = response.json()
            except ValueError:
                raise errors.DeliveryError(errors.BUCKET_NOT_PRIVATE, "bucket privacy could not be confirmed") from None
        finally:
            response.close()
        if not isinstance(data, dict):
            raise errors.DeliveryError(errors.BUCKET_NOT_PRIVATE, "bucket privacy could not be confirmed")
        private = data.get("private")
        if not _is_int(private) or private != 1:
            raise errors.DeliveryError(errors.BUCKET_NOT_PRIVATE, "bucket is not private")

    def upload(self, path, key, retention_days=30) -> dict:
        if not _is_int(retention_days) or not 1 <= retention_days <= 3650:
            raise errors.DeliveryError(errors.CONFIG_INVALID, "retention_days must be an integer between 1 and 3650")
        self._validate_key(key)
        source = Path(path)
        try:
            info = os.lstat(source)
        except OSError:
            raise errors.DeliveryError(errors.IO_ERROR, "upload source is not readable") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise errors.DeliveryError(errors.IO_ERROR, "upload source must be a regular non-symlink file")
        self.ensure_private()
        try:
            with open(source, "rb") as handle:
                data = handle.read()
        except OSError:
            raise errors.DeliveryError(errors.IO_ERROR, "upload source is not readable") from None
        _, _, etag_stream = _load_qiniu()
        etag = etag_stream(io.BytesIO(data))
        token = self._auth.upload_token(
            self._bucket, key, expires=UPLOAD_TOKEN_EXPIRES,
            policy={"insertOnly": 1, "deleteAfterDays": retention_days, "fileType": 0})
        url = f"https://up-{self._region}.qiniup.com"
        response = self._request("POST", url, data={"key": key, "token": token},
                                 files={"file": (key, data)})
        try:
            if response.status_code == 614:
                raise errors.DeliveryError(errors.REMOTE_CONFLICT, "remote object already exists")
            if response.status_code != 200:
                raise self._map_status(response.status_code, "upload")
            try:
                result = response.json()
            except ValueError:
                raise errors.DeliveryError(errors.REMOTE_INTEGRITY, "upload response was malformed") from None
        finally:
            response.close()
        if not isinstance(result, dict) or result.get("key") != key:
            raise errors.DeliveryError(errors.REMOTE_INTEGRITY, "uploaded key did not match")
        if result.get("hash") != etag:
            raise errors.DeliveryError(errors.REMOTE_INTEGRITY, "uploaded content did not match local etag")
        return {"status": "uploaded", "key": key, "etag": etag, "size": len(data)}

    def stat(self, key) -> dict | None:
        self._validate_key(key)
        url = self._rs_url("stat", key)
        response = self._request("GET", url, headers=self._management_headers(url))
        try:
            if response.status_code == 612:
                return None
            if response.status_code != 200:
                raise self._map_status(response.status_code, "stat")
            try:
                data = response.json()
            except ValueError:
                raise errors.DeliveryError(errors.REMOTE_ERROR, "stat response was malformed") from None
        finally:
            response.close()
        etag = data.get("hash") if isinstance(data, dict) else None
        size = data.get("fsize") if isinstance(data, dict) else None
        if not isinstance(etag, str) or not etag or not _is_int(size) or size < 0:
            raise errors.DeliveryError(errors.REMOTE_ERROR, "stat response was malformed")
        return {"key": key, "etag": etag, "size": size}

    def signed_url(self, key, ttl_seconds=DEFAULT_LINK_TTL) -> dict:
        self._validate_key(key)
        self._validate_ttl(ttl_seconds)
        deadline = int(time.time()) + ttl_seconds
        base = f"{self._origin_url(key)}?e={deadline}"
        token = self._auth.token(base)
        return {"url": f"{base}&token={token}", "expires_at": deadline}

    def verify_download(self, key, expected_sha256, expected_size) -> dict:
        if not isinstance(expected_sha256, str) or not _SHA256_HEX.match(expected_sha256):
            raise errors.DeliveryError(errors.CONFIG_INVALID, "expected_sha256 must be 64 lowercase hex chars")
        if not _is_int(expected_size) or expected_size < 0:
            raise errors.DeliveryError(errors.CONFIG_INVALID, "expected_size must be a non-negative integer")
        self._validate_key(key)
        anonymous = self._request("GET", self._origin_url(key), stream=True)
        try:
            if 200 <= anonymous.status_code < 300:
                raise errors.DeliveryError(errors.BUCKET_NOT_PRIVATE, "object is anonymously downloadable")
            if anonymous.status_code not in (401, 403):
                raise errors.DeliveryError(errors.REMOTE_ERROR, "privacy probe got an unexpected result")
        finally:
            anonymous.close()
        signed = self.signed_url(key)
        response = self._request("GET", signed["url"], stream=True)
        try:
            if response.status_code != 200:
                raise self._map_status(response.status_code, "signed download")
            digest = hashlib.sha256()
            size = 0
            exceeded = False
            try:
                for chunk in response.iter_content(CHUNK_SIZE):
                    if not chunk:
                        continue
                    digest.update(chunk)
                    size += len(chunk)
                    if size > expected_size:
                        exceeded = True
                        break
            except Exception:
                raise errors.DeliveryError(
                    errors.REMOTE_UNKNOWN, "download stream was interrupted (outcome unknown)") from None
        finally:
            response.close()
        if exceeded or size != expected_size or digest.hexdigest() != expected_sha256:
            raise errors.DeliveryError(errors.REMOTE_INTEGRITY, "downloaded content did not match the expected hash or size")
        return {"status": "link-verified", "sha256": digest.hexdigest(), "size": size}

    def delete(self, key) -> dict:
        self._validate_key(key)
        url = self._rs_url("delete", key)
        response = self._request("POST", url, headers=self._management_headers(url))
        try:
            if response.status_code not in (200, 612):
                raise self._map_status(response.status_code, "delete")
        finally:
            response.close()
        if self.stat(key) is not None:
            raise errors.DeliveryError(errors.REMOTE_DELETE_UNCONFIRMED, "object still present after deletion")
        return {"status": "object-deleted", "key": key}
