"""
lx06_tool/modules/backup.py
----------------------------
Phase 2: NAND partition dump + checksum verification.

Dumps all 7 LX06 partitions sequentially using direct NAND→host
transfer (update mread store <label> normal <file>), then verifies
each dump with SHA-256 + MD5 before allowing the pipeline to
advance to Phase 3.

The old two-step RAM approach (store read.part → mread mem at
0x03000000) has been removed — it caused heap corruption on AXG.
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Callable, Optional

from lx06_tool.config import BackupSet, PartitionBackup
from lx06_tool.constants import (
    DEFAULT_PARTITION_TIMEOUT,
    MIN_PARTITION_DUMP_RATIO,
    PARTITION_MAP,
    PARTITION_TIMEOUTS,
)
from lx06_tool.exceptions import (
    BackupIncompleteError,
    ChecksumMismatchError,
    PartitionDumpError,
)
from lx06_tool.utils.amlogic import AmlogicTool
from lx06_tool.utils.checksum import FileChecksums, hash_file, write_checksum_file

logger = logging.getLogger(__name__)


# ─── Partition Dump ───────────────────────────────────────────────────────────

async def dump_partition(
    tool: AmlogicTool,
    mtd_name: str,
    output_path: Path,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    sudo_password: str = "",
) -> PartitionBackup:
    """
    Dump a single MTD partition to `output_path`.

    Uses direct NAND→host transfer (update mread store <label> normal <file>).
    Per-partition timeouts are resolved from PARTITION_TIMEOUTS constants.

    Returns a PartitionBackup with size populated (not yet checksummed).
    Raises PartitionDumpError on failure.
    """
    meta = PARTITION_MAP.get(mtd_name)
    if meta is None:
        raise PartitionDumpError(mtd_name, f"Unknown partition '{mtd_name}'")

    label         = str(meta["label"])
    expected_size = int(meta["size"])  # type: ignore[arg-type]

    # Resolve per-partition timeout
    timeout = PARTITION_TIMEOUTS.get(label, DEFAULT_PARTITION_TIMEOUT)
    logger.info(
        "[BACKUP] Dumping '%s' (%s) — size=%d, timeout=%ds",
        mtd_name, label, expected_size, timeout,
    )

    try:
        await tool.mread(
            partition=label,
            output_path=output_path,
            size=expected_size,
            timeout=timeout,
            on_progress=on_progress,
            sudo_password=sudo_password,
        )
    except Exception as exc:
        raise PartitionDumpError(mtd_name, str(exc)) from exc

    if not output_path.exists():
        raise PartitionDumpError(mtd_name, "Output file was not created")

    actual_size = output_path.stat().st_size
    if actual_size < expected_size * MIN_PARTITION_DUMP_RATIO:
        raise PartitionDumpError(
            mtd_name,
            f"Dump is suspiciously small: {actual_size} bytes "
            f"(expected ≥ {int(expected_size * MIN_PARTITION_DUMP_RATIO)})",
        )

    return PartitionBackup(
        name=mtd_name,
        label=label,
        path=output_path,
        size_bytes=actual_size,
        expected_size=expected_size,
    )


async def dump_all_partitions(
    tool: AmlogicTool,
    backup_dir: Path,
    *,
    on_partition_start: Optional[Callable[[str, str], None]] = None,
    on_partition_done:  Optional[Callable[[PartitionBackup], None]] = None,
    on_line:            Optional[Callable[[str], None]] = None,
    on_partition_skip:  Optional[Callable[[str, str], None]] = None,
    sudo_password: str = "",
) -> BackupSet:
    """
    Dump all MTD partitions (mtd0 through mtd6) to `backup_dir`.

    Uses direct NAND→host transfer for each partition.
    Per-partition timeouts are configured in constants.PARTITION_TIMEOUTS.
    Large partitions (system0/system1) get 10-minute timeouts to
    accommodate slow USB 2.0 bulk transfers.

    Parameters
    ----------
    on_partition_start : Called with (mtd_name, label) at the start of each dump.
    on_partition_done  : Called with the completed PartitionBackup.
    on_line            : Called with each output line from the tool.
    on_partition_skip  : Called with (mtd_name, reason) when a partition is skipped.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    backup_set = BackupSet(
        timestamp=timestamp,
        backup_dir=backup_dir,
    )

    for mtd_name, meta in PARTITION_MAP.items():
        label  = str(meta["label"])
        output = backup_dir / f"{mtd_name}_{label}.img"

        if on_partition_start:
            on_partition_start(mtd_name, label)

        try:
            part = await dump_partition(
                tool, mtd_name, output, on_progress=on_line,
                sudo_password=sudo_password,
            )
            backup_set.partitions[mtd_name] = part

            if on_partition_done:
                on_partition_done(part)
        except (PartitionDumpError, Exception) as exc:
            reason = str(exc)
            # Log the failure but continue with remaining partitions
            logger.warning(
                "Skipping partition %s (%s): %s", mtd_name, label, reason
            )
            if on_partition_skip:
                on_partition_skip(mtd_name, reason)
    return backup_set


# ─── Checksum Verification ────────────────────────────────────────────────────

async def compute_checksums(
    backup_set: BackupSet,
    *,
    on_partition: Optional[Callable[[str, FileChecksums], None]] = None,
) -> None:
    """
    Compute SHA-256 + MD5 for every partition in `backup_set`.
    Writes a sidecar `.sha256` file next to each dump.
    Mutates each PartitionBackup in-place.
    """
    for mtd_name, part in backup_set.partitions.items():
        if part.path is None or not part.path.exists():
            raise PartitionDumpError(mtd_name, "Dump file missing before checksum")

        checksums = await hash_file(part.path)
        part.sha256 = checksums.sha256
        part.md5    = checksums.md5

        write_checksum_file(checksums, part.path.with_suffix(".sha256"))

        if on_partition:
            on_partition(mtd_name, checksums)


async def verify_backup(backup_set: BackupSet) -> None:
    """
    Verify all partition dumps in `backup_set` by re-hashing and comparing.
    Raises ChecksumMismatchError or BackupIncompleteError on failure.
    """
    if not backup_set.partitions:
        raise BackupIncompleteError("Backup set is empty — nothing to verify.")

    for mtd_name, part in backup_set.partitions.items():
        if not part.sha256:
            raise BackupIncompleteError(
                f"No checksum recorded for '{mtd_name}'. Run compute_checksums first."
            )
        if part.path is None or not part.path.exists():
            raise BackupIncompleteError(
                f"Dump file missing for '{mtd_name}': {part.path}"
            )

        live = await hash_file(part.path)

        if live.sha256 != part.sha256:
            raise ChecksumMismatchError(
                str(part.path), expected=part.sha256, actual=live.sha256
            )
        if part.md5 and live.md5 != part.md5:
            raise ChecksumMismatchError(
                str(part.path), expected=part.md5, actual=live.md5
            )
        if not part.size_ok:
            raise BackupIncompleteError(
                f"Partition '{mtd_name}' dump is smaller than expected "
                f"({part.size_bytes} vs {part.expected_size} bytes)."
            )

        part.verified = True

    backup_set.all_verified = all(p.verified for p in backup_set.partitions.values())
