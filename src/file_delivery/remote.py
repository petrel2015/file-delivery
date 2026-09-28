"""Durable idempotent remote delivery to a private Qiniu bucket (FD-004B).

The state directory holds its own ``remote.sqlite3`` ledger plus private
bundle and handoff directories. Signed links and passwords never enter the
database, results or logs; the plaintext password only ever lives inside the
bundle's password.txt and the signed URL only inside the 0600 handoff file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat as stat_module
import tempfile
import time
from pathlib import Path

from file_delivery import archive, errors, ledger, planning
from file_delivery.qiniu_store import QiniuStore

SCHEMA_VERSION = 1

DB_NAME = "remote.sqlite3"
BUNDLES_DIR = "remote-bundles"
HANDOFFS_DIR = "remote-handoffs"
LOCKS_DIR = "locks"

MAX_TTL_SECONDS = 604800
MAX_RETENTION_DAYS = 3650

STATE_PENDING = "pending"
STATE_PACKAGED = "packaged"
STATE_UPLOADING = "uploading"
STATE_UPLOADED = "uploaded"
STATE_LINK_VERIFIED = "link-verified"
STATE_REVOKING = "revoking"
STATE_OBJECT_DELETED = "object-deleted"

_OBJECT_PREFIX = "file-delivery"

# Ledger-owned task ids are exactly "fd-" plus 32 lowercase hex chars; any
# other value means the row was corrupted or hand-edited.
_TASK_ID_RE = re.compile(r"fd-[0-9a-f]{32}\Z")

# The complete controlled error vocabulary; any code outside it (e.g. from an
# injected provider or checkpoint) is never echoed, returned or persisted.
_KNOWN_CODES = frozenset(
    value for name, value in vars(errors).items()
    if isinstance(value, str) and name.isupper())

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    archive_sha256 TEXT,
    archive_size INTEGER,
    bundle_path TEXT,
    password_file TEXT,
    handoff_path TEXT,
    file_count INTEGER,
    total_bytes INTEGER,
    expires_at INTEGER,
    url_sha256 TEXT,
    provider_identity TEXT,
    retention_days INTEGER,
    first_upload_at INTEGER,
    retention_expires_at INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _invalid(detail: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.CONFIG_INVALID, "invalid remote delivery policy: " + detail)


def _validate_policy(ttl_seconds, retention_days) -> tuple[int, int]:
    if not _is_int(ttl_seconds) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise _invalid("ttl_seconds must be an integer between 1 and 604800")
    if not _is_int(retention_days) or not 1 <= retention_days <= MAX_RETENTION_DAYS:
        raise _invalid("retention_days must be an integer between 1 and 3650")
    if ttl_seconds > retention_days * 86400:
        raise _invalid("ttl_seconds must not exceed retention_days expressed in seconds")
    return ttl_seconds, retention_days


def _load_identity(config_path, root_real: Path) -> tuple[QiniuStore, dict]:
    """Validate the real config file and derive its normalized identity.

    The identity always comes from the config file itself, never from an
    injected store, so an untrusted injected destination cannot redirect a
    request bound to a different logical destination.
    """
    config = Path(os.fspath(config_path)).expanduser()
    if not config.is_absolute():
        config = Path.cwd() / config
    config_real = Path(os.path.realpath(config))
    if config_real == root_real or config_real.is_relative_to(root_real):
        raise errors.DeliveryError(
            errors.CONFIG_INVALID, "config file must be outside the input root")
    store = QiniuStore.from_file(config)
    return store, store.identity()


def _provider(op: str, func, *args, **kwargs):
    """Call a provider operation, re-raising with a sanitized message."""
    try:
        return func(*args, **kwargs)
    except errors.DeliveryError as exc:
        code = exc.code if exc.code in _KNOWN_CODES else errors.REMOTE_UNKNOWN
        raise errors.DeliveryError(code, f"provider {op} failed") from None
    except OSError:
        raise errors.DeliveryError(errors.IO_ERROR, f"provider {op} io failure") from None
    except Exception:
        raise errors.DeliveryError(
            errors.REMOTE_UNKNOWN, f"provider {op} failed (outcome unknown)") from None


def _local_etag(path: Path) -> str:
    try:
        from qiniu import etag
    except ImportError:
        raise errors.DeliveryError(
            errors.DEPENDENCY_MISSING,
            "optional dependency 'qiniu' is required: install with pip install 'file-delivery[qiniu]'",
        ) from None
    try:
        return etag(str(path))
    except OSError:
        raise errors.DeliveryError(errors.IO_ERROR, "cannot read bundle archive") from None


def _fingerprint(root_real: Path, manifest: dict, identity: dict,
                 ttl_seconds: int, retention_days: int) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root_real),
        "files": [[f["path"], f["size_bytes"], f["sha256"]] for f in manifest["files"]],
        "provider": identity,
        "ttl_seconds": ttl_seconds,
        "retention_days": retention_days,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _state_invalid(detail: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.STATE_INVALID, detail)


def _reject_symlink_file(path: Path, what: str) -> None:
    if os.path.lexists(path):
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise errors.DeliveryError(
                errors.IO_ERROR, f"cannot inspect {what}: {exc.strerror or exc}") from None
        if stat_module.S_ISLNK(info.st_mode):
            raise _state_invalid(f"{what} must not be a symlink: {path.name}")


def _reject_path_symlinks(path: Path, what: str) -> None:
    """Lexically reject any symlink component of a private path (read-only).

    Unlike :func:`_reject_symlink_file`, every existing component from the
    anchor down is checked, so a subdirectory swapped for a symlink pointing
    outside the state tree is never followed. Missing components are allowed;
    the caller decides whether absence matters.
    """
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if os.path.lexists(current) and os.path.islink(current):
                raise _state_invalid(
                    f"{what} path contains a symlink component: {current}")
    except errors.DeliveryError:
        raise
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot inspect {what} path: {exc.strerror or exc}") from None


def _reject_bundle_symlinks(bundle_dir: Path) -> None:
    _reject_symlink_file(bundle_dir, "bundle directory")
    if os.path.lexists(bundle_dir) and not os.path.islink(bundle_dir):
        for name in (archive.ARCHIVE_NAME, archive.MANIFEST_NAME, archive.PASSWORD_NAME):
            _reject_symlink_file(bundle_dir / name, "bundle file")


def _ensure_private_subdir(path: Path) -> None:
    """Create a private subdirectory; reject a preexisting symlink target."""
    _reject_symlink_file(path, "private state subdirectory")
    try:
        os.mkdir(path, mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot create private directory: {exc.strerror or exc}") from None
    info = os.stat(path)
    if not stat_module.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise _state_invalid(f"invalid private directory: {path}")
    os.chmod(path, 0o700)


def _open_db(state_dir: Path) -> sqlite3.Connection:
    db_path = state_dir / DB_NAME
    _reject_symlink_file(db_path, "remote ledger database")
    try:
        if os.path.lexists(db_path) and not stat_module.S_ISREG(os.lstat(db_path).st_mode):
            raise _state_invalid("remote ledger database is not a regular file")
        conn = sqlite3.connect(str(db_path), timeout=ledger.LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        with contextlib.suppress(sqlite3.Error):
            # Ledger from a build without url binding; NULL keeps loading lenient.
            conn.execute("ALTER TABLE tasks ADD COLUMN url_sha256 TEXT")
        # FD-004C destination/retention metadata; legacy rows stay NULL and
        # are never deleted, and nothing is inferred from the current config.
        for column, col_type in (("provider_identity", "TEXT"),
                                 ("retention_days", "INTEGER"),
                                 ("first_upload_at", "INTEGER"),
                                 ("retention_expires_at", "INTEGER")):
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {col_type}")
        conn.commit()
        os.chmod(db_path, 0o600)
    except errors.DeliveryError:
        raise
    except sqlite3.Error as exc:
        raise ledger._sqlite_error(exc) from None
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot access remote ledger database: {exc.strerror or exc}") from None
    return conn


def _set_state(conn: sqlite3.Connection, key: str, state: str, **fields) -> None:
    assignments = ["state = ?", "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
    values: list = [state]
    for name, value in fields.items():
        assignments.append(f"{name} = ?")
        values.append(value)
    assignments.append("last_error = NULL")
    values.append(key)
    conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE key = ?", values)
    conn.commit()


def _run_checkpoint(checkpoint, name: str) -> None:
    """Run a checkpoint callback; every failure becomes a safe IO_ERROR.

    Callback details (type, code, message) are never echoed or persisted.
    """
    if checkpoint is None:
        return
    try:
        checkpoint(name)
    except Exception:
        raise errors.DeliveryError(
            errors.IO_ERROR, f"checkpoint {name} failed") from None


def _record_error(conn: sqlite3.Connection, key: str, code: str) -> None:
    """Persist only a controlled error code; never provider text."""
    if code not in _KNOWN_CODES:
        code = errors.REMOTE_UNKNOWN
    with contextlib.suppress(sqlite3.Error):
        conn.execute(
            "UPDATE tasks SET last_error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE key = ?",
            (code, key))
        conn.commit()


def _handoff_expectation(row, task_id: str, object_key: str) -> dict:
    expected = {"task_id": task_id, "object_key": object_key}
    for field in ("archive_sha256", "archive_size", "password_file", "expires_at"):
        value = row[field] if row is not None else None
        if value is None:
            raise _state_invalid(
                "handoff file exists without a complete task record")
        expected[field] = value
    return expected


def _load_handoff(path: Path, expected: dict, url_sha256=None) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _state_invalid("handoff file is not readable JSON") from None
    if not isinstance(payload, dict):
        raise _state_invalid("handoff file is not a JSON object")
    for field, value in expected.items():
        if payload.get(field) != value:
            raise _state_invalid("handoff file does not match the task record")
    expires_at = payload.get("expires_at")
    if not _is_int(expires_at):
        raise _state_invalid("handoff file has an invalid expires_at")
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise _state_invalid("handoff file has an invalid url")
    if url_sha256 is not None:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if digest != url_sha256:
            raise _state_invalid("handoff file does not match the task record")
    return payload


def _write_handoff(path: Path, payload: dict) -> None:
    """Atomically publish the 0600 handoff file; never replace an existing one."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        if os.path.lexists(path):
            raise _state_invalid("handoff file already exists")
        os.replace(tmp_path, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def deliver(paths, root, state_dir, config_path, key, *,
            ttl_seconds=604800, retention_days=30, store=None, checkpoint=None) -> dict:
    """Idempotently deliver an encrypted bundle to a private Qiniu bucket."""
    key = ledger._validate_key(key)
    ttl_seconds, retention_days = _validate_policy(ttl_seconds, retention_days)
    root_real = Path(os.path.realpath(os.fspath(Path(root).expanduser())))
    config_store, identity = _load_identity(config_path, root_real)
    provider = store if store is not None else config_store

    state_real = ledger._validate_private_dir(state_dir, root_real)
    ledger._create_private_dir(state_real)
    bundles_dir = state_real / BUNDLES_DIR
    handoffs_dir = state_real / HANDOFFS_DIR
    locks_dir = state_real / LOCKS_DIR
    _ensure_private_subdir(bundles_dir)
    _ensure_private_subdir(handoffs_dir)
    _ensure_private_subdir(locks_dir)

    manifest = planning.plan(paths, root)
    fingerprint = _fingerprint(root_real, manifest, identity, ttl_seconds, retention_days)

    lock_path = locks_dir / f"{key}.lock"
    _reject_symlink_file(lock_path, "lock file")

    with ledger._key_lock(locks_dir, key):
        conn = _open_db(state_real)
        try:
            row = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
            reused = row is not None
            if row is None:
                task_id = "fd-" + secrets.token_hex(16)
                object_key = f"{_OBJECT_PREFIX}/{task_id}.zip"
                conn.execute(
                    "INSERT INTO tasks (key, task_id, object_key, fingerprint, state, "
                    "provider_identity, retention_days) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, task_id, object_key, fingerprint, STATE_PENDING,
                     json.dumps(identity, sort_keys=True, ensure_ascii=True),
                     retention_days))
                conn.commit()
                recorded_sha = None
                expires_at = None
            if row is None:
                entry_state = STATE_PENDING
            else:
                if row["state"] in (STATE_REVOKING, STATE_OBJECT_DELETED):
                    _record_error(conn, key, errors.TASK_REVOKED)
                    raise errors.DeliveryError(
                        errors.TASK_REVOKED,
                        "task was revoked; use a new key for a new delivery")
                if row["fingerprint"] != fingerprint:
                    _record_error(conn, key, errors.IDEMPOTENCY_CONFLICT)
                    raise errors.DeliveryError(
                        errors.IDEMPOTENCY_CONFLICT,
                        "key already used with different content, destination or policy; "
                        "original task is preserved")
                task_id = row["task_id"]
                entry_state = row["state"]
                object_key = row["object_key"]
                recorded_sha = row["archive_sha256"]
                expires_at = row["expires_at"]

            handoff_path = handoffs_dir / f"{task_id}.json"
            _reject_symlink_file(handoff_path, "handoff file")
            handoff = None
            if os.path.lexists(handoff_path):
                recorded_url_sha = row["url_sha256"] if row is not None else None
                if recorded_url_sha is None:
                    # The URL digest is persisted before the handoff file is
                    # written, so a file without one cannot be trusted.
                    raise _state_invalid(
                        "handoff file exists without a bound URL digest")
                handoff = _load_handoff(
                    handoff_path, _handoff_expectation(row, task_id, object_key),
                    recorded_url_sha)

            # The original deadline is never extended: once selected, a retry
            # past it fails safely without signing or uploading again.
            if expires_at is not None and int(time.time()) >= expires_at:
                _record_error(conn, key, errors.LINK_EXPIRED)
                raise errors.DeliveryError(
                    errors.LINK_EXPIRED,
                    "signed link deadline has passed; original records are preserved")

            try:
                bundle_dir = bundles_dir / task_id
                _reject_bundle_symlinks(bundle_dir)
                bundle = ledger._ensure_bundle(paths, root, bundle_dir, recorded_sha)
                archive_path = bundle_dir / archive.ARCHIVE_NAME
                archive_size = os.stat(archive_path).st_size
                _run_checkpoint(checkpoint, "after_pack")
                # State never regresses: a row that already progressed past
                # packing keeps its later state, so re-entry can never
                # rewrite a historically qualified row (one whose upload
                # timestamps were corrupted to NULL after the fact) into
                # looking like a fresh pre-upload task.
                if entry_state in (STATE_PENDING, STATE_PACKAGED):
                    _set_state(conn, key, STATE_PACKAGED,
                               archive_sha256=bundle["archive_sha256"],
                               archive_size=archive_size,
                               bundle_path=str(bundle_dir),
                               password_file=bundle["password_file"],
                               file_count=bundle["file_count"],
                               total_bytes=bundle["total_bytes"])

                # Retention starts with the first upload intent, never while
                # still packing. Rows created before this metadata existed
                # keep NULL columns forever: backfilling from the current
                # config/time would grant delete eligibility the ledger never
                # recorded, so only rows that already carry identity and
                # retention get their first-upload timestamps selected.
                # The row must also still be positively identified as
                # pre-upload-intent (pending or packaged as fetched at entry):
                # a row that ever reached the upload-intent transition has
                # state at or beyond uploading, and because the timestamps
                # are selected atomically with that transition, NULL dates
                # on such a row mean the historical record was corrupted and
                # must never be backfilled. A state rewrite can no longer
                # launder that qualification because state never regresses.
                legacy_row = row is not None and not (
                    _row_field(row, "provider_identity") is not None
                    and _row_field(row, "retention_days") is not None
                    and _row_field(row, "first_upload_at") is None
                    and _row_field(row, "retention_expires_at") is None
                    and entry_state in (STATE_PENDING, STATE_PACKAGED))
                if not legacy_row:
                    upload_now = int(time.time())
                    conn.execute(
                        "UPDATE tasks SET state = ?, "
                        "provider_identity = COALESCE(provider_identity, ?), "
                        "retention_days = COALESCE(retention_days, ?), "
                        "first_upload_at = COALESCE(first_upload_at, ?), "
                        "retention_expires_at = COALESCE(retention_expires_at, ?), "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                        "WHERE key = ?",
                        (STATE_UPLOADING,
                         json.dumps(identity, sort_keys=True, ensure_ascii=True),
                         retention_days, upload_now,
                         upload_now + retention_days * 86400, key))
                    conn.commit()
                elif entry_state in (STATE_PENDING, STATE_PACKAGED):
                    # Legacy row without complete destination metadata:
                    # proceed with the upload-intent transition but never
                    # select retention timestamps for it.
                    _set_state(conn, key, STATE_UPLOADING)
                _provider("bucket privacy check", provider.ensure_private)
                local_etag = _local_etag(archive_path)
                existing = _provider("stat", provider.stat, object_key)
                if existing is None:
                    uploaded = _provider("upload", provider.upload,
                                         str(archive_path), object_key, retention_days)
                    if (not isinstance(uploaded, dict) or uploaded.get("etag") != local_etag
                            or uploaded.get("size") != archive_size):
                        raise errors.DeliveryError(
                            errors.REMOTE_INTEGRITY, "uploaded object did not match the bundle")
                    _run_checkpoint(checkpoint, "after_upload")
                    _set_state(conn, key, STATE_UPLOADED)
                else:
                    if (not isinstance(existing, dict) or existing.get("etag") != local_etag
                            or existing.get("size") != archive_size):
                        raise errors.DeliveryError(
                            errors.REMOTE_CONFLICT,
                            "remote object exists with different content; "
                            "bundle and records are preserved")
                    _set_state(conn, key, STATE_UPLOADED)

                download = _provider("download verification", provider.verify_download,
                                     object_key, bundle["archive_sha256"], archive_size)
                if (not isinstance(download, dict)
                        or download.get("sha256") != bundle["archive_sha256"]
                        or download.get("size") != archive_size):
                    raise errors.DeliveryError(
                        errors.REMOTE_INTEGRITY, "remote download did not match the bundle")

                if handoff is None:
                    if expires_at is None:
                        # Persist the deadline before signing so retries reuse
                        # the remaining TTL instead of extending it.
                        expires_at = int(time.time()) + ttl_seconds
                        conn.execute("UPDATE tasks SET expires_at = ? WHERE key = ?",
                                     (expires_at, key))
                        conn.commit()
                    remaining = expires_at - int(time.time())
                    if remaining < 1:
                        raise errors.DeliveryError(
                            errors.LINK_EXPIRED,
                            "signed link deadline has passed; original records are preserved")
                    signed = _provider("signing", provider.signed_url, object_key, remaining)
                    if not isinstance(signed, dict) or not isinstance(signed.get("url"), str):
                        raise errors.DeliveryError(
                            errors.REMOTE_ERROR, "signing response was malformed")
                    url_digest = hashlib.sha256(
                        signed["url"].encode("utf-8")).hexdigest()
                    # Bind the URL digest durably before the handoff file is
                    # written, so recovery validates content instead of
                    # trusting whatever the file first shows.
                    conn.execute("UPDATE tasks SET url_sha256 = ? WHERE key = ?",
                                 (url_digest, key))
                    conn.commit()
                    _write_handoff(handoff_path, {
                        "url": signed["url"],
                        "expires_at": expires_at,
                        "password_file": bundle["password_file"],
                        "task_id": task_id,
                        "object_key": object_key,
                        "archive_sha256": bundle["archive_sha256"],
                        "archive_size": archive_size,
                    })
                else:
                    url_digest = row["url_sha256"]
                _run_checkpoint(checkpoint, "after_link")
                _set_state(conn, key, STATE_LINK_VERIFIED,
                           handoff_path=str(handoff_path), url_sha256=url_digest)
            except errors.DeliveryError as exc:
                _record_error(conn, key, exc.code)
                raise
            except OSError:
                raise errors.DeliveryError(errors.IO_ERROR, "bundle access failed") from None
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
        finally:
            conn.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATE_LINK_VERIFIED,
        "task_id": task_id,
        "key": key,
        "object_key": object_key,
        "archive_sha256": bundle["archive_sha256"],
        "archive_size": archive_size,
        "file_count": bundle["file_count"],
        "total_bytes": bundle["total_bytes"],
        "password_file": bundle["password_file"],
        "handoff_file": str(handoff_path),
        "expires_at": expires_at,
        "reused": reused,
    }


def status(state_dir, key) -> dict:
    """Return the persisted remote task for key without config, input or network."""
    key = ledger._validate_key(key)
    state_real = ledger._strict_abs_path(state_dir)
    if os.path.lexists(state_real):
        try:
            ledger._check_existing_private_dir(state_real)
        except errors.DeliveryError:
            raise
        except OSError as exc:
            raise errors.DeliveryError(
                errors.IO_ERROR,
                f"cannot access state directory: {exc.strerror or exc}") from None
    db_path = state_real / DB_NAME
    if not os.path.lexists(db_path):
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no remote task found for key: {key}")
    _reject_symlink_file(db_path, "remote ledger database")
    try:
        if not stat_module.S_ISREG(os.lstat(db_path).st_mode):
            raise _state_invalid("remote ledger database is not a regular file")
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
            f"cannot access remote ledger database: {exc.strerror or exc}") from None
    try:
        try:
            row = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise errors.DeliveryError(
                    errors.TASK_NOT_FOUND, f"no remote task found for key: {key}") from None
            raise ledger._sqlite_error(exc) from None
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
    finally:
        conn.close()
    if row is None:
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no remote task found for key: {key}")
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": row["state"],
        "task_id": row["task_id"],
        "key": key,
    }
    for field in ("object_key", "archive_sha256", "archive_size", "file_count",
                  "total_bytes", "expires_at", "retention_expires_at"):
        value = _row_field(row, field)
        if value is not None:
            result[field] = value
    handoff_dir = state_real / HANDOFFS_DIR
    handoff_path = handoff_dir / f"{row['task_id']}.json"
    if os.path.lexists(handoff_path):
        _reject_path_symlinks(handoff_path, "handoff file")
        result["handoff_file"] = str(handoff_path)
    if row["password_file"]:
        password_path = Path(row["password_file"]).expanduser()
        if not password_path.is_absolute():
            password_path = Path.cwd() / password_path
        if os.path.lexists(password_path):
            _reject_path_symlinks(password_path, "password file")
            result["password_file"] = row["password_file"]
    if row["last_error"]:
        result["last_error"] = row["last_error"]
    return result


# ---- revocation and retention cleanup (FD-004C) ---------------------------

_RETENTION_METADATA = ("provider_identity", "retention_days",
                       "first_upload_at", "retention_expires_at")


def _row_field(row, name):
    """Read a possibly-absent column from a row of a legacy-schema ledger."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _config_path_abs(config_path) -> Path:
    config = Path(os.fspath(config_path)).expanduser()
    if not config.is_absolute():
        config = Path.cwd() / config
    return config


def _load_config_identity(config_path) -> tuple[QiniuStore, dict]:
    """Validate the caller's real config file; identity never comes from an
    injected store, so revocation is always bound to the configured
    destination actually recorded in the ledger."""
    store = QiniuStore.from_file(_config_path_abs(config_path))
    return store, store.identity()


def _existing_state_dir(state_dir) -> Path:
    state_real = ledger._strict_abs_path(state_dir)
    if os.path.lexists(state_real):
        try:
            ledger._check_existing_private_dir(state_real)
        except OSError as exc:
            raise errors.DeliveryError(
                errors.IO_ERROR,
                f"cannot access state directory: {exc.strerror or exc}") from None
    db_path = state_real / DB_NAME
    if not os.path.lexists(db_path):
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, "no remote ledger database found")
    _reject_symlink_file(db_path, "remote ledger database")
    try:
        if not stat_module.S_ISREG(os.lstat(db_path).st_mode):
            raise _state_invalid("remote ledger database is not a regular file")
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot access remote ledger database: {exc.strerror or exc}") from None
    return state_real


def _probe_old_link(provider, handoff_url) -> tuple[str, int | None]:
    """Report the availability of a previously issued URL, if probeable.

    The probe itself is credential-free; an unavailable or failed probe is
    reported as unknown and never exposes the signed URL.
    """
    probe = getattr(provider, "probe_link", None)
    if probe is None:
        return "link-unknown", None
    try:
        probed = probe(handoff_url)
    except Exception:
        # Any probe failure (including unknown injected-provider errors) is
        # reported as unknown; provider text never leaks into results.
        return "link-unknown", None
    if not isinstance(probed, dict) or probed.get("status") not in (
            "link-accessible", "link-unavailable", "link-unknown"):
        return "link-unknown", None
    http_status = probed.get("http_status")
    return probed["status"], http_status if _is_int(http_status) else None


def _revoke_row(conn: sqlite3.Connection, state_real: Path,
                provider, identity: dict, key: str) -> dict:
    """Delete the ledger-owned object for key; link status is separate."""
    row = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no remote task found for key: {key}")
    task_id = row["task_id"]
    object_key = row["object_key"]
    # Ownership is validated from the row itself before any private path is
    # derived or a provider is called: a corrupted task_id must never turn
    # into a delete of some other target.
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise _state_invalid("task id is not ledger-owned")
    if object_key != f"{_OBJECT_PREFIX}/{task_id}.zip":
        raise _state_invalid("task object key is not ledger-owned")
    for field in _RETENTION_METADATA:
        if _row_field(row, field) is None:
            raise _state_invalid(
                "task predates destination retention metadata and cannot "
                "be deleted safely")
    try:
        persisted_identity = json.loads(row["provider_identity"])
    except ValueError:
        raise _state_invalid("persisted provider identity is unreadable") from None
    if persisted_identity != identity:
        raise errors.DeliveryError(
            errors.IDEMPOTENCY_CONFLICT,
            "config does not match the persisted destination of this task")

    link_status, link_http_status = "link-unknown", None
    handoff_path = state_real / HANDOFFS_DIR / f"{task_id}.json"
    _reject_path_symlinks(handoff_path, "handoff file")
    _reject_symlink_file(handoff_path, "handoff file")
    if os.path.lexists(handoff_path):
        recorded_url_sha = row["url_sha256"]
        if recorded_url_sha is None:
            raise _state_invalid("handoff file exists without a bound URL digest")
        handoff = _load_handoff(
            handoff_path, _handoff_expectation(row, task_id, object_key),
            recorded_url_sha)
        link_status, link_http_status = _probe_old_link(provider, handoff["url"])
    elif row["handoff_path"] is not None:
        # A handoff that was published once and later disappeared means the
        # private state was tampered with; only a task that never reached
        # handoff publication may legitimately lack the file.
        raise _state_invalid("previously persisted handoff file is missing")

    result = {
        "schema_version": SCHEMA_VERSION,
        "status": STATE_OBJECT_DELETED,
        "key": key,
        "task_id": task_id,
        "object_key": object_key,
        "link_status": link_status,
        "link_http_status": link_http_status,
    }
    if row["state"] == STATE_OBJECT_DELETED:
        # object-deleted is terminal: an explicit revoke may re-probe the old
        # link above, but never re-enters revoking or deletes a reappeared
        # object.
        return result
    try:
        _set_state(conn, key, STATE_REVOKING)
        if _provider("stat", provider.stat, object_key) is not None:
            _provider("delete", provider.delete, object_key)
        if _provider("stat", provider.stat, object_key) is not None:
            raise errors.DeliveryError(
                errors.REMOTE_DELETE_UNCONFIRMED,
                "object still present after deletion (outcome unknown)")
        _set_state(conn, key, STATE_OBJECT_DELETED)
    except errors.DeliveryError as exc:
        _record_error(conn, key, exc.code)
        raise
    return result


def revoke(state_dir, config_path, key, *, store=None) -> dict:
    """Revoke a ledger-owned Qiniu object using the caller's config.

    Works without the original inputs or locally stored secrets; local
    bundle, password and handoff are retained byte-for-byte.
    """
    key = ledger._validate_key(key)
    state_real = _existing_state_dir(state_dir)
    config_store, identity = _load_config_identity(config_path)
    provider = store if store is not None else config_store
    locks_dir = state_real / LOCKS_DIR
    _ensure_private_subdir(locks_dir)
    with ledger._key_lock(locks_dir, key):
        conn = _open_db(state_real)
        try:
            return _revoke_row(conn, state_real, provider, identity, key)
        finally:
            conn.close()


def _scan_rows(state_real: Path) -> list:
    db_path = state_real / DB_NAME
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                               timeout=ledger.LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise ledger._sqlite_error(exc) from None
    try:
        try:
            return conn.execute(
                "SELECT * FROM tasks ORDER BY created_at, key").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise errors.DeliveryError(
                    errors.TASK_NOT_FOUND, "no remote tasks found") from None
            raise ledger._sqlite_error(exc) from None
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
    finally:
        conn.close()


def _cleanup_reason(row, now: int) -> str | None:
    """Return the skip reason, or None when the task is due and revocable."""
    if any(_row_field(row, field) is None for field in _RETENTION_METADATA):
        return "metadata-missing"
    if row["state"] == STATE_OBJECT_DELETED:
        return "already-revoked"
    if now < row["retention_expires_at"]:
        return "not-due"
    return None


def cleanup(state_dir, config_path=None, *, dry_run=True, store=None,
            now=None) -> dict:
    """Preview (default) or execute retention cleanup of ledger-owned objects.

    A task is due when ``now`` reaches its immutable retention deadline; link
    expiry alone never triggers deletion. Every task is handled independently.
    """
    if now is None:
        now = int(time.time())
    elif not _is_int(now) or now < 0:
        raise _invalid("now must be a non-negative integer")
    state_real = _existing_state_dir(state_dir)
    rows = _scan_rows(state_real)

    if dry_run:
        items = []
        for row in rows:
            reason = _cleanup_reason(row, now) or "due"
            items.append({
                "key": row["key"],
                "task_id": row["task_id"],
                "object_key": row["object_key"],
                "eligible": reason == "due",
                "reason": reason,
            })
        return {"schema_version": SCHEMA_VERSION,
                "status": "cleanup-dry-run", "items": items}

    if config_path is None:
        raise _invalid("executing cleanup requires the delivery config file")
    config_store, identity = _load_config_identity(config_path)
    provider = store if store is not None else config_store
    locks_dir = state_real / LOCKS_DIR
    _ensure_private_subdir(locks_dir)

    items = []
    failures = 0
    for row in rows:
        key = row["key"]
        item = {"key": key, "task_id": row["task_id"],
                "object_key": row["object_key"]}
        reason = _cleanup_reason(row, now)
        if reason is not None:
            items.append({**item, "eligible": False, "reason": reason})
            continue
        try:
            persisted_identity = json.loads(row["provider_identity"])
        except ValueError:
            persisted_identity = None
        if persisted_identity is not None and persisted_identity != identity:
            items.append({**item, "eligible": False,
                          "reason": "identity-mismatch"})
            continue
        try:
            with ledger._key_lock(locks_dir, key):
                conn = _open_db(state_real)
                try:
                    result = _revoke_row(conn, state_real, provider,
                                         identity, key)
                finally:
                    conn.close()
        except errors.DeliveryError as exc:
            failures += 1
            items.append({**item, "eligible": True, "reason": "due",
                          "status": "failed", "error": exc.code})
            continue
        items.append({**item, "eligible": True, "reason": "due",
                      "status": result["status"],
                      "link_status": result["link_status"],
                      "link_http_status": result["link_http_status"]})
    status = "cleanup-partial" if failures else "cleanup-complete"
    return {"schema_version": SCHEMA_VERSION, "status": status, "items": items}
