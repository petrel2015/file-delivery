"""Read-only manifest planning: validate inputs, hash files, build the manifest."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat as stat_module
from pathlib import Path

from file_delivery import errors

SCHEMA_VERSION = 1

SENSITIVE_COMPONENTS = {".git", ".ssh", ".aws", ".kube"}
SENSITIVE_BASENAME_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "credentials*", "secret*"]


def is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in SENSITIVE_COMPONENTS:
        return True
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SENSITIVE_BASENAME_PATTERNS)


def _lexical_walk(raw: str | Path, missing_code: str, link_code: str) -> Path:
    """Resolve a path component by component without collapsing symlinks first.

    Each real component is checked for being a symlink (link_code) and for
    existence (missing_code) before any later ".." can lexically remove it,
    so paths like root/alias/../safe.txt are still rejected when alias is a
    symlink. Normal non-symlink ".." components remain supported.
    """
    raw_path = Path(os.fspath(raw))
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    stack: list[str] = []
    for part in raw_path.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
        current = Path(raw_path.anchor, *stack)
        if os.path.islink(current):
            raise DeliveryError(link_code, f"path contains a symlink component: {current}")
        if not os.path.lexists(current):
            raise DeliveryError(missing_code, f"path does not exist: {current}")
    return Path(raw_path.anchor, *stack)


class DeliveryError(errors.DeliveryError):
    """Local alias so callers can import it from the planner module."""


def _validate_root(root: str | Path) -> Path:
    try:
        root_path = _lexical_walk(root, errors.INVALID_ROOT, errors.INVALID_ROOT)
    except DeliveryError as exc:
        raise DeliveryError(errors.INVALID_ROOT, f"invalid root: {exc.message}") from None
    if not root_path.is_dir():
        raise DeliveryError(errors.INVALID_ROOT, f"invalid root, not a directory: {root_path}")
    for part in root_path.parts[1:]:
        if is_sensitive_name(part):
            raise DeliveryError(errors.SENSITIVE_PATH, f"sensitive path component in root: {part}")
    return root_path


def _resolve_input(path: str | Path, root_path: Path) -> Path:
    input_path = _lexical_walk(path, errors.INPUT_NOT_FOUND, errors.SYMLINK_NOT_ALLOWED)
    if not input_path.is_relative_to(root_path):
        raise DeliveryError(errors.PATH_NOT_ALLOWED, f"path outside root: {input_path}")
    for part in input_path.relative_to(root_path).parts:
        if is_sensitive_name(part):
            raise DeliveryError(errors.SENSITIVE_PATH, f"sensitive path component: {part}")
    return input_path


def _collect(path: Path, root_path: Path, selected: dict[Path, None]) -> None:
    st = os.lstat(path)
    if stat_module.S_ISLNK(st.st_mode):
        raise DeliveryError(errors.SYMLINK_NOT_ALLOWED, f"symlink not allowed: {path}")
    if stat_module.S_ISDIR(st.st_mode):
        with os.scandir(path) as entries:
            children = sorted(entries, key=lambda e: e.name)
        for entry in children:
            child = path / entry.name
            if entry.is_symlink():
                raise DeliveryError(errors.SYMLINK_NOT_ALLOWED, f"symlink not allowed: {child}")
            if is_sensitive_name(entry.name):
                raise DeliveryError(errors.SENSITIVE_PATH, f"sensitive path: {child}")
            if entry.is_dir(follow_symlinks=False):
                _collect(child, root_path, selected)
            elif entry.is_file(follow_symlinks=False):
                selected.setdefault(child, None)
            else:
                raise DeliveryError(errors.UNSUPPORTED_FILE_TYPE, f"unsupported file type: {child}")
    elif stat_module.S_ISREG(st.st_mode):
        selected.setdefault(path, None)
    else:
        raise DeliveryError(errors.UNSUPPORTED_FILE_TYPE, f"unsupported file type: {path}")


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _hash_file(path: Path) -> tuple[int, str]:
    try:
        before = os.stat(path)
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        after = os.stat(path)
    except OSError as exc:
        raise DeliveryError(errors.IO_ERROR, f"failed to read {path}: {exc.strerror or exc}") from None
    if size != before.st_size or _identity(before) != _identity(after):
        raise DeliveryError(errors.INPUT_CHANGED, f"input changed during planning: {path}")
    return before.st_size, digest.hexdigest()


def plan(paths: list[str] | tuple[str, ...], root: str | Path) -> dict:
    """Validate inputs under root and return the planned manifest."""
    root_path = _validate_root(root)
    selected: dict[Path, None] = {}
    for raw in paths:
        input_path = _resolve_input(raw, root_path)
        _collect(input_path, root_path, selected)
    if not selected:
        raise DeliveryError(errors.EMPTY_SELECTION, "no regular files selected")
    files = []
    for path in sorted(selected, key=lambda p: p.relative_to(root_path).as_posix()):
        size, sha256 = _hash_file(path)
        files.append({
            "path": path.relative_to(root_path).as_posix(),
            "size_bytes": size,
            "sha256": sha256,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(f["size_bytes"] for f in files),
    }
