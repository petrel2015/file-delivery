"""Independent cloud operations reusing durable remote object identities."""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from . import archive, errors, ledger, notification, remote


def _safe(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
        except OSError:
            raise errors.DeliveryError(errors.IO_ERROR, 'cloud operation storage access failed') from None
    return wrapped

LINKS_DB = 'links.sqlite3'
LINKS_DIR = 'remote-links'
_LINK_SCHEMA = '''CREATE TABLE IF NOT EXISTS links (
    key TEXT PRIMARY KEY, delivery_key TEXT NOT NULL, task_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, expires_at INTEGER NOT NULL,
    url_sha256 TEXT, state TEXT NOT NULL)'''


def _links_db(state, *, readonly=False):
    path = state / LINKS_DB
    if not os.path.lexists(path):
        if readonly:
            raise errors.DeliveryError(errors.TASK_NOT_FOUND, 'link version not found')
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        except FileExistsError:
            pass
    notification._validate_private_file(path, errors.STATE_INVALID, 'link ledger')
    conn = sqlite3.connect(f'file:{path}?mode=ro' if readonly else str(path),
        uri=readonly, timeout=ledger.LOCK_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute(_LINK_SCHEMA); conn.commit()
    return conn


def _link_path(state, task, link_key):
    return state / LINKS_DIR / task['task_id'] / f'{link_key}.json'


def _validate_link_row(row):
    if (row['state'] not in ('prepared', 'link-verified')
            or not remote._is_int(row['expires_at'])
            or not isinstance(row['fingerprint'], str) or not notification._HEX64_RE.fullmatch(row['fingerprint'])
            or (row['url_sha256'] is not None and (not isinstance(row['url_sha256'], str)
                or not notification._HEX64_RE.fullmatch(row['url_sha256'])))):
        raise remote._state_invalid('invalid link version record')


def apply_link_version(state, task, link_key):
    """Load only a ledger-bound immutable version; used by notification core."""
    link_key = ledger._validate_key(link_key)
    conn = _links_db(state, readonly=True)
    try: row = conn.execute('SELECT * FROM links WHERE key=?', (link_key,)).fetchone()
    finally: conn.close()
    if row is None:
        raise errors.DeliveryError(errors.TASK_NOT_FOUND, 'link version not found')
    _validate_link_row(row)
    if row['delivery_key'] != task['key'] or row['task_id'] != task['task_id']:
        raise errors.DeliveryError(errors.IDEMPOTENCY_CONFLICT, 'link version belongs to another delivery')
    if row['state'] != 'link-verified' or not row['url_sha256']:
        raise errors.DeliveryError(errors.DELIVERY_NOT_READY, 'link version not verified')
    result = dict(task)
    result['original_delivery_state'] = task['state']
    result.update(expires_at=row['expires_at'], url_sha256=row['url_sha256'],
                  version_handoff_path=str(_link_path(state, task, link_key)), link_key=link_key)
    if result['state'] == remote.STATE_UPLOADED:
        result['state'] = remote.STATE_LINK_VERIFIED
    return result


@_safe
def link_status(state_dir, delivery_key, link_key):
    state = _state(state_dir)
    task = notification._load_remote_task(state, ledger._validate_key(delivery_key), require_deadline=False)
    task = apply_link_version(state, task, link_key)
    notification._load_validated_handoff(state, task)
    return {'schema_version': 1, 'status': 'link-verified', 'delivery_state': task['original_delivery_state'],
        'delivery_key': delivery_key, 'link_key': link_key, 'task_id': task['task_id'],
        'expires_at': task['expires_at'], 'expired': int(time.time()) >= task['expires_at'],
        'handoff_file': task['version_handoff_path'], 'password_file': task['password_file'],
        'archive_sha256': task['archive_sha256'], 'archive_size': task['archive_size']}


@_safe
def renew_link(state_dir, config_path, delivery_key, link_key, *, ttl_seconds=604800, store=None):
    state = _state(state_dir)
    link_key = ledger._validate_key(link_key)
    lock_name = 'link-' + hashlib.sha256(link_key.encode()).hexdigest()[:32]
    with ledger._key_lock(state / remote.LOCKS_DIR, lock_name):
        return _renew_link(state, config_path, delivery_key, link_key, ttl_seconds=ttl_seconds, store=store)


def _renew_link(state_dir, config_path, delivery_key, link_key, *, ttl_seconds, store):
    delivery_key = ledger._validate_key(delivery_key)
    link_key = ledger._validate_key(link_key)
    if not remote._is_int(ttl_seconds) or not 1 <= ttl_seconds <= remote.MAX_TTL_SECONDS:
        raise errors.DeliveryError(errors.CONFIG_INVALID, 'invalid link lifetime')
    state = _state(state_dir)
    provider, identity = _provider(config_path, store)
    with ledger._key_lock(state / remote.LOCKS_DIR, delivery_key):
        task, original_row = _owned(state, delivery_key, identity)
        notification._verify_bundle(state, task)
        fingerprint = hashlib.sha256(json.dumps({'delivery_key': delivery_key,
            'archive_sha256': task['archive_sha256'], 'identity': identity,
            'ttl_seconds': ttl_seconds}, sort_keys=True).encode()).hexdigest()
        conn = _links_db(state)
        try:
            row = conn.execute('SELECT * FROM links WHERE key=?', (link_key,)).fetchone()
            reused = row is not None
            if row is not None:
                _validate_link_row(row)
                if row['fingerprint'] != fingerprint or row['delivery_key'] != delivery_key or row['task_id'] != task['task_id']:
                    raise errors.DeliveryError(errors.IDEMPOTENCY_CONFLICT, 'link key already used for another request')
                if row['state'] == 'link-verified':
                    result = link_status(state, delivery_key, link_key)
                    result['reused'] = True
                    return result
                deadline = row['expires_at']
            else:
                deadline = int(time.time()) + ttl_seconds
                retention = original_row['retention_expires_at']
                if not remote._is_int(retention) or deadline > retention:
                    raise errors.DeliveryError(errors.CONFIG_INVALID, 'new link must expire within saved object retention')
                conn.execute('INSERT INTO links VALUES (?,?,?,?,?,NULL,?)',
                    (link_key, delivery_key, task['task_id'], fingerprint, deadline, 'prepared'))
                conn.commit()
            if deadline <= int(time.time()):
                raise errors.DeliveryError(errors.LINK_EXPIRED, 'saved link version deadline has passed')
            remote._provider('bucket privacy', provider.ensure_private)
            signed = remote._provider('fixed deadline signing', provider.signed_url_at, task['object_key'], deadline)
            if signed.get('expires_at') != deadline or not isinstance(signed.get('url'), str):
                raise errors.DeliveryError(errors.REMOTE_ERROR, 'signing response was malformed')
            exact = remote._provider('new link integrity', provider.verify_link,
                signed['url'], task['archive_sha256'], task['archive_size'])
            if exact.get('sha256') != task['archive_sha256'] or exact.get('size') != task['archive_size']:
                raise errors.DeliveryError(errors.REMOTE_INTEGRITY, 'new signed link failed integrity verification')
            digest = hashlib.sha256(signed['url'].encode()).hexdigest()
            if row is not None and row['url_sha256'] is not None and row['url_sha256'] != digest:
                raise remote._state_invalid('saved signing digest differs; original intent preserved')
            conn.execute('UPDATE links SET url_sha256=? WHERE key=?', (digest, link_key)); conn.commit()
            directory = state / LINKS_DIR
            remote._ensure_private_subdir(directory)
            remote._ensure_private_subdir(directory / task['task_id'])
            path = _link_path(state, task, link_key)
            expected = dict(task, expires_at=deadline)
            if os.path.lexists(path):
                notification._check_state_parents(state, path)
                notification._validate_private_file(path, errors.STATE_INVALID, 'versioned handoff')
                remote._load_handoff(path, remote._handoff_expectation(expected, task['task_id'], task['object_key']), digest)
            else:
                remote._write_handoff(path, {'url': signed['url'], 'expires_at': deadline,
                    **{field: task[field] for field in ('task_id', 'object_key', 'archive_sha256', 'archive_size', 'password_file')}})
            conn.execute("UPDATE links SET state='link-verified' WHERE key=?", (link_key,)); conn.commit()
        finally: conn.close()
    result = link_status(state, delivery_key, link_key); result['reused'] = reused
    return result


def _state(state_dir):
    state = remote._existing_state_dir(state_dir)
    notification._validate_private_file(state / remote.DB_NAME, errors.STATE_INVALID, 'remote ledger')
    return state


def _provider(config_path, store=None):
    provider, identity = remote._load_config_identity(config_path)
    return (store if store is not None else provider), identity


def _owned(state, key, identity):
    key = ledger._validate_key(key)
    task = notification._load_remote_task(state, key, require_deadline=False)
    rows = remote._scan_rows(state)
    row = next(row for row in rows if row['key'] == key)
    try:
        saved = json.loads(row['provider_identity'])
    except (ValueError, TypeError):
        raise remote._state_invalid('task destination metadata is missing') from None
    if saved != identity:
        raise errors.DeliveryError(errors.IDEMPOTENCY_CONFLICT, 'config destination differs from original task')
    if task['state'] in (remote.STATE_REVOKING, remote.STATE_OBJECT_DELETED):
        raise errors.DeliveryError(errors.TASK_REVOKED, 'object was revoked')
    if task['state'] not in (remote.STATE_UPLOADED, remote.STATE_LINK_VERIFIED):
        raise errors.DeliveryError(errors.DELIVERY_NOT_READY, 'object upload is not confirmed')
    return task, row


@_safe
def list_files(config_path, state_dir=None, *, prefix='', marker='', limit=100, query='', store=None):
    if not isinstance(query, str) or len(query) > 1024 or any(ord(c) < 32 for c in query):
        raise errors.DeliveryError(errors.CONFIG_INVALID, 'invalid search query')
    provider, identity = _provider(config_path, store)
    owned = {}
    if state_dir is not None:
        _state(state_dir)  # Local state validation precedes provider access.
    page = remote._provider('list', provider.list_objects, prefix=prefix, marker=marker, limit=limit)
    live_keys = {item['object_key'] for item in page['items']}
    if state_dir is not None:
        state = _state(state_dir)
        for row in remote._scan_rows(state):
            if row['object_key'] not in live_keys:
                continue
            try:
                if json.loads(row['provider_identity']) != identity:
                    continue
            except (TypeError, ValueError):
                continue
            task_id = row['task_id']
            if not isinstance(task_id, str) or not remote._TASK_ID_RE.fullmatch(task_id):
                raise remote._state_invalid('invalid task identity')
            if row['object_key'] != f'file-delivery/{task_id}.zip':
                raise remote._state_invalid('invalid object ownership')
            names = None
            path = state / remote.BUNDLES_DIR / task_id / archive.MANIFEST_NAME
            if os.path.lexists(path):
                notification._check_state_parents(state, path)
                notification._validate_private_file(path, errors.STATE_INVALID, 'manifest')
                task = notification._load_remote_task(state, row['key'], require_deadline=False)
                notification._verify_bundle(state, task)
                try:
                    names = [entry['path'] for entry in archive._validate_manifest(json.loads(path.read_text()))]
                except (ValueError, errors.DeliveryError):
                    raise remote._state_invalid('original file manifest is invalid') from None
            owned[row['object_key']] = {'delivery_key': row['key'], 'task_id': task_id,
                'original_files': names, 'saved_state': row['state'],
                'retention_expires_at': row['retention_expires_at'], 'ownership': 'owned'}
    items = []
    for item in page['items']:
        item = dict(item)
        item.update(owned.get(item['object_key'], {'ownership': 'unmanaged', 'original_files': None}))
        if not query or query.casefold() in (' '.join(item['original_files'] or []) + ' ' + item['object_key']).casefold():
            items.append(item)
    return {'schema_version': 1, 'status': 'listed', 'source': 'live-qiniu',
            'items': items, 'next_marker': page['next_marker'], 'search_scope': 'current-page'}


@_safe
def download(state_dir, config_path, key, output_path, *, store=None):
    state = _state(state_dir)
    provider, identity = _provider(config_path, store)
    output = ledger._strict_abs_path(output_path)
    remote._reject_path_symlinks(output, 'download destination')
    if os.path.lexists(output):
        raise errors.DeliveryError(errors.OUTPUT_EXISTS, 'download destination already exists')
    if not output.parent.is_dir():
        raise errors.DeliveryError(errors.OUTPUT_NOT_ALLOWED, 'download parent directory must exist')
    key = ledger._validate_key(key)
    with ledger._key_lock(state / remote.LOCKS_DIR, key):
        task, _ = _owned(state, key, identity)
        remote._provider('bucket privacy', provider.ensure_private)
        fd, temp_name = tempfile.mkstemp(prefix='.file-delivery-download-', dir=output.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, 'w+b') as handle:
                remote._provider('download', provider.download_to, task['object_key'], handle,
                    task['archive_sha256'], task['archive_size'])
                handle.flush(); os.fsync(handle.fileno()); handle.seek(0)
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: handle.read(65536), b''):
                    size += len(chunk); digest.update(chunk)
                if size != task['archive_size'] or digest.hexdigest() != task['archive_sha256']:
                    raise errors.DeliveryError(errors.REMOTE_INTEGRITY, 'downloaded archive failed verification')
            remote._reject_path_symlinks(output, 'download destination')
            try:
                os.link(temp, output)  # Atomic publication that never replaces an existing file.
            except FileExistsError:
                raise errors.DeliveryError(errors.OUTPUT_EXISTS, 'download destination already exists') from None
            directory_fd = os.open(output.parent, os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        finally:
            with contextlib.suppress(FileNotFoundError): temp.unlink()
    return {'schema_version': 1, 'status': 'downloaded-verified', 'key': key,
        'task_id': task['task_id'], 'output_path': str(output),
        'archive_sha256': task['archive_sha256'], 'archive_size': task['archive_size'],
        'password_file': task['password_file'], 'format': 'aes256-zip'}
