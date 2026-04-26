"""
Firmware unpack/repack orchestrator for LX06 Flash Tool (Phase 3).

Handles:
- Extracting system squashfs partition to a working directory
- Coordinating debloat, media suite, and AI brain modifications
- Repacking the modified rootfs back into squashfs
- Validating firmware integrity at each stage
- Managing the complete customization pipeline

This module is the central coordinator — it doesn't do the actual
modifications itself, but orchestrates debloat.py, media_suite.py,
and ai_brain.py to apply the user's selections.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lx06_tool.config import CustomizationChoices
from lx06_tool.exceptions import (
    FirmwareError,
    SquashFSError,
)
from lx06_tool.modules.ai_brain import AIBrainInstaller
from lx06_tool.modules.debloat import DebloatEngine

if TYPE_CHECKING:
    from lx06_tool.utils.amlogic import AmlogicTool
from lx06_tool.modules.docker_builder import DockerBuilder
from lx06_tool.modules.media_suite import MediaSuiteInstaller
from lx06_tool.utils.compat import AsyncRunner
from lx06_tool.utils.squashfs import SquashFSTool

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class FirmwarePaths:
    """Resolved paths for firmware extraction and modification."""

    # Input: raw partition dump
    system_dump: Path          # e.g. backups/mtd5_system0.bin
    boot_dump: Path | None     # e.g. backups/mtd3_boot0.bin

    # Working directories
    extract_dir: Path          # Where squashfs is extracted
    rootfs_dir: Path           # The actual rootfs inside extract_dir

    # Output
    output_dir: Path           # Where modified images go
    output_system: Path        # Final root.squashfs
    output_boot: Path | None   # Final boot.img (if modified)

    # A/B partition info
    active_slot: str = ""       # "system0" or "system1"
    target_slot: str = ""       # The inactive slot to flash to


@dataclass
class CustomizationResult:
    """Result of the complete firmware customization pipeline."""

    success: bool = False
    steps_completed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_system: Path | None = None
    output_boot: Path | None = None
    rootfs_size_before: int = 0
    rootfs_size_after: int = 0
    packages_added: list[str] = field(default_factory=list)
    packages_removed: list[str] = field(default_factory=list)


# ── Firmware Orchestrator ───────────────────────────────────────────────────


class FirmwareOrchestrator:
    """Orchestrates the complete firmware customization pipeline.

    Coordinates extraction, modification (debloat + media + AI),
    and repacking of the firmware squashfs image.

    Usage:
        orch = FirmwareOrchestrator(
            paths=paths,
            choices=choices,
            docker_builder=builder,
        )
        result = await orch.run_pipeline(on_output=ui_callback)
        if result.success:
            flash result.output_system to device
    """

    def __init__(
        self,
        paths: FirmwarePaths,
        choices: CustomizationChoices,
        runner: AsyncRunner | None = None,
        docker_builder: DockerBuilder | None = None,
    ):
        self._paths = paths
        self._choices = choices
        self._runner = runner or AsyncRunner(default_timeout=300.0, sudo=True)
        self._docker_builder = docker_builder or DockerBuilder(runner=self._runner)
        self._squashfs = SquashFSTool(runner=self._runner)

    # ── Pipeline Execution ───────────────────────────────────────────────────

    async def run_pipeline(
        self,
        *,
        on_output: Callable[[str, str], None] | None = None,
        on_step: Callable[[str, str], None] | None = None,
    ) -> CustomizationResult:
        """Execute the complete customization pipeline.

        Steps:
        1. Validate input files
        2. Extract squashfs
        3. Apply debloat
        4. Install media suite
        5. Install AI brain
        6. Repack squashfs
        7. Validate output

        Args:
            on_output: Callback for detailed output.
            on_step: Callback(step_name, status) for progress tracking.

        Returns:
            CustomizationResult with success status and details.
        """
        result = CustomizationResult()

        def step(name: str, status: str) -> None:
            if on_step:
                on_step(name, status)
            logger.info("Pipeline step: %s — %s", name, status)

        # Step 1: Validate inputs
        step("validate", "Validating input files...")
        if not self._paths.system_dump.exists():
            result.steps_failed.append("validate")
            raise FirmwareError(
                f"System dump not found: {self._paths.system_dump}",
                details="Ensure backup was completed successfully.",
            )
        result.steps_completed.append("validate")

        # Step 2: Extract squashfs
        step("extract", "Extracting squashfs...")
        if on_output:
            on_output("stdout", f"Extracting {self._paths.system_dump.name}...")

        try:
            self._paths.extract_dir.mkdir(parents=True, exist_ok=True)
            await self._squashfs.extract(
                self._paths.system_dump,
                self._paths.extract_dir,
            )
            result.rootfs_size_before = self._dir_size(self._paths.rootfs_dir)
            result.steps_completed.append("extract")
            if on_output:
                on_output("stdout", f"✅ Extracted to {self._paths.rootfs_dir}")
        except SquashFSError as exc:
            result.steps_failed.append("extract")
            raise FirmwareError(f"Extraction failed: {exc}") from exc

        # Step 3: Apply debloat
        if self._choices.remove_telemetry or self._choices.remove_auto_updater:
            step("debloat", "Removing bloatware...")
            try:
                engine = DebloatEngine(self._paths.rootfs_dir, runner=self._runner)
                debloat_result = await engine.apply(
                    choices=self._choices,
                    on_output=on_output,
                )
                result.packages_removed.extend(debloat_result.removed)
                result.warnings.extend(debloat_result.warnings)
                result.steps_completed.append("debloat")
                if on_output:
                    on_output(
                        "stdout",
                        f"✅ Debloat complete: removed {len(debloat_result.removed)} items",
                    )
            except Exception as exc:
                result.steps_failed.append("debloat")
                result.warnings.append(f"Debloat failed (non-fatal): {exc}")
                logger.warning("Debloat step failed (continuing): %s", exc)
        else:
            step("debloat", "Skipped (not selected)")

        # Step 4: Install media suite
        media_choices = [
            self._choices.install_airplay,
            self._choices.install_dlna,
            self._choices.install_spotify,
            self._choices.install_snapcast,
        ]
        if any(media_choices):
            step("media", "Installing media suite...")
            try:
                installer = MediaSuiteInstaller(
                    rootfs_dir=self._paths.rootfs_dir,
                    runner=self._runner,
                )
                media_result = await installer.apply(
                    choices=self._choices,
                    on_output=on_output,
                )
                result.packages_added.extend(media_result.installed)
                result.warnings.extend(media_result.warnings)
                result.steps_completed.append("media")
                if on_output:
                    on_output(
                        "stdout",
                        f"✅ Media suite installed: {len(media_result.installed)} components",
                    )
            except Exception as exc:
                result.steps_failed.append("media")
                result.warnings.append(f"Media suite failed: {exc}")
                logger.warning("Media suite step failed: %s", exc)
        else:
            step("media", "Skipped (not selected)")

        # Step 5: Install AI brain
        if self._choices.ai_mode != "none":
            step("ai_brain", f"Installing AI brain ({self._choices.ai_mode})...")
            try:
                installer = AIBrainInstaller(
                    rootfs_dir=self._paths.rootfs_dir,
                    runner=self._runner,
                )
                ai_result = await installer.apply(
                    choices=self._choices,
                    on_output=on_output,
                )
                result.packages_added.extend(ai_result.installed)
                result.warnings.extend(ai_result.warnings)
                result.steps_completed.append("ai_brain")
                if on_output:
                    on_output(
                        "stdout",
                        f"✅ AI brain installed: {self._choices.ai_mode}",
                    )
            except Exception as exc:
                result.steps_failed.append("ai_brain")
                result.warnings.append(f"AI brain failed: {exc}")
                logger.warning("AI brain step failed: %s", exc)
        else:
            step("ai_brain", "Skipped (not selected)")

        # Step 6: Repack squashfs
        step("repack", "Repacking squashfs...")
        try:
            self._paths.output_dir.mkdir(parents=True, exist_ok=True)
            await self._docker_builder.repack_squashfs(
                rootfs_dir=self._paths.rootfs_dir,
                output_squashfs=self._paths.output_system,
                on_output=on_output,
            )
            result.output_system = self._paths.output_system
            result.steps_completed.append("repack")
            if on_output:
                size = self._paths.output_system.stat().st_size
                on_output(
                    "stdout",
                    f"✅ Repacked: {self._paths.output_system.name} ({size:,} bytes)",
                )
        except Exception as exc:
            result.steps_failed.append("repack")
            raise FirmwareError(f"Repack failed: {exc}") from exc

        # Step 7: Validate output
        step("validate_output", "Validating output...")
        if self._paths.output_system.exists():
            result.rootfs_size_after = self._paths.output_system.stat().st_size
            result.success = True
            result.steps_completed.append("validate_output")
            if on_output:
                diff = result.rootfs_size_after - result.rootfs_size_before
                direction = "larger" if diff > 0 else "smaller"
                on_output(
                    "stdout",
                    f"✅ Output validated. Image is {abs(diff):,} bytes {direction}",
                )
        else:
            result.steps_failed.append("validate_output")
            raise FirmwareError("Output squashfs not found after repack")

        step("complete", "Pipeline complete!")
        return result

    # ── Cleanup ──────────────────────────────────────────────────────────────

    async def cleanup(self) -> None:
        """Clean up temporary extraction directories."""
        if self._paths.extract_dir.exists():
            logger.info("Cleaning up extraction directory: %s", self._paths.extract_dir)
            shutil.rmtree(self._paths.extract_dir, ignore_errors=True)

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _dir_size(path: Path) -> int:
        """Calculate total size of a directory tree."""
        total = 0
        if path.exists():
            for p in path.rglob("*"):
                if p.is_file():
                    with contextlib.suppress(OSError):
                        total += p.stat().st_size
        return total

    @staticmethod
    def determine_target_slot(active_slot: str) -> tuple[str, str, str]:
        """Determine A/B partition targets based on active slot.

        Args:
            active_slot: Current active partition ("system0" or "system1").

        Returns:
            Tuple of (system_mtd, boot_mtd, slot_label).
            e.g. ("mtd5", "mtd3", "system0") if active is system0.
        """
        if active_slot in ("system0", "mtd4"):
            return "mtd4", "mtd2", "system0"
        else:
            return "mtd5", "mtd3", "system1"


# ── Standalone Device Extraction Helpers ─────────────────────────────────────


async def extract_partition_from_device(
    tool: AmlogicTool,
    partition_label: str,
    output_path: Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    sudo_password: str = "",
) -> Path:
    """Extract a partition directly from a connected device.

    Uses AmlogicTool.mread() to dump the named partition to output_path.
    This is used when the user skipped backup and we need to extract
    the system/boot images directly from the device for firmware customization.

    Args:
        tool: Connected AmlogicTool instance.
        partition_label: Partition label (e.g. "system0", "boot0").
        output_path: Where to save the dumped partition.
        on_progress: Optional callback for progress output lines.
        sudo_password: Optional sudo password for USB permission operations.

    Returns:
        Path to the extracted partition image.

    Raises:
        FirmwareError: If extraction fails.
    """
    from lx06_tool.exceptions import UpdateExeError

    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Extracting partition '%s' directly from device -> %s",
        partition_label,
        output_path,
    )

    try:
        await tool.mread(
            partition=partition_label,
            output_path=output_path,
            on_progress=on_progress,
            sudo_password=sudo_password,
        )
    except UpdateExeError as exc:
        raise FirmwareError(
            f"Failed to extract '{partition_label}' from device: {exc}",
            details="Ensure device is connected and in USB bootloader mode.",
        ) from exc

    if not output_path.exists():
        raise FirmwareError(
            f"Partition extraction produced no output: {output_path}",
            details="The mread command appeared to succeed but no file was created.",
        )

    size = output_path.stat().st_size
    logger.info(
        "Extracted partition '%s': %d bytes", partition_label, size,
    )

    # Validate the dump is actually a squashfs image
    from lx06_tool.utils.squashfs import SquashFSTool
    from lx06_tool.utils.validation import validate_path_safe

    if not SquashFSTool.check_magic_bytes(output_path):
        # SECURITY: Validate path is within expected directory before reading
        try:
            safe_path = validate_path_safe(
                output_path,
                output_path.parent.parent,  # Go up two levels to workspace
                must_exist=True,
            )
        except ValueError as exc:
            raise FirmwareError(
                f"Path validation failed: {exc}",
                details="Output path may be outside expected directory.",
            ) from exc

        # Read first 16 bytes for diagnostics
        with open(safe_path, 'rb') as f:
            header = f.read(16)
        header_hex = header.hex()
        logger.error(
            "Partition dump is NOT a valid squashfs! First 16 bytes: %s",
            header_hex,
        )
        raise FirmwareError(
            f"Dumped image is not a valid squashfs (magic bytes: {header_hex[:8]}). "
            f"The partition dump may have failed — wrong partition label, wrong size, "
            f"or corrupted NAND. Try re-dumping with correct partition size.",
            details=f"File: {safe_path} ({size} bytes). Expected squashfs magic 'hsqs'.",
        )
    logger.info("Partition dump validated as squashfs: %s", output_path)

    return output_path


async def extract_active_system_from_device(
    tool: AmlogicTool,
    output_dir: Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    sudo_password: str = "",
) -> tuple[Path, str]:
    """Extract the active system partition from a connected device.

    Determines which system slot is active (system0 or system1) and extracts
    both the system and corresponding boot partition.

    Args:
        tool: Connected AmlogicTool instance.
        output_dir: Directory to save extracted images.
        on_progress: Optional callback for progress output lines.
        sudo_password: Optional sudo password for USB permission operations.

    Returns:
        Tuple of (system_image_path, active_slot_label).
        e.g. (Path(".../mtd4_system0.img"), "system0")
    """

    output_dir.mkdir(parents=True, exist_ok=True)

    # Try to determine active slot via boot environment
    active_slot = "system0"  # default assumption
    try:
        result = await tool.bulk_cmd("printenv boot_part", timeout=10, sudo_password=sudo_password)
        output = result.stdout + result.stderr
        if "system1" in output or "boot1" in output:
            active_slot = "system1"
            logger.info("Active system slot detected: system1")
        else:
            logger.info(
                "Active system slot: system0 (default, boot_part=%s)",
                output.strip(),
            )
    except Exception as exc:
        logger.debug("Could not determine active slot, defaulting to system0: %s", exc)

    # Map active slot to mtd names
    slot_mtd_map = {
        "system0": ("mtd4", "system0", "boot0"),
        "system1": ("mtd5", "system1", "boot1"),
    }
    mtd_name, sys_label, boot_label = slot_mtd_map[active_slot]

    # Extract system partition
    system_path = output_dir / f"{mtd_name}_{sys_label}.img"
    if on_progress:
        on_progress(f"Extracting {sys_label} partition from device...")
    await extract_partition_from_device(
        tool, sys_label, system_path, on_progress=on_progress,
        sudo_password=sudo_password,
    )

    return system_path, active_slot
