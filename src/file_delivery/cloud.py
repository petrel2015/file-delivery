"""Independent cloud operations reusing durable remote object identities."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from . import archive, errors, ledger, notification, remote


def _safe(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise ledger._sqlite_error(exc) from None
        except OSError:
            raise errors.DeliveryError(errors.IO_ERROR, 'cloud operation storage access failed') from None
    return wrapped


def _state(state_dir):
    state = remote._existing_state_dir(state_dir)
    notification._validate_private_file(state / remote.DB_NAME, errors.STATE_INVALID, 'remote ledger')
    return state


def _provider(config_path, store=None):
    provider, identity = remote._load_config_identity(config_path)
    return (store if store is not None else provider), identity


def _owned(state, key, identity):
    key = ledger._validate_key(key)
    task = notification._load_remote_task(state, key)
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
        state = _state(state_dir)
        for row in remote._scan_rows(state):
            if row['provider_identity'] != json.dumps(identity, sort_keys=True, separators=(',', ':')):
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
                try:
                    names = [entry['path'] for entry in archive._validate_manifest(json.loads(path.read_text()))]
                except (ValueError, errors.DeliveryError):
                    raise remote._state_invalid('original file manifest is invalid') from None
            owned[row['object_key']] = {'delivery_key': row['key'], 'task_id': task_id,
                'original_files': names, 'saved_state': row['state'],
                'retention_expires_at': row['retention_expires_at'], 'ownership': 'owned'}
    page = remote._provider('list', provider.list_objects, prefix=prefix, marker=marker, limit=limit)
    items = []
    for item in page['items']:
        item = dict(item)
        item.update(owned.get(item['object_key'], {'ownership': 'unmanaged', 'original_files': None}))
        if not query or query.casefold() in (' '.join(item['original_files'] or []) + ' ' + item['object_key']).casefold():
            items.append(item)
    return {'schema_version': 1, 'status': 'listed', 'source': 'live-qiniu',
            'items': items, 'next_marker': page['next_marker'], 'search_scope': 'current-page'}
