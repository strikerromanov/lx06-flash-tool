"""
lx06_tool/modules/backup.py
----------------------------
Phase 2: NAND partition dump + checksum verification.

Dumps ALL 7 LX06 partitions per the official xiaoai-patch guide:
https://github.com/duhow/xiaoai-patch/blob/master/research/lx06/install.md

Official backup commands:
    update.exe mread store bootloader normal 0x200000 mtd0.img
    update.exe mread store tpl normal 0x800000 mtd1.img
    update.exe mread store boot0 normal 0x600000 mtd2.img
    update.exe mread store boot1 normal 0x600000 mtd3.img
    update.exe mread store system0 normal 0x2820000 mtd4.img
    update.exe mread store system1 normal 0x2800000 mtd5.img
    update.exe mread store data normal 0x13e0000 mtd6.img

Individual partition failures are non-fatal — the backup continues
and logs which partitions succeeded/failed.
"""
from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from pathlib import Path

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
    on_progress: Callable[[str], None] | None = None,
    sudo_password: str = "",
) -> PartitionBackup:
    """
    Dump a single MTD partition to `output_path`.

    Uses official syntax: update mread store <label> normal <size> <file>

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
        "[BACKUP] Dumping '%s' (%s) — size=0x%X (%d bytes), timeout=%ds",
        mtd_name, label, expected_size, expected_size, timeout,
    )

    try:
        await tool.dump_partition(
            partition_label=label,
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
            f"(expected >= {int(expected_size * MIN_PARTITION_DUMP_RATIO)})",
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
    on_partition_start: Callable[[str, str], None] | None = None,
    on_partition_done:  Callable[[PartitionBackup], None] | None = None,
    on_line:            Callable[[str], None] | None = None,
    on_partition_skip:  Callable[[str, str], None] | None = None,
    sudo_password: str = "",
) -> BackupSet:
    """
    Dump ALL 7 MTD partitions per the official xiaoai-patch guide.

    Official commands:
        mread store bootloader normal 0x200000 mtd0.img
        mread store tpl        normal 0x800000 mtd1.img
        mread store boot0      normal 0x600000 mtd2.img
        mread store boot1      normal 0x600000 mtd3.img
        mread store system0    normal 0x2820000 mtd4.img
        mread store system1    normal 0x2800000 mtd5.img
        mread store data       normal 0x13e0000 mtd6.img

    Individual failures are non-fatal — backup continues and logs
    which partitions succeeded/failed.
    """
    import asyncio

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    backup_set = BackupSet(
        timestamp=timestamp,
        backup_dir=backup_dir,
    )

    for idx, (mtd_name, meta) in enumerate(PARTITION_MAP.items()):
        label  = str(meta["label"])
        output = backup_dir / "{}_{}.img".format(mtd_name, label)

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

            # Add delay between partitions to let device stabilize
            if idx < len(PARTITION_MAP) - 1:
                await asyncio.sleep(2)

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
    on_partition: Callable[[str, FileChecksums], None] | None = None,
) -> None:
    """Compute SHA-256 + MD5 checksums for each dumped partition."""
    for mtd_name, pbackup in backup_set.partitions.items():
        checksums = await hash_file(pbackup.path)
        pbackup.sha256 = checksums.sha256
        pbackup.md5 = checksums.md5
        if on_partition:
            on_partition(mtd_name, checksums)
        logger.info(
            "[BACKUP] %s checksums: sha256=%s... md5=%s...",
            mtd_name, checksums.sha256[:16], checksums.md5[:16],
        )


async def write_checksum_manifest(backup_set: BackupSet) -> Path:
    """Write a checksum manifest file alongside the backup."""
    manifest_path = backup_set.backup_dir / "checksums.sha256"
    lines: list[str] = []
    for mtd_name, pbackup in sorted(backup_set.partitions.items()):
        if pbackup.sha256:
            lines.append(f"{pbackup.sha256}  {pbackup.path.name}")
    manifest_path.write_text("\n".join(lines) + "\n")
    return manifest_path


async def verify_backup(backup_set: BackupSet) -> None:
    """
    Re-verify all checksums in the backup set.

    Raises ChecksumMismatchError if any file has been modified since
    the checksums were computed.
    """
    for mtd_name, pbackup in backup_set.partitions.items():
        if not pbackup.sha256 or not pbackup.path.exists():
            continue
        actual = await hash_file(pbackup.path)
        if actual.sha256 != pbackup.sha256:
            raise ChecksumMismatchError(
                str(pbackup.path),
                expected=pbackup.sha256,
                actual=actual.sha256,
            )
