"""FD-005 email notification of one verified remote handoff over TLS SMTP.

The notification ledger (``notifications.sqlite3``) is separate from the
remote lifecycle and strictly read-only towards it. The stable Message-ID
and notification id are persisted before any network activity; once a
submission may have begun, the outcome is authoritative ``unknown`` and the
same key is never resubmitted. Secrets (SMTP password, signed URL, archive
password) live only in memory and in their pre-existing private files.
"""

from __future__ import annotations

import contextlib
import hashlib
import functools
import ipaddress
import json
import math
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import stat as stat_module
import time
from email import policy
from email.message import EmailMessage
from pathlib import Path

from file_delivery import archive, contacts, email_template, errors, ledger, remote

SCHEMA_VERSION = 1

DB_NAME = "notifications.sqlite3"

STATE_PREPARED = "prepared"
STATE_SENDING = "sending"
STATE_ACCEPTED = "channel-accepted"
STATE_FAILED = "failed-before-send"
STATE_UNKNOWN = "unknown"

SMTP_PREPARE_VOCAB = frozenset((
    errors.SMTP_CONNECT, errors.SMTP_TLS, errors.SMTP_AUTH,
    errors.SMTP_REJECTED, errors.SMTP_PREPARE))

_KNOWN_CODES = frozenset(
    value for name, value in vars(errors).items()
    if isinstance(value, str) and name.isupper())

_TASK_ID_RE = remote._TASK_ID_RE
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9.\-]+\Z")
_CONFIG_FIELDS = frozenset((
    "schema_version", "host", "port", "tls", "username",
    "password_file", "from_address", "timeout_seconds"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    key TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    delivery_task_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    sender TEXT NOT NULL,
    state TEXT NOT NULL,
    smtp_code INTEGER,
    retryable INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _state_invalid(message: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.STATE_INVALID, message)


def _config_invalid(message: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.CONFIG_INVALID, "invalid smtp config: " + message)


def _has_ascii_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _reject_duplicates(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen.add(key)
    return dict(pairs)


def _validate_private_file(path: Path, code: str, what: str) -> None:
    """Require an existing owner-private regular file; reject symlink paths."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if os.path.lexists(current) and os.path.islink(current):
            raise errors.DeliveryError(code, f"{what} path contains a symlink component")
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        raise errors.DeliveryError(code, f"{what} is missing or unreadable") from None
    if not stat_module.S_ISREG(info.st_mode):
        raise errors.DeliveryError(code, f"{what} is not a regular file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise errors.DeliveryError(code, f"{what} is not owned by the current user")
    if info.st_mode & 0o077:
        raise errors.DeliveryError(code, f"{what} must be owner-private")


# ---- SMTP config ----------------------------------------------------------

def load_smtp_config(smtp_config) -> dict:
    """Validate the real config file and return the in-memory config."""
    path = Path(os.fspath(smtp_config)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    _validate_private_file(path, errors.CONFIG_INVALID, "smtp config file")
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise _config_invalid("config file is not readable") from None
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (ValueError, UnicodeError):
        raise _config_invalid("config is not valid JSON") from None
    if not isinstance(payload, dict) or set(payload) != _CONFIG_FIELDS:
        raise _config_invalid("config must contain exactly the smtp fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise _config_invalid("unsupported schema_version")
    host = payload["host"]
    if (not isinstance(host, str) or not host or _has_ascii_controls(host)
            or any(char.isspace() for char in host)):
        raise _config_invalid("host must be a hostname or ip without whitespace")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not contacts._valid_domain(host):
            raise _config_invalid("host must be a DNS hostname or IP address")
    port = payload["port"]
    if not _is_int(port) or not 1 <= port <= 65535:
        raise _config_invalid("port must be an integer between 1 and 65535")
    tls = payload["tls"]
    if tls not in ("implicit", "starttls"):
        raise _config_invalid("tls must be implicit or starttls")
    username = payload["username"]
    if not isinstance(username, str) or not username or _has_ascii_controls(username):
        raise _config_invalid("username must be nonempty control-free text")
    timeout = payload["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise _config_invalid("timeout_seconds must be a number")
    if not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise _config_invalid("timeout_seconds must be between 1 and 60")
    from_address = contacts.parse_addr_spec(payload["from_address"])
    if from_address is None:
        raise _config_invalid("from_address is not a valid mailbox")
    password_file = payload["password_file"]
    if not isinstance(password_file, str) or not password_file:
        raise _config_invalid("password_file must be a path string")
    password_path = Path(password_file).expanduser()
    if not password_path.is_absolute():
        password_path = path.parent / password_path
    _validate_private_file(password_path, errors.CONFIG_INVALID, "password file")
    try:
        password_raw = password_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        raise _config_invalid("password file is not readable UTF-8") from None
    if password_raw.endswith("\r\n"):
        password_raw = password_raw[:-2]
    elif password_raw.endswith("\n"):
        password_raw = password_raw[:-1]
    if not password_raw:
        raise _config_invalid("password file is empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "host": host,
        "port": port,
        "tls": tls,
        "username": username,
        "from_address": from_address,
        "timeout_seconds": timeout,
        "password": password_raw,
    }


# ---- remote handoff (read-only, FD-004B) ----------------------------------

def _row_get(row, name):
    """Read a possibly-absent column from a row of a legacy-schema ledger."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _load_remote_task(state_real: Path, delivery_key: str) -> dict:
    db_path = state_real / remote.DB_NAME
    if not os.path.lexists(db_path):
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no remote delivery task found for key: {delivery_key}")
    _validate_private_file(db_path, errors.STATE_INVALID, "remote ledger")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                               timeout=ledger.LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise ledger._sqlite_error(exc) from None
    try:
        try:
            row = conn.execute("SELECT * FROM tasks WHERE key = ?",
                               (delivery_key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise _state_invalid("remote ledger schema is missing") from None
            raise ledger._sqlite_error(exc) from None
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
    finally:
        conn.close()
    if row is None:
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no remote delivery task found for key: {delivery_key}")
    task_id = _row_get(row, "task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise _state_invalid("remote task id is not ledger-owned")
    if _row_get(row, "object_key") != f"file-delivery/{task_id}.zip":
        raise _state_invalid("remote object key is not ledger-owned")
    archive_sha256 = _row_get(row, "archive_sha256")
    if not isinstance(archive_sha256, str) or not _HEX64_RE.fullmatch(archive_sha256):
        raise _state_invalid("remote task has no recorded archive digest")
    archive_size = _row_get(row, "archive_size")
    if not _is_int(archive_size) or archive_size < 0:
        raise _state_invalid("remote task has no recorded archive size")
    expires_at = _row_get(row, "expires_at")
    if not _is_int(expires_at):
        raise _state_invalid("remote task has no recorded link deadline")
    password_file = _row_get(row, "password_file")
    expected_password = state_real / remote.BUNDLES_DIR / task_id / archive.PASSWORD_NAME
    if not isinstance(password_file, str) or Path(password_file) != Path(expected_password):
        raise _state_invalid("recorded password path is not the ledger-owned bundle")
    return {
        "key": delivery_key,
        "task_id": task_id,
        "object_key": _row_get(row, "object_key"),
        "state": _row_get(row, "state"),
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "expires_at": expires_at,
        "url_sha256": _row_get(row, "url_sha256"),
        "password_file": password_file,
    }


def _load_validated_handoff(state_real: Path, task: dict) -> dict:
    handoff_path = state_real / remote.HANDOFFS_DIR / f"{task['task_id']}.json"
    _check_state_parents(state_real, handoff_path)
    _validate_private_file(handoff_path, errors.STATE_INVALID, "handoff file")
    if task["url_sha256"] is None:
        raise _state_invalid("remote task has no bound URL digest")
    expected = {
        "task_id": task["task_id"],
        "object_key": task["object_key"],
        "archive_sha256": task["archive_sha256"],
        "archive_size": task["archive_size"],
        "expires_at": task["expires_at"],
        "password_file": task["password_file"],
    }
    return remote._load_handoff(handoff_path, expected, task["url_sha256"])


def _read_bundle_password(state_real: Path, task: dict) -> str:
    password_path = Path(task["password_file"])
    _check_state_parents(state_real, password_path)
    _validate_private_file(password_path, errors.STATE_INVALID, "bundle password file")
    try:
        text = password_path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError):
        raise _state_invalid("bundle password file is not readable UTF-8") from None
    if not text:
        raise _state_invalid("bundle password file is empty")
    return text


def _verify_bundle(state_real: Path, task: dict) -> None:
    """Read-only archive verification; a changed password or corrupted
    encrypted archive must never be mailed as usable."""
    bundle_dir = state_real / remote.BUNDLES_DIR / task["task_id"]
    try:
        for name in (archive.ARCHIVE_NAME, archive.MANIFEST_NAME, archive.PASSWORD_NAME):
            path = bundle_dir / name
            _check_state_parents(state_real, path)
            _validate_private_file(path, errors.STATE_INVALID, "bundle file")
        verified = archive.verify(bundle_dir)
        if (verified["archive_sha256"] != task["archive_sha256"]
                or (bundle_dir / archive.ARCHIVE_NAME).stat().st_size != task["archive_size"]):
            raise _state_invalid("bundle differs from the recorded remote archive")
    except errors.DeliveryError:
        raise _state_invalid(
            "bundle failed verification before notification") from None
    except (OSError, ValueError):
        raise _state_invalid("bundle is not readable before notification") from None


# ---- notification ledger ---------------------------------------------------

def _open_db(state_real: Path) -> sqlite3.Connection:
    db_path = state_real / DB_NAME
    conn = None
    try:
        if not os.path.lexists(db_path):
            try:
                fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        _validate_private_file(db_path, errors.STATE_INVALID, "notification ledger")
        conn = sqlite3.connect(str(db_path), timeout=ledger.LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        conn.commit()
        os.chmod(db_path, 0o600)
    except (errors.DeliveryError, sqlite3.Error, OSError) as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, errors.DeliveryError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise ledger._sqlite_error(exc) from None
        raise errors.DeliveryError(errors.IO_ERROR, "cannot access notification database") from None
    return conn


def _update_state(conn: sqlite3.Connection, key: str, state: str,
                  retryable: bool, last_error: str | None = None,
                  smtp_code: int | None = None) -> None:
    conn.execute(
        "UPDATE notifications SET state = ?, retryable = ?, smtp_code = ?, "
        "last_error = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE key = ?",
        (state, 1 if retryable else 0, smtp_code, last_error, key))
    conn.commit()


def _record_error(conn: sqlite3.Connection, key: str, code: str) -> None:
    """Persist only a controlled error code; never provider text."""
    if code not in _KNOWN_CODES:
        code = errors.SMTP_UNKNOWN
    with contextlib.suppress(sqlite3.Error):
        conn.execute(
            "UPDATE notifications SET last_error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE key = ?",
            (code, key))
        conn.commit()


def _fingerprint(delivery_key: str, task: dict, recipient: str, config: dict) -> str:
    """Bind source identity and routing; exclude secret bytes and paths."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "delivery_key": delivery_key,
        "task_id": task["task_id"],
        "archive_sha256": task["archive_sha256"],
        "handoff_digest": task["url_sha256"] or "",
        "recipient": recipient,
        "sender": config["from_address"],
        "host": config["host"].lower(),
        "port": config["port"],
        "tls": config["tls"],
        "username": config["username"],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _accepted_result(stored, key: str, reused: bool) -> dict:
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": STATE_ACCEPTED,
        "notification_id": stored["notification_id"],
        "key": key,
        "delivery_task_id": stored["delivery_task_id"],
        "message_id": stored["message_id"],
        "reused": reused,
        "receipt_status": "unverified",
        "read_status": "unverified",
    }
    if stored["smtp_code"] is not None:
        result["smtp_code"] = stored["smtp_code"]
    return result


def _build_message(message_id: str, notification_id: str, from_addr: str,
                   to_addr: str, handoff: dict, password: str, task: dict) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    message["Subject"] = f"[file-delivery] notification {notification_id}"
    message["Message-ID"] = message_id
    plain, html = email_template.render(
        handoff['url'], password, task['archive_size'], task['expires_at'])
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    return message


def _classify_prepare_error(exc: BaseException) -> str:
    """Map a prepare exception to the safe vocabulary, classified before
    the durable sending boundary."""
    if isinstance(exc, errors.DeliveryError):
        if exc.code in SMTP_PREPARE_VOCAB or exc.code == errors.IO_ERROR:
            return exc.code
        return errors.SMTP_PREPARE
    if isinstance(exc, ssl.SSLError):
        return errors.SMTP_TLS
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return errors.SMTP_AUTH
    if isinstance(exc, smtplib.SMTPConnectError):
        return errors.SMTP_CONNECT
    if isinstance(exc, smtplib.SMTPResponseException):
        return errors.SMTP_REJECTED
    if isinstance(exc, smtplib.SMTPException):
        return errors.SMTP_CONNECT
    if isinstance(exc, OSError):
        return errors.IO_ERROR
    return errors.SMTP_PREPARE


def _close_quietly(transport) -> None:
    try:
        transport.close()
    except Exception:
        pass


def _state_dir_real(state_dir) -> Path:
    state_path = ledger._strict_abs_path(state_dir)
    if os.path.lexists(state_path):
        try:
            ledger._check_existing_private_dir(state_path)
        except OSError as exc:
            raise errors.DeliveryError(
                errors.IO_ERROR,
                f"cannot access state directory: {exc.strerror or exc}") from None
    return Path(os.path.realpath(state_path))


def _check_state_parents(state_real: Path, path: Path) -> None:
    try:
        relative = path.relative_to(state_real)
    except ValueError:
        raise _state_invalid("private artifact is outside state directory") from None
    current = state_real
    for part in relative.parts[:-1]:
        current = current / part
        ledger._strict_abs_path(current)
        ledger._check_existing_private_dir(current)


def _validate_notification_row(row) -> None:
    try:
        state = row["state"]
        if state not in (STATE_PREPARED, STATE_SENDING, STATE_ACCEPTED, STATE_FAILED, STATE_UNKNOWN):
            raise ValueError
        identity = row["notification_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"fn-[0-9a-f]{32}", identity):
            raise ValueError
        if row["message_id"] != f"<{identity}@file-delivery.invalid>":
            raise ValueError
        if not isinstance(row["delivery_task_id"], str) or not _TASK_ID_RE.fullmatch(row["delivery_task_id"]):
            raise ValueError
        if not isinstance(row["fingerprint"], str) or not _HEX64_RE.fullmatch(row["fingerprint"]):
            raise ValueError
        if row["last_error"] is not None and row["last_error"] not in _KNOWN_CODES:
            raise ValueError
        if row["retryable"] != int(state in (STATE_PREPARED, STATE_FAILED)):
            raise ValueError
        if row["smtp_code"] is not None and not _positive(row["smtp_code"]):
            raise ValueError
        if state == STATE_ACCEPTED and row["smtp_code"] is None:
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise _state_invalid("notification record is invalid") from None


def _safe_errors(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
        except OSError:
            raise errors.DeliveryError(errors.IO_ERROR, "notification storage access failed") from None
    return wrapped


# ---- public API ------------------------------------------------------------

@_safe_errors
def send(state_dir, delivery_key, smtp_config, recipient, notification_key, *,
         contacts_path=None, transport=None, checkpoint=None) -> dict:
    """Idempotently notify one mailbox about one verified remote handoff."""
    notification_key = ledger._validate_key(notification_key)
    delivery_key = ledger._validate_key(delivery_key)
    recipient_addr = contacts.resolve(recipient, contacts_path)
    config = load_smtp_config(smtp_config)

    state_real = _state_dir_real(state_dir)
    task = _load_remote_task(state_real, delivery_key)
    fingerprint = _fingerprint(delivery_key, task, recipient_addr, config)

    locks_dir = state_real / remote.LOCKS_DIR
    remote._ensure_private_subdir(locks_dir)
    ledger._check_existing_private_dir(locks_dir)
    lock_path = locks_dir / f"notification-{notification_key}.lock"
    if os.path.lexists(lock_path):
        _validate_private_file(lock_path, errors.STATE_INVALID, "notification lock")
    with ledger._key_lock(locks_dir, f"notification-{notification_key}"):
        conn = _open_db(state_real)
        try:
            stored = conn.execute(
                "SELECT * FROM notifications WHERE key = ?",
                (notification_key,)).fetchone()
            if stored is not None:
                _validate_notification_row(stored)
                if stored["fingerprint"] != fingerprint:
                    _record_error(conn, notification_key, errors.IDEMPOTENCY_CONFLICT)
                    raise errors.DeliveryError(
                        errors.IDEMPOTENCY_CONFLICT,
                        "notification key already used with a different "
                        "source or mailbox; original record is preserved")
                if stored["state"] == STATE_ACCEPTED:
                    return _accepted_result(stored, notification_key, True)
                if stored["state"] in (STATE_UNKNOWN, STATE_SENDING):
                    # A prior submission may have reached the wire: never
                    # send again on this key.
                    _update_state(conn, notification_key, STATE_UNKNOWN,
                                  retryable=False, last_error=errors.SMTP_UNKNOWN)
                    raise errors.DeliveryError(
                        errors.SMTP_UNKNOWN,
                        "previous submission outcome is unknown; "
                        "this notification will not be sent again")
                notification_id = stored["notification_id"]
                message_id = stored["message_id"]
            else:
                notification_id = "fn-" + secrets.token_hex(16)
                message_id = f"<{notification_id}@file-delivery.invalid>"

            if task["state"] != remote.STATE_LINK_VERIFIED:
                _record_error(conn, notification_key, errors.DELIVERY_NOT_READY)
                raise errors.DeliveryError(
                    errors.DELIVERY_NOT_READY,
                    "remote delivery task is not link-verified")
            if int(time.time()) >= task["expires_at"]:
                _record_error(conn, notification_key, errors.LINK_EXPIRED)
                raise errors.DeliveryError(
                    errors.LINK_EXPIRED,
                    "handoff link deadline has passed; original records are preserved")
            handoff = _load_validated_handoff(state_real, task)
            password = _read_bundle_password(state_real, task)
            _verify_bundle(state_real, task)

            if stored is None:
                conn.execute(
                    "INSERT INTO notifications (key, notification_id, message_id, "
                    "fingerprint, delivery_task_id, recipient, sender, state, retryable) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (notification_key, notification_id, message_id, fingerprint,
                     task["task_id"], recipient_addr, config["from_address"],
                     STATE_PREPARED))
                conn.commit()
            else:
                _update_state(conn, notification_key, STATE_PREPARED, retryable=True)

            message = _build_message(message_id, notification_id,
                                     config["from_address"], recipient_addr,
                                     handoff, password, task)
            sender = transport if transport is not None else SmtpTransport()
            try:
                sender.prepare(config, config["from_address"], recipient_addr)
            except Exception as exc:
                code = _classify_prepare_error(exc)
                _close_quietly(sender)
                _update_state(conn, notification_key, STATE_FAILED,
                              retryable=True, last_error=code)
                raise errors.DeliveryError(code, "smtp preparation failed") from None

            # Durable sending boundary: committed before submit; any later
            # failure or interruption is conservatively unknown.
            try:
                _update_state(conn, notification_key, STATE_SENDING, retryable=False)
            except Exception:
                _close_quietly(sender)
                raise
            try:
                if checkpoint is not None:
                    checkpoint("before_submit")
                result = sender.submit(message, config["from_address"], recipient_addr)
                if (not isinstance(result, dict)
                        or result.get("status") != "channel-accepted"
                        or not _is_int(result.get("smtp_code"))
                        or not 200 <= result["smtp_code"] < 300):
                    raise ValueError("malformed submit result")
                if checkpoint is not None:
                    checkpoint("after_submit")
                _update_state(conn, notification_key, STATE_ACCEPTED,
                              retryable=False, smtp_code=result["smtp_code"])
            except Exception:
                with contextlib.suppress(sqlite3.Error, OSError):
                    _update_state(conn, notification_key, STATE_UNKNOWN,
                                  retryable=False, last_error=errors.SMTP_UNKNOWN)
                _close_quietly(sender)
                raise errors.DeliveryError(
                    errors.SMTP_UNKNOWN, "smtp submission outcome is unknown") from None
            except BaseException:
                with contextlib.suppress(sqlite3.Error, OSError):
                    _update_state(conn, notification_key, STATE_UNKNOWN,
                                  retryable=False, last_error=errors.SMTP_UNKNOWN)
                _close_quietly(sender)
                raise

            _close_quietly(sender)
            stored = conn.execute(
                "SELECT * FROM notifications WHERE key = ?",
                (notification_key,)).fetchone()
            return _accepted_result(stored, notification_key, False)
        finally:
            conn.close()


@_safe_errors
def status(state_dir, notification_key) -> dict:
    """Return the persisted notification state without config, source or network."""
    key = ledger._validate_key(notification_key)
    state_real = _state_dir_real(state_dir)
    db_path = state_real / DB_NAME
    if not os.path.lexists(db_path):
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no notification found for key: {key}")
    _validate_private_file(db_path, errors.STATE_INVALID, "notification ledger")
    try:
        if not stat_module.S_ISREG(os.lstat(db_path).st_mode):
            raise _state_invalid("notification database is not a regular file")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                               timeout=ledger.LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
    except errors.DeliveryError:
        raise
    except sqlite3.Error as exc:
        raise ledger._sqlite_error(exc) from None
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot access notification database: {exc.strerror or exc}") from None
    try:
        try:
            row = conn.execute("SELECT * FROM notifications WHERE key = ?",
                               (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise _state_invalid("notification ledger schema is missing") from None
            raise ledger._sqlite_error(exc) from None
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
    finally:
        conn.close()
    if row is None:
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no notification found for key: {key}")
    _validate_notification_row(row)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": row["state"],
        "notification_id": row["notification_id"],
        "key": key,
        "delivery_task_id": row["delivery_task_id"],
        "message_id": row["message_id"],
        "retryable": bool(row["retryable"]),
        "receipt_status": "unverified",
        "read_status": "unverified",
    }
    if row["smtp_code"] is not None:
        result["smtp_code"] = row["smtp_code"]
    if row["last_error"]:
        result["last_error"] = row["last_error"]
    return result


# ---- production transport --------------------------------------------------

def _positive(code) -> bool:
    return _is_int(code) and 200 <= code < 300


class SmtpTransport:
    """TLS SMTP seam: prepare connects through RCPT TO, submit sends DATA only.

    Certificates and hostnames are always verified with
    ``ssl.create_default_context``; there is no plaintext fallback.
    """

    def __init__(self) -> None:
        self._client = None

    def prepare(self, config, from_addr, to_addr) -> None:
        context = ssl.create_default_context()
        timeout = config.get("timeout_seconds", 3)
        try:
            if config.get("tls") == "starttls":
                client = smtplib.SMTP(config["host"], config["port"], timeout=timeout)
            else:
                client = smtplib.SMTP_SSL(config["host"], config["port"],
                                          timeout=timeout, context=context)
            self._client = client
        except ssl.SSLError:
            raise errors.DeliveryError(errors.SMTP_TLS, "smtp TLS connection failed") from None
        except (OSError, smtplib.SMTPException):
            raise errors.DeliveryError(errors.SMTP_CONNECT, "smtp connection failed") from None
        if not _positive(client.ehlo()[0]):
            raise errors.DeliveryError(errors.SMTP_REJECTED, "smtp ehlo failed")
        if config.get("tls") == "starttls":
            try:
                if not _positive(client.starttls(context=context)[0]):
                    raise errors.DeliveryError(errors.SMTP_TLS, "smtp starttls refused")
            except (ssl.SSLError, smtplib.SMTPException, OSError):
                raise errors.DeliveryError(errors.SMTP_TLS, "smtp starttls failed") from None
            if not _positive(client.ehlo()[0]):
                raise errors.DeliveryError(errors.SMTP_REJECTED, "smtp ehlo failed")
        if not _positive(client.login(config["username"], config["password"])[0]):
            raise errors.DeliveryError(errors.SMTP_AUTH, "smtp login refused")
        if not _positive(client.mail(from_addr)[0]):
            raise errors.DeliveryError(errors.SMTP_REJECTED, "smtp MAIL FROM refused")
        if not _positive(client.rcpt(to_addr)[0]):
            raise errors.DeliveryError(errors.SMTP_REJECTED, "smtp RCPT TO refused")

    def submit(self, message, from_addr, to_addr) -> dict:
        if self._client is None:
            raise errors.DeliveryError(errors.SMTP_PREPARE, "transport is not prepared")
        payload = message.as_bytes(policy=policy.SMTP)
        code = self._client.data(payload)[0]
        if not _positive(code):
            raise errors.DeliveryError(errors.SMTP_REJECTED, "smtp DATA refused")
        return {"status": "channel-accepted", "smtp_code": code}

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    client.close()
