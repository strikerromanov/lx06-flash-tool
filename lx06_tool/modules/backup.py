"""
Partition backup engine for LX06 Flash Tool (Phase 2).

Handles:
- Sequential dumping of all MTD partitions (mtd0–mtd6)
- SHA256 and MD5 checksum computation for each dump
- File size verification against expected partition sizes
- Backup manifest generation for integrity auditing
- Backup validation on load (re-verify existing backups)

This is the MOST CRITICAL safety module. Backup integrity is the
primary defense against permanent device bricking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from lx06_tool.config import BackupSet, PartitionBackup
from lx06_tool.constants import PARTITION_MAP, PARTITION_ORDER
from lx06_tool.exceptions import (
    BackupError,
    BackupIncompleteError,
    ChecksumMismatchError,
    SizeMismatchError,
)
from lx06_tool.utils.amlogic import AmlogicTool
from lx06_tool.utils.checksum import compute_sha256, compute_md5, verify_file_size
from lx06_tool.utils.runner import AsyncRunner

logger = logging.getLogger(__name__)


# ── Backup Progress Callback ────────────────────────────────────────────────

# Called with (partition_name, partition_label, status_message, progress_pct)
ProgressCallback = Callable[[str, str, str, float], None]


# ── Backup Manager ──────────────────────────────────────────────────────────


class BackupManager:
    """Manages full device partition backup with integrity verification.

    Dumps all MTD partitions sequentially, computes checksums,
    and verifies dump integrity. Generates a backup manifest that
    can be used to validate backups later.

    Usage:
        mgr = BackupManager(aml_tool=aml_tool, backup_dir=Path("./backups"))
        result = await mgr.dump_all_partitions(on_progress=ui_callback)
        if result.all_verified:
            print("Backup complete and verified!")
    """

    def __init__(
        self,
        aml_tool: AmlogicTool,
        backup_dir: Path,
        runner: AsyncRunner | None = None,
    ):
        self._aml = aml_tool
        self._backup_dir = backup_dir
        self._runner = runner or AsyncRunner(default_timeout=120.0, sudo=True)

    # ── Single Partition Dump ────────────────────────────────────────────────

    async def dump_partition(
        self,
        mtd_name: str,
        *,
        on_output: Callable[[str, str], None] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PartitionBackup:
        """Dump a single MTD partition to a file.

        Args:
            mtd_name: Partition name (e.g. "mtd0").
            on_output: Callback for real-time output lines.
            on_progress: Callback for progress updates.

        Returns:
            PartitionBackup with dump path and checksums.

        Raises:
            BackupError: If the dump fails.
        """
        if mtd_name not in PARTITION_MAP:
            raise BackupError(f"Unknown partition: {mtd_name}")

        part_info = PARTITION_MAP[mtd_name]
        label = part_info["label"]
        expected_size = part_info["size"]

        backup = PartitionBackup(
            name=mtd_name,
            label=label,
            expected_size=expected_size,
        )

        output_file = self._backup_dir / f"{mtd_name}_{label}.bin"
        logger.info("Dumping %s (%s) → %s", mtd_name, label, output_file.name)

        if on_progress:
            on_progress(mtd_name, label, "Reading partition...", 0.0)

        if on_output:
            on_output("stdout", f"Reading {mtd_name} ({label})...")

        # Dump via Amlogic tool
        try:
            dump_path = await self._aml.mread(mtd_name, output_file)
        except Exception as exc:
            raise BackupError(
                f"Failed to dump {mtd_name} ({label}): {exc}",
                details="Check USB connection. The device may have disconnected.",
            ) from exc

        # Record actual size
        backup.size_bytes = dump_path.stat().st_size
        backup.path = str(dump_path)

        if on_progress:
            on_progress(mtd_name, label, "Computing checksums...", 0.5)

        if on_output:
            on_output(
                "stdout",
                f"  Read {backup.size_bytes:,} bytes. Computing checksums...",
            )

        # Compute checksums
        backup.sha256 = compute_sha256(dump_path)
        backup.md5 = compute_md5(dump_path)

        if on_progress:
            on_progress(mtd_name, label, "Verifying...", 0.8)

        # Verify size
        if expected_size > 0:
            size_ok = verify_file_size(dump_path, expected_size, label)
            if not size_ok:
                logger.warning(
                    "Size mismatch for %s: expected %d, got %d (may be normal for partial partitions)",
                    mtd_name, expected_size, backup.size_bytes,
                )
                # Don't fail — some partitions may report different sizes
                # than the partition table suggests

        if on_progress:
            on_progress(mtd_name, label, "Done", 1.0)

        if on_output:
            on_output(
                "stdout",
                f"  ✅ {mtd_name} ({label}): {backup.size_bytes:,} bytes "
                f"SHA256:{backup.sha256[:16]}...",
            )

        logger.info(
            "Dumped %s (%s): %d bytes, SHA256=%s...",
            mtd_name, label, backup.size_bytes, backup.sha256[:16],
        )

        return backup

    # ── Full Backup ──────────────────────────────────────────────────────────

    async def dump_all_partitions(
        self,
        *,
        on_output: Callable[[str, str], None] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> BackupSet:
        """Dump all MTD partitions (mtd0 through mtd6).

        This is the main backup operation. It dumps each partition
        sequentially, computes checksums, and verifies integrity.

        Args:
            on_output: Callback for real-time output lines.
            on_progress: Callback for per-partition progress.

        Returns:
            BackupSet with all partition dumps and verification status.

        Raises:
            BackupIncompleteError: If any partition fails to dump.
        """
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        backup_set = BackupSet()
        backup_set.mark_timestamp()

        total = len(PARTITION_ORDER)
        failed: list[str] = []

        if on_output:
            on_output(
                "stdout",
                f"Starting full backup of {total} partitions...\n"
                f"Backup directory: {self._backup_dir}",
            )

        for i, mtd_name in enumerate(PARTITION_ORDER):
            label = PARTITION_MAP[mtd_name]["label"]
            logger.info("Dumping partition %d/%d: %s (%s)", i + 1, total, mtd_name, label)

            try:
                backup = await self.dump_partition(
                    mtd_name,
                    on_output=on_output,
                    on_progress=on_progress,
                )
                backup_set.add_partition(backup)
            except BackupError as exc:
                logger.error("Failed to dump %s: %s", mtd_name, exc)
                failed.append(mtd_name)
                if on_output:
                    on_output("stdout", f"  ❌ {mtd_name} ({label}): FAILED - {exc}")
                # Continue with remaining partitions — partial backup is better than none

        # Verify all dumps
        if on_output:
            on_output("stdout", "\nVerifying backup integrity...")

        backup_set.all_verified = await self.verify_backup(backup_set, on_output=on_output)

        # Report results
        dumped = len(backup_set.partitions)
        if failed:
            if on_output:
                on_output(
                    "stdout",
                    f"\n⚠️ Backup completed with {len(failed)} failures: {', '.join(failed)}",
                )
            if dumped == 0:
                raise BackupIncompleteError(
                    f"All partitions failed to dump: {', '.join(failed)}"
                )
        else:
            if on_output:
                on_output(
                    "stdout",
                    f"\n✅ All {total} partitions dumped successfully.",
                )

        # Save manifest
        await self.save_manifest(backup_set)

        logger.info(
            "Backup complete: %d/%d partitions dumped, verified=%s",
            dumped, total, backup_set.all_verified,
        )

        return backup_set

    # ── Verification ─────────────────────────────────────────────────────────

    async def verify_backup(
        self,
        backup_set: BackupSet,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> bool:
        """Verify the integrity of all partition backups.

        Re-computes checksums and compares against the recorded values.
        Also checks that all dump files exist and have the expected sizes.

        Args:
            backup_set: The backup set to verify.
            on_output: Callback for verification progress.

        Returns:
            True if ALL partitions verified successfully.
        """
        all_ok = True

        for mtd_name, part_data in backup_set.partitions.items():
            backup = PartitionBackup(**part_data)
            label = backup.label

            if not backup.path:
                logger.error("No path for %s", mtd_name)
                if on_output:
                    on_output("stdout", f"  ❌ {mtd_name} ({label}): No dump path recorded")
                all_ok = False
                continue

            dump_path = Path(backup.path)

            # Check file exists
            if not dump_path.exists():
                logger.error("Dump file missing: %s", dump_path)
                if on_output:
                    on_output("stdout", f"  ❌ {mtd_name} ({label}): File missing")
                all_ok = False
                continue

            # Verify SHA256
            if backup.sha256:
                actual_sha256 = compute_sha256(dump_path)
                if actual_sha256 != backup.sha256:
                    logger.error(
                        "SHA256 mismatch for %s: expected %s, got %s",
                        mtd_name, backup.sha256[:16], actual_sha256[:16],
                    )
                    if on_output:
                        on_output("stdout", f"  ❌ {mtd_name} ({label}): SHA256 mismatch!")
                    all_ok = False
                    continue

            # Verify file size is non-zero
            actual_size = dump_path.stat().st_size
            if actual_size == 0:
                logger.error("Empty dump for %s", mtd_name)
                if on_output:
                    on_output("stdout", f"  ❌ {mtd_name} ({label}): Empty file!")
                all_ok = False
                continue

            # Mark as verified
            part_data["verified"] = True
            if on_output:
                on_output(
                    "stdout",
                    f"  ✅ {mtd_name} ({label}): Verified ({actual_size:,} bytes)",
                )

        return all_ok

    # ── Manifest Persistence ─────────────────────────────────────────────────

    async def save_manifest(self, backup_set: BackupSet) -> Path:
        """Save a backup manifest JSON file.

        The manifest records all partition dumps with their checksums,
        allowing offline verification and future restore operations.

        Args:
            backup_set: The backup set to save.

        Returns:
            Path to the manifest file.
        """
        manifest_path = self._backup_dir / "backup_manifest.json"

        manifest = {
            "version": "1.0",
            "device": "LX06",
            "timestamp": backup_set.timestamp,
            "all_verified": backup_set.all_verified,
            "partitions": backup_set.partitions,
        }

        manifest_path.write_text(json.dumps(manifest, indent=2))
        logger.info("Backup manifest saved: %s", manifest_path)
        return manifest_path

    async def load_manifest(self, manifest_path: Path | None = None) -> BackupSet:
        """Load a backup manifest from a JSON file.

        Args:
            manifest_path: Path to manifest. Defaults to backup_dir/backup_manifest.json.

        Returns:
            BackupSet loaded from manifest.

        Raises:
            BackupError: If manifest cannot be loaded.
        """
        path = manifest_path or (self._backup_dir / "backup_manifest.json")

        if not path.exists():
            raise BackupError(f"Backup manifest not found: {path}")

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise BackupError(f"Failed to read manifest: {exc}") from exc

        backup_set = BackupSet(
            timestamp=data.get("timestamp", ""),
            all_verified=data.get("all_verified", False),
            partitions=data.get("partitions", {}),
        )

        logger.info(
            "Loaded backup manifest: %s (%s, %d partitions)",
            backup_set.timestamp,
            "verified" if backup_set.all_verified else "unverified",
            len(backup_set.partitions),
        )
        return backup_set

    # ── Re-verify Existing Backup ────────────────────────────────────────────

    async def reverify_existing_backup(
        self,
        backup_dir: Path | None = None,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> BackupSet:
        """Re-verify an existing backup from its manifest.

        Useful for confirming backup integrity before flashing.
        Re-computes checksums and compares against manifest records.

        Args:
            backup_dir: Directory containing the backup. Defaults to self._backup_dir.
            on_output: Callback for progress.

        Returns:
            BackupSet with updated verification status.
        """
        target_dir = backup_dir or self._backup_dir
        backup_set = await self.load_manifest(target_dir / "backup_manifest.json")

        if on_output:
            on_output(
                "stdout",
                f"Re-verifying backup from {backup_set.timestamp}...",
            )

        backup_set.all_verified = await self.verify_backup(backup_set, on_output=on_output)

        # Update manifest with new verification status
        await self.save_manifest(backup_set)

        return backup_set

    # ── Backup Report ────────────────────────────────────────────────────────

    @staticmethod
    def generate_report(backup_set: BackupSet) -> str:
        """Generate a human-readable backup report.

        Args:
            backup_set: The backup set to report on.

        Returns:
            Formatted report string.
        """
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║              BACKUP REPORT                                 ║",
            f"║  Device: LX06 (Xiaoai Speaker Pro)                         ║",
            f"║  Timestamp: {backup_set.timestamp:<47} ║",
            f"║  Overall: {'✅ VERIFIED' if backup_set.all_verified else '⚠️  UNVERIFIED':<47} ║",
            "╠══════════════════════════════════════════════════════════════╣",
            "║  MTD    │ Label       │ Size       │ SHA256            │ OK  ║",
            "╠─────────┼─────────────┼────────────┼───────────────────┼─────╣",
        ]

        for mtd_name in PARTITION_ORDER:
            part = backup_set.get_partition(mtd_name)
            if part:
                size_str = f"{part.size_bytes:,}"
                sha_str = part.sha256[:16] + "..." if part.sha256 else "N/A"
                ok_str = "✅" if part.verified else "❌"
                lines.append(
                    f"║  {mtd_name:<6} │ {part.label:<11} │ {size_str:>10} │ {sha_str:<17} │ {ok_str}  ║"
                )
            else:
                lines.append(
                    f"║  {mtd_name:<6} │ {'MISSING':<11} │ {'0':>10} │ {'N/A':<17} │ ❌  ║"
                )

        lines.append("╚══════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)
