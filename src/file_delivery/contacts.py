"""Contact alias resolution and strict single-mailbox validation (FD-005).

A recipient is either one entire ASCII addr-spec (used directly, domain
lower-cased) or a Unicode alias looked up in an owner-private contacts file.
Anything that looks like a mailbox but fails validation is RECIPIENT_INVALID;
aliases never reach the SMTP transport unvalidated, so header/address
injection is impossible before any transport call.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
from pathlib import Path

from file_delivery import errors

SCHEMA_VERSION = 1

# RFC 5322 atext characters; quoted local parts are deliberately rejected.
_ATEXT = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "!#$%&'*+-/=?^_`{|}~")


def _contacts_invalid(message: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.CONTACTS_INVALID, message)


def _recipient_invalid(message: str) -> errors.DeliveryError:
    return errors.DeliveryError(errors.RECIPIENT_INVALID, message)


def _has_ascii_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def parse_addr_spec(value) -> str | None:
    """Validate one entire ASCII addr-spec and return its canonical form.

    The whole string is the mailbox: no substring parsing, no display names,
    no quoted local parts, no non-ASCII bytes. Returns ``local@domain``
    with the local case preserved and the domain lower-cased, or None.
    """
    if not isinstance(value, str):
        return None
    if len(value) > 254:
        return None
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if _has_ascii_controls(value) or any(char.isspace() for char in value):
        return None
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if not _valid_local(local) or not _valid_domain(domain):
        return None
    return f"{local}@{domain.lower()}"


def _valid_local(local: str) -> bool:
    if not local or len(local) > 64:
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    return all(char == "." or char in _ATEXT for char in local)


def _valid_domain(domain: str) -> bool:
    if not domain or len(domain) > 253:
        return False
    for label in domain.split("."):
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not all(char.isascii() and (char.isalnum() or char == "-") for char in label):
            return False
    return True


def _reject_duplicates(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen.add(key)
    return dict(pairs)


def _check_private_file(path: Path) -> None:
    """Require an existing owner-private regular file with no symlink path."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if os.path.lexists(current) and os.path.islink(current):
            raise _contacts_invalid(
                "contacts path contains a symlink component")
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        raise _contacts_invalid("contacts file is missing or unreadable") from None
    if not stat_module.S_ISREG(info.st_mode):
        raise _contacts_invalid("contacts file is not a regular file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise _contacts_invalid("contacts file is not owned by the current user")
    if info.st_mode & 0o077:
        raise _contacts_invalid("contacts file must be owner-private")


def _load_contacts(contacts_path) -> dict:
    path = Path(os.fspath(contacts_path)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    _check_private_file(path)
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise _contacts_invalid("contacts file is not readable") from None
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (ValueError, UnicodeError):
        raise _contacts_invalid("contacts file is not valid JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "contacts"}:
        raise _contacts_invalid(
            "contacts file must contain exactly schema_version and contacts")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise _contacts_invalid("unsupported contacts schema_version")
    entries = payload["contacts"]
    if not isinstance(entries, dict):
        raise _contacts_invalid("contacts must be an object of alias to mailbox")
    table = {}
    for alias, target in entries.items():
        if not isinstance(alias, str) or not alias or _has_ascii_controls(alias):
            raise _contacts_invalid("contact aliases must be nonempty control-free text")
        if not isinstance(target, str):
            raise _contacts_invalid("contact target must be a single mailbox string")
        address = parse_addr_spec(target)
        if address is None:
            raise _contacts_invalid(
                f"contact target for alias is not a valid mailbox: {alias!r}")
        table[alias] = address
    return table


def resolve(recipient, contacts_path=None) -> str:
    """Return the canonical mailbox for a direct addr-spec or alias."""
    if not isinstance(recipient, str) or not recipient:
        raise _recipient_invalid("recipient must be a nonempty string")
    if "@" in recipient:
        address = parse_addr_spec(recipient)
        if address is None:
            raise _recipient_invalid("recipient is not a valid mailbox address")
        return address
    if _has_ascii_controls(recipient):
        raise _recipient_invalid("recipient alias contains control characters")
    if contacts_path is None:
        raise _contacts_invalid("alias recipient requires a contacts file")
    table = _load_contacts(contacts_path)
    if recipient in table:
        return table[recipient]
    raise errors.DeliveryError(
        errors.CONTACT_NOT_FOUND, f"unknown contact alias: {recipient!r}")
