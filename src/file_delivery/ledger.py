"""SQLite task ledger: idempotent local delivery with recovery and locking.

The state directory is the private credential-recovery boundary. The password
only ever lives in the FD-002 bundle's password.txt; the database stores task
metadata and non-secret paths/digests only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat as stat_module
import tempfile
import time
from pathlib import Path

from file_delivery import archive, errors, planning

SCHEMA_VERSION = 1

DB_NAME = "ledger.sqlite3"
BUNDLES_DIR = "bundles"
LOCKS_DIR = "locks"

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05

STATE_PENDING = "pending"
STATE_PACKAGED = "packaged"
STATE_STORED = "stored-local"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    archive_sha256 TEXT,
    bundle_path TEXT,
    artifact_path TEXT,
    file_count INTEGER,
    total_bytes INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_key(key: object) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise errors.DeliveryError(
            errors.INVALID_KEY, "key must match [A-Za-z0-9_-]{1,64}")
    return key


def _strict_abs_path(raw: object) -> Path:
    """Absolute path checked component-by-component against symlinks.

    Missing trailing components are allowed; the caller decides whether to
    create them.
    """
    path = Path(os.fspath(raw)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if os.path.islink(current):
            raise errors.DeliveryError(
                errors.STATE_INVALID, f"path contains a symlink component: {current}")
    return path


def _check_existing_private_dir(path: Path) -> None:
    info = os.stat(path)
    if not stat_module.S_ISDIR(info.st_mode):
        raise errors.DeliveryError(errors.STATE_INVALID, f"not a directory: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise errors.DeliveryError(
            errors.STATE_INVALID, f"directory is not owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise errors.DeliveryError(
            errors.STATE_INVALID, f"directory allows group/other access: {path}")


def _validate_private_dir(raw: object, root_real: Path) -> Path:
    """Validate a private directory location without creating anything.

    The raw path must be free of symlink components; the real path (with any
    ``..`` resolved) must sit outside the input root and not contain it.
    """
    path = _strict_abs_path(raw)
    real = Path(os.path.realpath(path))
    if real == root_real or real.is_relative_to(root_real) or root_real.is_relative_to(real):
        raise errors.DeliveryError(
            errors.STATE_INVALID,
            f"directory must be outside the input root and not contain it: {real}")
    return real


def _create_private_dir(real: Path) -> None:
    """Create (or adopt an existing) private directory, race safe."""
    try:
        # exist_ok tolerates a concurrent creator; new dirs get mode 0700.
        os.makedirs(real, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot create private directory: {exc.strerror or exc}") from None
    _check_existing_private_dir(real)


def _ensure_private_subdir(path: Path) -> None:
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
        raise errors.DeliveryError(
            errors.STATE_INVALID, f"invalid private directory: {path}")
    os.chmod(path, 0o700)


def _fingerprint(root_real: Path, store_real: Path, manifest: dict) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root_real),
        "store": str(store_real),
        "files": [[f["path"], f["size_bytes"], f["sha256"]] for f in manifest["files"]],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _task_id_for_key(key: str) -> str:
    return "fd-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _sqlite_error(exc: sqlite3.Error) -> errors.DeliveryError:
    """Map a sqlite failure to a safe structured business error."""
    text = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return errors.DeliveryError(
            errors.BUSY, "ledger database is locked by another process")
    if isinstance(exc, sqlite3.DatabaseError):
        return errors.DeliveryError(
            errors.STATE_INVALID, "ledger database is unreadable or corrupted")
    return errors.DeliveryError(errors.IO_ERROR, "ledger database access failed")


def _open_db(state_dir: Path) -> sqlite3.Connection:
    db_path = state_dir / DB_NAME
    try:
        conn = sqlite3.connect(str(db_path), timeout=LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        conn.commit()
        os.chmod(db_path, 0o600)
    except sqlite3.Error as exc:
        raise _sqlite_error(exc) from None
    except OSError as exc:
        raise errors.DeliveryError(
            errors.IO_ERROR,
            f"cannot access ledger database: {exc.strerror or exc}") from None
    return conn


@contextlib.contextmanager
def _key_lock(locks_dir: Path, key: str):
    lock_path = locks_dir / f"{key}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise errors.DeliveryError(
                        errors.BUSY,
                        f"another process is working on key: {key}") from None
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _set_state(conn: sqlite3.Connection, key: str, state: str,
               last_error: dict | None = None, **fields) -> None:
    assignments = ["state = ?", "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
    values: list = [state]
    for name, value in fields.items():
        assignments.append(f"{name} = ?")
        values.append(value)
    assignments.append("last_error = ?")
    values.append(json.dumps(last_error, ensure_ascii=True) if last_error else None)
    values.append(key)
    conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE key = ?", values)
    conn.commit()


def _record_error(conn: sqlite3.Connection, key: str, code: str, message: str) -> None:
    row = conn.execute("SELECT state FROM tasks WHERE key = ?", (key,)).fetchone()
    if row is not None:
        with contextlib.suppress(sqlite3.Error):
            _set_state(conn, key, row["state"],
                       last_error={"code": code, "message": message})


def _run_checkpoint(checkpoint, name: str) -> None:
    if checkpoint is None:
        return
    try:
        checkpoint(name)
    except OSError:
        # Recoverable: the durable artifact already exists; the state commit is
        # simply retried on the next request. Never echo callback details.
        raise errors.DeliveryError(
            errors.IO_ERROR, f"checkpoint {name} failed") from None


def _copy_object(source: Path, target: Path, expected_sha: str) -> None:
    """Copy source to target via a private temp file, verifying the hash.

    The publish step never overwrites an existing target.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as inp:
            while True:
                chunk = inp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp_path, 0o600)
        if _hash_file(tmp_path) != expected_sha:
            raise errors.DeliveryError(
                errors.IO_ERROR, "copied object failed hash verification") from None
        try:
            os.link(tmp_path, target)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise errors.DeliveryError(
                    errors.IO_ERROR, f"failed to publish object: {exc.strerror or exc}") from None
            if _hash_file(target) != expected_sha:
                raise errors.DeliveryError(
                    errors.LOCAL_STORE_CONFLICT,
                    f"existing object does not match the bundle: {target}") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _ensure_bundle(paths, root, bundle_dir: Path, recorded_sha: str | None) -> dict:
    """Return bundle metadata, packing fresh only when no durable bundle exists.

    An existing bundle is never deleted or re-encrypted: a second pack would
    mint a new password and change durable identity. If the bundle exists but
    cannot be verified (regardless of a recorded digest), the request fails
    safely; only a task whose bundle directory is absent and has no recorded
    digest may pack the first package.
    """
    if os.path.lexists(bundle_dir):
        try:
            verified = archive.verify(bundle_dir)
        except errors.DeliveryError:
            raise errors.DeliveryError(
                errors.VERIFY_FAILED,
                f"retained bundle is missing or unreadable: {bundle_dir}") from None
        else:
            if recorded_sha is not None and verified["archive_sha256"] != recorded_sha:
                raise errors.DeliveryError(
                    errors.VERIFY_FAILED,
                    f"existing bundle does not match the recorded digest: {bundle_dir}")
            return {
                "archive_sha256": verified["archive_sha256"],
                "password_file": str(bundle_dir / archive.PASSWORD_NAME),
                "file_count": verified["file_count"],
                "total_bytes": verified["total_bytes"],
            }
    elif recorded_sha is not None:
        raise errors.DeliveryError(
            errors.VERIFY_FAILED,
            f"retained bundle is missing or unreadable: {bundle_dir}")
    result = archive.pack(paths, root, bundle_dir)
    return {
        "archive_sha256": result["archive_sha256"],
        "password_file": result["password_file"],
        "file_count": result["file_count"],
        "total_bytes": result["total_bytes"],
    }


def deliver_local(paths, root, state_dir, store_dir, key, checkpoint=None) -> dict:
    """Idempotently package inputs and publish a verified local object."""
    key = _validate_key(key)
    root_real = Path(os.path.realpath(os.fspath(Path(root).expanduser())))
    state_real = _validate_private_dir(state_dir, root_real)
    store_real = _validate_private_dir(store_dir, root_real)
    if (state_real == store_real or state_real.is_relative_to(store_real)
            or store_real.is_relative_to(state_real)):
        raise errors.DeliveryError(
            errors.STATE_INVALID,
            "state directory and store directory must be disjoint")
    _create_private_dir(state_real)
    _create_private_dir(store_real)
    bundles_dir = state_real / BUNDLES_DIR
    locks_dir = state_real / LOCKS_DIR
    _ensure_private_subdir(bundles_dir)
    _ensure_private_subdir(locks_dir)

    manifest = planning.plan(paths, root)
    fingerprint = _fingerprint(root_real, store_real, manifest)

    with _key_lock(locks_dir, key):
        conn = _open_db(state_real)
        try:
            row = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
            reused = row is not None
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise errors.DeliveryError(
                        errors.IDEMPOTENCY_CONFLICT,
                        "key already used with different content or store; "
                        "original task and bundle are preserved")
                task_id = row["task_id"]
                recorded_sha = row["archive_sha256"]
            else:
                task_id = _task_id_for_key(key)
                conn.execute(
                    "INSERT INTO tasks (key, task_id, fingerprint, state) VALUES (?, ?, ?, ?)",
                    (key, task_id, fingerprint, STATE_PENDING))
                conn.commit()
                recorded_sha = None

            bundle_dir = bundles_dir / task_id
            try:
                bundle = _ensure_bundle(paths, root, bundle_dir, recorded_sha)
            except errors.DeliveryError as exc:
                _record_error(conn, key, exc.code, exc.message)
                raise
            try:
                _run_checkpoint(checkpoint, "after_pack")
            except errors.DeliveryError as exc:
                _record_error(conn, key, exc.code, exc.message)
                raise
            _set_state(conn, key, STATE_PACKAGED,
                       archive_sha256=bundle["archive_sha256"],
                       bundle_path=str(bundle_dir),
                       file_count=bundle["file_count"],
                       total_bytes=bundle["total_bytes"])

            object_path = store_real / f"{task_id}.zip"
            try:
                if os.path.lexists(object_path):
                    if _hash_file(object_path) != bundle["archive_sha256"]:
                        raise errors.DeliveryError(
                            errors.LOCAL_STORE_CONFLICT,
                            f"existing object does not match the bundle: {object_path}")
                else:
                    _copy_object(bundle_dir / archive.ARCHIVE_NAME,
                                 object_path, bundle["archive_sha256"])
                    os.chmod(object_path, 0o600)
                _run_checkpoint(checkpoint, "after_store")
            except errors.DeliveryError as exc:
                _record_error(conn, key, exc.code, exc.message)
                raise
            _set_state(conn, key, STATE_STORED, artifact_path=str(object_path))
        except sqlite3.Error as exc:
            raise _sqlite_error(exc) from None
        finally:
            conn.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATE_STORED,
        "task_id": task_id,
        "key": key,
        "archive_sha256": bundle["archive_sha256"],
        "artifact_path": str(object_path),
        "password_file": bundle["password_file"],
        "file_count": bundle["file_count"],
        "total_bytes": bundle["total_bytes"],
        "reused": reused,
    }


def status(state_dir, key) -> dict:
    """Return the persisted task for key without touching input sources."""
    key = _validate_key(key)
    state_real = _strict_abs_path(state_dir)
    db_path = state_real / DB_NAME
    if not os.path.lexists(db_path):
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no ledger task found for key: {key}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=LOCK_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise _sqlite_error(exc) from None
    try:
        try:
            row = conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise errors.DeliveryError(
                    errors.TASK_NOT_FOUND, f"no ledger task found for key: {key}") from None
            raise _sqlite_error(exc) from None
        except sqlite3.Error as exc:
            raise _sqlite_error(exc) from None
    finally:
        conn.close()
    if row is None:
        raise errors.DeliveryError(
            errors.TASK_NOT_FOUND, f"no ledger task found for key: {key}")
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": row["state"],
        "task_id": row["task_id"],
        "key": key,
    }
    for field in ("archive_sha256", "file_count", "total_bytes",
                  "bundle_path", "artifact_path"):
        if row[field] is not None:
            result[field] = row[field]
    if row["last_error"]:
        result["last_error"] = json.loads(row["last_error"])
    return result
