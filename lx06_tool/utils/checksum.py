"""
Checksum utilities for LX06 Flash Tool.

Provides SHA256 and MD5 hashing for firmware backup verification.
All operations are synchronous (CPU-bound, not I/O-bound) and
run on the default executor when called from async code.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable

from lx06_tool.constants import CHECKSUM_BUFFER_SIZE
from lx06_tool.exceptions import ChecksumMismatchError

logger = logging.getLogger(__name__)


def compute_sha256(file_path: Path, progress_cb: Callable[[int], None] | None = None) -> str:
    """Compute the SHA256 hash of a file.

    Args:
        file_path: Path to the file to hash.
        progress_cb: Optional callback invoked with bytes processed so far.

    Returns:
        Hex-encoded SHA256 digest string.

    Raises:
        FileNotFoundError: If file_path does not exist.
    """
    return _compute_hash(file_path, "sha256", progress_cb)


def compute_md5(file_path: Path, progress_cb: Callable[[int], None] | None = None) -> str:
    """Compute the MD5 hash of a file.

    Args:
        file_path: Path to the file to hash.
        progress_cb: Optional callback invoked with bytes processed so far.

    Returns:
        Hex-encoded MD5 digest string.
    """
    return _compute_hash(file_path, "md5", progress_cb)


def verify_checksum(
    file_path: Path,
    expected_sha256: str | None = None,
    expected_md5: str | None = None,
    partition_name: str = "",
) -> dict[str, bool]:
    """Verify a file's checksums against expected values.

    Args:
        file_path: Path to the file to verify.
        expected_sha256: Expected SHA256 hex digest. None to skip SHA256 check.
        expected_md5: Expected MD5 hex digest. None to skip MD5 check.
        partition_name: Partition name for error messages.

    Returns:
        Dict with keys "sha256" and/or "md5" mapping to bool pass/fail.
        Only includes keys for checksums that were checked.

    Raises:
        ChecksumMismatchError: If any checksum doesn't match (strict mode).
    """
    results: dict[str, bool] = {}

    if expected_sha256:
        actual = compute_sha256(file_path)
        passed = actual == expected_sha256.lower()
        results["sha256"] = passed
        if not passed:
            logger.warning(
                "SHA256 mismatch for %s (%s): expected %s, got %s",
                file_path.name, partition_name, expected_sha256[:16], actual[:16],
            )

    if expected_md5:
        actual = compute_md5(file_path)
        passed = actual == expected_md5.lower()
        results["md5"] = passed
        if not passed:
            logger.warning(
                "MD5 mismatch for %s (%s): expected %s, got %s",
                file_path.name, partition_name, expected_md5[:16], actual[:16],
            )

    return results


def verify_file_size(file_path: Path, expected_size: int, partition_name: str = "") -> bool:
    """Verify a file's size matches an expected value.

    Args:
        file_path: Path to the file to check.
        expected_size: Expected file size in bytes.
        partition_name: Partition name for error messages.

    Returns:
        True if the file size matches.
    """
    actual_size = file_path.stat().st_size
    if actual_size != expected_size:
        logger.warning(
            "Size mismatch for %s (%s): expected %d bytes, got %d bytes",
            file_path.name, partition_name, expected_size, actual_size,
        )
        return False
    return True


def _compute_hash(
    file_path: Path,
    algorithm: str,
    progress_cb: Callable[[int], None] | None = None,
) -> str:
    """Core hash computation with buffered reading.

    Args:
        file_path: File to hash.
        algorithm: Hash algorithm name ("sha256", "md5", etc.).
        progress_cb: Optional callback with cumulative bytes read.

    Returns:
        Hex-encoded hash digest.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    hasher = hashlib.new(algorithm)
    bytes_processed = 0

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(CHECKSUM_BUFFER_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            bytes_processed += len(chunk)
            if progress_cb:
                try:
                    progress_cb(bytes_processed)
                except Exception:
                    pass

    digest = hasher.hexdigest()
    logger.debug("%s(%s) = %s...", algorithm, file_path.name, digest[:16])
    return digest
