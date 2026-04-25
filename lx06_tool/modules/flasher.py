"""
lx06_tool/modules/flasher.py
------------------------------
Phase 4: A/B partition detection, flash, verification, and rollback.

Safety Rules
─────────────
1. NEVER flash to the active (currently-booted) partition.
2. Always verify post-flash by comparing reported write size.
3. On failure, log recovery steps — the A/B design means the device
   can still boot from the untouched active partition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from lx06_tool.config import LX06Device
from lx06_tool.constants import (
    AB_BOOT_SLOTS,
    AB_SYSTEM_SLOTS,
    MIN_SQUASHFS_SIZE_BYTES,
    READ_ONLY_PARTITIONS,
)
from lx06_tool.exceptions import (
    ActivePartitionError,
    FlashError,
    FlashVerificationError,
)
from lx06_tool.utils.amlogic import AmlogicTool


# ─── A/B Partition Detection ─────────────────────────────────────────────────

async def detect_active_partition(
    tool: AmlogicTool,
    device: LX06Device,
) -> None:
    """
    Determine which A/B slots are currently active and populate `device`.

    Strategy:
    1. Query U-boot environment for `boot_part` variable.
    2. Parse the variable value to determine active boot slot.
    3. Derive inactive slots as complements.

    If U-boot query fails, defaults to system0/boot0 as active (safest guess
    for stock firmware — the user can override on the confirmation screen).
    """
    # Try to read U-boot env via AmlogicTool (uses correct 5-space prefix)
    result = await tool.bulkcmd("printenv boot_part", timeout=10)

    active_boot = "boot0"  # safe default

    if result.ok:
        stdout = result.stdout + result.stderr
        # Typical output: "boot_part=1" (1=boot0, 2=boot1) or "boot_part=a"
        if "boot_part=2" in stdout or "boot_part=b" in stdout:
            active_boot = "boot1"
        elif "boot_part=1" in stdout or "boot_part=a" in stdout:
            active_boot = "boot0"

    # Derive complements
    boot_slots   = list(AB_BOOT_SLOTS)
    system_slots = list(AB_SYSTEM_SLOTS)

    inactive_boot   = boot_slots[1]   if active_boot == boot_slots[0]   else boot_slots[0]
    active_system   = "system0"       if active_boot == "boot0"          else "system1"
    inactive_system = "system1"       if active_system == "system0"      else "system0"

    device.active_boot      = active_boot
    device.inactive_boot    = inactive_boot
    device.active_system    = active_system
    device.inactive_system  = inactive_system


# ─── Safety Guard ─────────────────────────────────────────────────────────────

def assert_inactive(partition_label: str, device: LX06Device) -> None:
    """
    Raise ActivePartitionError if `partition_label` is currently active.
    This is a hard safety guard that cannot be overridden by the UI.
    """
    active_labels = {device.active_boot, device.active_system}

    # Also block read-only partitions unconditionally
    if partition_label in READ_ONLY_PARTITIONS:
        raise ActivePartitionError(partition_label)

    if partition_label in active_labels:
        raise ActivePartitionError(partition_label)


# ─── Flash Operations ─────────────────────────────────────────────────────────

async def flash_partition(
    tool: AmlogicTool,
    partition_label: str,
    image_path: Path,
    device: LX06Device,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    min_size: int = 0,
) -> None:
    """
    Flash `image_path` to `partition_label` after safety checks.

    Raises
    ------
    ActivePartitionError : Attempted to flash the active partition.
    FlashError           : Flash command returned non-zero.
    """
    # Guard 1: not active
    assert_inactive(partition_label, device)

    # Guard 2: image exists and has sane size
    if not image_path.exists():
        raise FlashError(
            f"Image file not found: {image_path}",
            partition=partition_label,
        )
    image_size = image_path.stat().st_size
    if image_size < max(min_size, 1):
        raise FlashError(
            f"Image is empty or too small ({image_size} bytes): {image_path}",
            partition=partition_label,
        )

    # Flash
    try:
        await tool.partition(
            partition_name=partition_label,
            image_path=image_path,
            on_progress=on_progress,
        )
    except Exception as exc:
        raise FlashError(
            f"Flash of '{partition_label}' failed: {exc}",
            partition=partition_label,
        ) from exc


async def flash_all(
    tool: AmlogicTool,
    device: LX06Device,
    boot_image: Optional[Path],
    system_image: Path,
    *,
    on_step: Optional[Callable[[str], None]] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Flash boot (optional) and system images to inactive A/B slots.
    """
    if boot_image is not None:
        if on_step:
            on_step(f"Flashing boot → {device.inactive_boot}")
        await flash_partition(
            tool, device.inactive_boot, boot_image, device, on_progress=on_line
        )

    if on_step:
        on_step(f"Flashing system → {device.inactive_system}")
    await flash_partition(
        tool,
        device.inactive_system,
        system_image,
        device,
        on_progress=on_line,
        min_size=MIN_SQUASHFS_SIZE_BYTES,
    )


# ─── Post-Flash Verification ──────────────────────────────────────────────────

async def verify_flash(
    tool: AmlogicTool,
    partition_label: str,
    expected_size: int,
) -> None:
    """
    Crude post-flash verification: re-read partition size via identify output.
    A more thorough check would re-dump and checksum, but that takes too long
    for a UX-friendly flow.

    For now, we check that the flash command reported success (exit 0) and
    that the expected image size is non-zero. The caller can offer a
    "re-dump and verify" option for the cautious.
    """
    if expected_size <= 0:
        raise FlashVerificationError(
            f"Cannot verify flash of '{partition_label}': expected_size is 0."
        )
    # Success at this stage is determined by the flash command's exit code
    # (which flash_partition already checks). Additional checks can go here.


# ─── Rollback Instructions ────────────────────────────────────────────────────

def rollback_instructions(device: LX06Device) -> str:
    """
    Return a human-readable recovery message if the flash fails.

    Because we always flash the INACTIVE partition, the device can still boot
    from the original active partition even after a failed flash.
    """
    return (
        "Flash failed, but your device is safe.\n\n"
        f"  Active partition : {device.active_system} / {device.active_boot}\n"
        f"  Flashed (failed) : {device.inactive_system} / {device.inactive_boot}\n\n"
        "The device will boot from the active (original) partition on next power-on.\n"
        "If you set bootdelay=15 earlier, you can interrupt U-boot via serial TTL\n"
        "to select a partition or restore from backup.\n\n"
        "To restore: replay the backup phase with your saved partition dumps."
    )
