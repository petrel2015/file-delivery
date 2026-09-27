"""Local AES-256 ZIP bundle creation and read-only decryption verification.

WinZip AES encryption protects file contents only; entry filenames stay
visible in the archive. The password is generated per bundle from a
cryptographically secure random source and is only ever written to
password.txt inside the private bundle directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import tempfile
import zlib
from pathlib import Path, PurePosixPath

from file_delivery import errors, planning

SCHEMA_VERSION = 1

ARCHIVE_NAME = "archive.zip"
MANIFEST_NAME = "manifest.json"
PASSWORD_NAME = "password.txt"

PASSWORD_ENTROPY_BYTES = 24  # 192 bits, above the required 128-bit minimum

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _load_pyzipper():
    try:
        import pyzipper
    except ImportError:
        raise errors.DeliveryError(
            errors.DEPENDENCY_MISSING,
            "optional dependency 'pyzipper' is required: install with pip install 'file-delivery[archive]'",
        ) from None
    return pyzipper


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_entry(source: Path) -> tuple[bytes, str, int]:
    """Read a file once, returning content, sha256 and size."""
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with open(source, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            chunks.append(chunk)
    data = b"".join(chunks)
    return data, digest.hexdigest(), len(data)


def _is_safe_relpath(name: str) -> bool:
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        return False
    pure = PurePosixPath(name)
    if pure.is_absolute():
        return False
    parts = pure.parts
    if not parts:
        return False
    return all(part not in ("", ".", "..") for part in parts)


def _validate_manifest(manifest: object) -> list[dict]:
    """Validate manifest structure and return its file entries."""
    if not isinstance(manifest, dict):
        raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest is not a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise errors.DeliveryError(errors.VERIFY_FAILED, "unsupported manifest schema_version")
    if manifest.get("status") != "planned":
        raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest status is not 'planned'")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest has no file entries")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest entry is not an object")
        path = entry.get("path")
        if not _is_safe_relpath(path):
            raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest entry has an unsafe path")
        if path in seen:
            raise errors.DeliveryError(errors.VERIFY_FAILED, f"duplicate manifest entry: {path}")
        seen.add(path)
        size = entry.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest entry has invalid size_bytes")
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not _SHA256_HEX.match(sha):
            raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest entry has invalid sha256")
    if manifest.get("file_count") != len(files):
        raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest file_count mismatch")
    total = sum(entry["size_bytes"] for entry in files)
    if manifest.get("total_bytes") != total:
        raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest total_bytes mismatch")
    return files


def _aes_key_bits(info) -> int | None:
    """Return WinZip AES key length in bits, or None if the entry is not AES.

    pyzipper's AESZipInfo decodes compression method 99 into the real method
    (e.g. 8/DEFLATE) and exposes the AES strength as ``wz_aes_strength``; the
    raw 0x9901 extra-field parse is kept as fallback for other readers.
    """
    strength = getattr(info, "wz_aes_strength", None)
    if isinstance(strength, int) and strength in (1, 2, 3):
        return {1: 128, 2: 192, 3: 256}[strength]
    extra = info.extra
    i = 0
    while i + 4 <= len(extra):
        header_id, length = struct.unpack("<HH", extra[i:i + 4])
        data = extra[i + 4:i + 4 + length]
        if header_id == 0x9901 and length >= 5:
            strength = data[4]
            return {1: 128, 2: 192, 3: 256}.get(strength)
        i += 4 + length
    return None


def _check_encrypted_archive(pyzipper, archive_path: Path, password: str, files: list[dict]) -> None:
    """Decrypt every entry and compare size/SHA256 against the manifest entries."""
    try:
        with pyzipper.AESZipFile(archive_path, "r") as archive:
            archive.setpassword(password.encode("utf-8"))
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise errors.DeliveryError(errors.VERIFY_FAILED, "archive contains duplicate entries")
            expected = {entry["path"] for entry in files}
            actual = set(names)
            if actual != expected:
                missing = sorted(expected - actual)
                unexpected = sorted(actual - expected)
                raise errors.DeliveryError(
                    errors.VERIFY_FAILED,
                    f"archive entries do not match manifest (missing={missing}, unexpected={unexpected})",
                )
            for info in infos:
                if info.filename.endswith("/"):
                    raise errors.DeliveryError(errors.VERIFY_FAILED, "archive contains a directory entry")
                if not info.flag_bits & 0x1:
                    raise errors.DeliveryError(errors.VERIFY_FAILED, "archive entry is not encrypted")
                if _aes_key_bits(info) != 256:
                    raise errors.DeliveryError(errors.VERIFY_FAILED, "archive entry is not AES-256")
            by_name = {entry["path"]: entry for entry in files}
            for name in sorted(by_name):
                data = archive.read(name)
                entry = by_name[name]
                if len(data) != entry["size_bytes"] or _hash_bytes(data) != entry["sha256"]:
                    raise errors.DeliveryError(
                        errors.VERIFY_FAILED, f"decrypted content mismatch for entry: {name}")
    except errors.DeliveryError:
        raise
    except (RuntimeError, pyzipper.BadZipFile, OSError, EOFError, struct.error, zlib.error,
            UnicodeError) as exc:
        # RuntimeError is zipfile's wrong-password signal; never echo the password.
        raise errors.DeliveryError(
            errors.VERIFY_FAILED, f"failed to decrypt or read archive: {type(exc).__name__}") from None


def _validate_output_path(output_dir: str | Path, root_real: Path) -> Path:
    out_path = Path(os.fspath(output_dir)).expanduser()
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    # Root equality/nesting is checked before existence so that an output that
    # collides with the root reports OUTPUT_NOT_ALLOWED, not OUTPUT_EXISTS.
    resolved = out_path.parent.resolve() / out_path.name
    if resolved == root_real or resolved.is_relative_to(root_real):
        raise errors.DeliveryError(
            errors.OUTPUT_NOT_ALLOWED, f"output directory must be outside the input root: {resolved}")
    if root_real.is_relative_to(resolved):
        raise errors.DeliveryError(
            errors.OUTPUT_NOT_ALLOWED, f"output directory must not contain the input root: {resolved}")
    if os.path.lexists(out_path):
        raise errors.DeliveryError(errors.OUTPUT_EXISTS, f"output already exists: {out_path}")
    parent = out_path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise errors.DeliveryError(errors.IO_ERROR, f"output parent directory is missing or invalid: {parent}")
    return out_path


def _write_private_file(path: Path, data: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def pack(paths: list[str] | tuple[str, ...], root: str | Path, output_dir: str | Path) -> dict:
    """Create a verified encrypted bundle in a new private output directory."""
    pyzipper = _load_pyzipper()
    root_real = Path(root).expanduser().resolve()
    out_path = _validate_output_path(output_dir, root_real)

    manifest = planning.plan(paths, root)
    files = manifest["files"]

    staging = Path(tempfile.mkdtemp(prefix=f".{out_path.name}.tmp-", dir=out_path.parent))
    try:
        os.chmod(staging, 0o700)
        password = secrets.token_urlsafe(PASSWORD_ENTROPY_BYTES)

        archive_path = staging / ARCHIVE_NAME
        with pyzipper.AESZipFile(
            archive_path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES,
        ) as archive:
            archive.setpassword(password.encode("utf-8"))
            for entry in files:
                # Re-read content during packaging and confirm it still matches
                # the manifest hashes taken at planning time.
                data, sha256, size = _read_entry(root_real / entry["path"])
                if size != entry["size_bytes"] or sha256 != entry["sha256"]:
                    raise errors.DeliveryError(
                        errors.INPUT_CHANGED, f"input changed during packaging: {entry['path']}")
                archive.writestr(entry["path"], data)
        os.chmod(archive_path, 0o600)

        # Decrypt everything back and compare against the manifest before publishing.
        _check_encrypted_archive(pyzipper, archive_path, password, files)

        _write_private_file(staging / MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
        _write_private_file(staging / PASSWORD_NAME, password + "\n")

        try:
            os.rename(staging, out_path)
        except OSError as exc:
            if os.path.lexists(out_path):
                raise errors.DeliveryError(
                    errors.OUTPUT_EXISTS, f"output already exists: {out_path}") from None
            raise errors.DeliveryError(
                errors.IO_ERROR, f"failed to publish bundle: {exc.strerror or exc}") from None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "packaged",
        "bundle_dir": str(out_path),
        "archive_path": str(out_path / ARCHIVE_NAME),
        "manifest_path": str(out_path / MANIFEST_NAME),
        "password_file": str(out_path / PASSWORD_NAME),
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "archive_sha256": _hash_file(out_path / ARCHIVE_NAME),
    }


def verify(bundle_dir: str | Path, password_file: str | Path | None = None) -> dict:
    """Read-only verification of an existing bundle; never extracts or writes."""
    pyzipper = _load_pyzipper()
    bundle = Path(os.fspath(bundle_dir)).expanduser()
    if not bundle.is_absolute():
        bundle = Path.cwd() / bundle
    archive_path = bundle / ARCHIVE_NAME
    manifest_path = bundle / MANIFEST_NAME
    if password_file is None:
        password_path = bundle / PASSWORD_NAME
    else:
        password_path = Path(os.fspath(password_file)).expanduser()
    try:
        if not bundle.is_dir():
            raise errors.DeliveryError(errors.VERIFY_FAILED, f"bundle directory not found: {bundle}")
        if not archive_path.is_file() or not manifest_path.is_file() or not password_path.is_file():
            raise errors.DeliveryError(errors.VERIFY_FAILED, "bundle is missing required files")
        password = password_path.read_text(encoding="utf-8").rstrip("\r\n")
        manifest_raw = manifest_path.read_text(encoding="utf-8")
    except errors.DeliveryError:
        raise
    except UnicodeError:
        raise errors.DeliveryError(
            errors.VERIFY_FAILED, "bundle password or manifest is not valid UTF-8") from None
    except OSError as exc:
        raise errors.DeliveryError(
            errors.VERIFY_FAILED, f"failed to read bundle: {exc.strerror or exc}") from None

    try:
        manifest = json.loads(manifest_raw)
    except ValueError:
        raise errors.DeliveryError(errors.VERIFY_FAILED, "manifest is not valid JSON") from None
    files = _validate_manifest(manifest)

    _check_encrypted_archive(pyzipper, archive_path, password, files)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified",
        "bundle_dir": str(bundle),
        "archive_path": str(archive_path),
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "archive_sha256": _hash_file(archive_path),
    }
