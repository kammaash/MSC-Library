"""
File storage for SmartLib 360: local disk (offline) or Cloudflare R2 (cloud).

Every uploaded file (PDFs, covers, QR codes, barcodes, backups, ...) is
identified by a *key* such as ``uploads/pdfs/abc.pdf`` — always relative,
always forward slashes. The same key is what the database stores, and it maps
to a file under BASE_DIR in local mode or to an object in the bucket in R2
mode, so rows keep working when the app moves between the two.

Legacy rows written on Windows contain backslashes (``uploads\\pdfs\\x.pdf``);
normalize_key() converts those, and every method below runs its key through it.
"""
import errno
import os
import shutil
from typing import Mapping
from urllib.parse import quote

R2_ENV_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")


# Win32 maps these names to devices (with or without an extension, any case).
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
_WINDOWS_BAD_CHARS = frozenset('~:*?"<>|')


def _is_aliasable_segment(part: str) -> bool:
    """True if Windows could resolve this segment to a different name."""
    if part[-1] in ". ":
        return True
    if any(ch in _WINDOWS_BAD_CHARS or ord(ch) < 32 or ord(ch) == 127 for ch in part):
        return True
    return part.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES


def normalize_key(key: str) -> str:
    """Return the canonical form of a storage key.

    Backslashes become '/', leading '/' is dropped. Non-string input, an empty
    result, a trailing '/', and any empty, '.' or '..' segment (so "a//b",
    "a/./b", "a/../b") are rejected: a key must name one file and can never
    point outside the storage root. Segments Windows would resolve to a
    different name are rejected too (a trailing '.' or space, '~' 8.3 short
    names, ':' alternate data streams, wildcard/control characters, reserved
    device names like NUL or COM1), because a string check on the key
    ("backups.") would then disagree with the file the OS opens ("backups").
    """
    if not isinstance(key, str):
        raise ValueError(f"invalid storage key: {key!r}")
    cleaned = key.replace("\\", "/").lstrip("/")
    parts = cleaned.split("/")
    if not cleaned or any(part in ("", ".", "..") or _is_aliasable_segment(part) for part in parts):
        raise ValueError(f"invalid storage key: {key!r}")
    return cleaned


def _normalize_prefix(prefix: str) -> str:
    # Like normalize_key, but an empty prefix is legal ("everything") and a
    # trailing '/' is meaningful, so it is kept.
    cleaned = str(prefix or "").replace("\\", "/").lstrip("/")
    if any(part == ".." for part in cleaned.split("/")):
        raise ValueError(f"invalid storage prefix: {prefix!r}")
    return cleaned


class LocalStorage:
    """Files live on disk under base_dir (the original offline behaviour)."""

    remote = False

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)

    def local_path(self, key: str) -> str:
        """Absolute path for key. Raises ValueError if it would leave base_dir."""
        key = normalize_key(key)
        base = os.path.realpath(self.base_dir)
        path = os.path.realpath(os.path.join(base, *key.split("/")))
        # normalize_key already blocks '..'; this also catches symlinks and
        # anything else that resolves outside the base directory.
        if os.path.commonpath([base, path]) != base:
            raise ValueError(f"storage key escapes base directory: {key!r}")
        # The resolved segments must be the key's own segments. A symlink or an
        # OS-level alias (Windows short names, trailing dots) that points at a
        # different name would let a caller's string check on the key ("not
        # backups/") disagree with the file actually opened. Case is ignored:
        # case-insensitive filesystems keep the caller's spelling anyway.
        resolved = os.path.relpath(path, base).replace(os.sep, "/").split("/")
        if [p.lower() for p in resolved] != [p.lower() for p in key.split("/")]:
            raise ValueError(f"storage key resolves to a different path: {key!r}")
        return path

    def save_file(self, src_path: str, key: str, content_type: str | None = None,
                  move: bool = False) -> str:
        key = normalize_key(key)
        dest = self.local_path(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if move:
            # Opt-in, for callers whose source is a throw-away temp file (a
            # 400 MB video upload): renaming it into place means the disk
            # never holds the file twice. os.replace only works inside one
            # filesystem; when the temp dir is on another drive it raises
            # EXDEV and we fall through to the copy below. After a move the
            # source no longer exists, so a caller that still needs it, or
            # that removes it afterwards, must handle that.
            try:
                os.replace(src_path, dest)
                return key
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
        # Default: copy, never move; the caller's source file is left untouched
        # (migrate_to_cloud.py relies on this for the PC's uploads folder).
        shutil.copyfile(src_path, dest)
        if move:
            # Cross-device "move": the copy succeeded, so drop the source too.
            try:
                os.remove(src_path)
            except OSError:
                pass
        return key

    def save_bytes(self, data: bytes, key: str, content_type: str | None = None) -> str:
        key = normalize_key(key)
        dest = self.local_path(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return key

    def delete(self, key: str | None) -> None:
        # Deleting something that is already gone (or a bad key) must never
        # break the request that triggered the cleanup.
        if not key:
            return
        try:
            os.remove(self.local_path(key))
        except Exception:
            pass

    def list_keys(self, prefix: str) -> list[str]:
        prefix = _normalize_prefix(prefix)
        base = os.path.realpath(self.base_dir)
        # Only walk the directory the prefix points into, not all of base_dir.
        start = os.path.join(base, *prefix.rsplit("/", 1)[0].split("/")) if "/" in prefix else base
        keys = []
        for root, _dirs, files in os.walk(start):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), base).replace(os.sep, "/")
                if rel.startswith(prefix):
                    keys.append(rel)
        return sorted(keys)


class R2Storage:
    """Files live in a Cloudflare R2 bucket, reached through boto3's S3 API."""

    remote = True

    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def save_file(self, src_path: str, key: str, content_type: str | None = None,
                  move: bool = False) -> str:
        # `move` exists only so both classes share one interface; an upload to
        # the bucket always leaves the source file where it was, so it is ignored.
        key = normalize_key(key)
        extra = {"ContentType": content_type} if content_type else None
        self.client.upload_file(src_path, self.bucket, key, ExtraArgs=extra)
        return key

    def save_bytes(self, data: bytes, key: str, content_type: str | None = None) -> str:
        key = normalize_key(key)
        kwargs = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **kwargs)
        return key

    def delete(self, key: str | None) -> None:
        if not key:
            return
        try:
            self.client.delete_object(Bucket=self.bucket, Key=normalize_key(key))
        except Exception:
            pass

    def list_keys(self, prefix: str) -> list[str]:
        prefix = _normalize_prefix(prefix)
        # list_objects_v2 returns at most 1000 keys per call; the paginator
        # follows the continuation tokens for us.
        paginator = self.client.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)

    def presigned_url(self, key: str, expires_in: int, content_type: str | None = None,
                      download_name: str | None = None) -> str:
        """Time-limited GET link straight to R2.

        The browser downloads from R2 directly, so the response headers are
        set here: 'inline' lets PDFs/media open in the page, while a
        download_name forces a save dialog with that filename.
        """
        key = normalize_key(key)
        if download_name is None:
            disposition = "inline"
        else:
            # Book titles can be Telugu/non-ASCII, and HTTP header values are
            # ASCII. RFC 6266: an ASCII fallback in filename= for old clients,
            # plus the real name percent-encoded in filename*= (RFC 5987).
            fallback = "".join(
                "_" if ord(c) > 126 or ord(c) < 32 else c
                for c in download_name if c not in '"\\\r\n'
            )
            disposition = (f'attachment; filename="{fallback}"; '
                           f"filename*=UTF-8''{quote(download_name, safe='')}")
        params = {"Bucket": self.bucket, "Key": key, "ResponseContentDisposition": disposition}
        if content_type:
            params["ResponseContentType"] = content_type
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)


def get_storage(base_dir: str, env: Mapping[str, str] = os.environ) -> "LocalStorage | R2Storage":
    """Pick the backend from environment variables.

    R2 configuration is all-or-nothing: all four R2_* variables set means R2,
    none set means local disk, anything in between is a startup error (a
    half-configured deployment would otherwise silently write to the
    throwaway disk of a cloud host). Empty strings count as unset.
    """
    values = {name: (env.get(name) or "").strip() for name in R2_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if len(missing) == len(R2_ENV_VARS):
        return LocalStorage(base_dir)
    if missing:
        raise RuntimeError(
            "Incomplete Cloudflare R2 configuration: missing " + ", ".join(missing)
            + ". Set all of " + ", ".join(R2_ENV_VARS) + " to use R2, or none of them for local storage."
        )

    # Imported here so offline installs that never use R2 don't pay for boto3.
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{values['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=values["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            # Recent boto3 adds CRC32 checksum headers to every request by
            # default, which Cloudflare R2 has rejected. Only send/verify
            # checksums when an operation requires them.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return R2Storage(client, values["R2_BUCKET_NAME"])
