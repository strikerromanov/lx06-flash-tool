"""
lx06_tool/utils/checksum.py
----------------------------
Async-friendly file hashing (SHA256 + MD5) with progress reporting.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable


_CHUNK = 1024 * 1024  # 1 MB read chunks


@dataclass
class FileChecksums:
    path: Path
    sha256: str
    md5: str
    size_bytes: int


async def hash_file(
    path: Path,
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> FileChecksums:
    """
    Compute SHA-256 and MD5 of a file asynchronously (runs in executor
    to avoid blocking the event loop on large partition dumps).

    Parameters
    ----------
    on_progress : Called with (bytes_read, total_bytes) periodically.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _hash_sync, path, on_progress)


def _hash_sync(
    path: Path,
    on_progress: Optional[Callable[[int, int], None]],
) -> FileChecksums:
    sha256 = hashlib.sha256()
    md5    = hashlib.md5()
    total  = path.stat().st_size
    read   = 0

    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            sha256.update(chunk)
            md5.update(chunk)
            read += len(chunk)
            if on_progress:
                on_progress(read, total)

    return FileChecksums(
        path=path,
        sha256=sha256.hexdigest(),
        md5=md5.hexdigest(),
        size_bytes=total,
    )


async def verify_file(
    path: Path,
    expected_sha256: str,
    *,
    expected_md5: Optional[str] = None,
) -> bool:
    """
    Hash a file and compare against expected checksums.
    Returns True if all provided checksums match.
    """
    checksums = await hash_file(path)
    sha_ok = checksums.sha256.lower() == expected_sha256.lower()
    md5_ok = (
        checksums.md5.lower() == expected_md5.lower()
        if expected_md5 else True
    )
    return sha_ok and md5_ok


def write_checksum_file(checksums: FileChecksums, dest: Path) -> None:
    """Write a <sha256>  <filename> style checksum file alongside the dump."""
    dest.write_text(
        f"SHA256: {checksums.sha256}  {checksums.path.name}\n"
        f"MD5:    {checksums.md5}  {checksums.path.name}\n",
        encoding="utf-8",
    )
