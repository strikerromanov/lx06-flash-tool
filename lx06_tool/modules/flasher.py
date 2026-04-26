"""
lx06_tool/modules/flasher.py
------------------------------
Phase 4: Flash firmware to the LX06 device.

Based on the official xiaoai-patch procedure:
https://github.com/duhow/xiaoai-patch/blob/master/research/lx06/install.md

Official flash commands:
    update partition boot0 boot.img
    update partition boot1 boot.img
    update partition system0 root.squashfs
    update partition system1 root.squashfs

Bootloader unlock:
    update bulkcmd "setenv bootdelay 15"
    update bulkcmd "saveenv"
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from lx06_tool.constants import (
    FLASH_PARTITION_TIMEOUTS,
    MIN_SQUASHFS_SIZE_BYTES,
)
from lx06_tool.exceptions import FlashError
from lx06_tool.utils.amlogic import AmlogicTool

# ─── Bootloader Unlock ────────────────────────────────────────────────────────

async def unlock_bootloader(
    tool: AmlogicTool,
    *,
    bootdelay: int = 15,
    on_step: Callable[[str], None] | None = None,
    sudo_password: str = "",
) -> None:
    """
    Set U-boot bootdelay to allow serial/console access.

    Official commands::

        update bulkcmd "setenv bootdelay 15"
        update bulkcmd "saveenv"
    """
    if on_step:
        on_step(f"Setting bootdelay={bootdelay}...")

    await tool.bulkcmd(
        f"setenv bootdelay {bootdelay}",
        sudo_password=sudo_password,
    )
    await tool.bulkcmd(
        "saveenv",
        sudo_password=sudo_password,
    )

    if on_step:
        on_step("Bootloader unlocked (bootdelay saved).")


# ─── Flash Single Partition ───────────────────────────────────────────────────

async def flash_partition(
    tool: AmlogicTool,
    partition_label: str,
    image_path: Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    min_size: int = 0,
    sudo_password: str = "",
) -> None:
    """
    Flash `image_path` to a named partition.

    Uses the official command syntax::

        update partition <name> <file>

    No type suffix — matches the official xiaoai-patch guide exactly.
    """
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

    # Resolve timeout for this partition
    from lx06_tool.constants import DEFAULT_FLASH_TIMEOUT
    flash_timeout = FLASH_PARTITION_TIMEOUTS.get(partition_label, DEFAULT_FLASH_TIMEOUT)

    try:
        await tool.partition(
            partition_name=partition_label,
            image_path=image_path,
            timeout=flash_timeout,
            on_progress=on_progress,
            sudo_password=sudo_password,
        )
    except Exception as exc:
        raise FlashError(
            f"Flash of '{partition_label}' failed: {exc}",
            partition=partition_label,
        ) from exc


# ─── Flash All Partitions (Official Procedure) ────────────────────────────────

async def flash_all(
    tool: AmlogicTool,
    boot_image: Path | None,
    system_image: Path,
    *,
    on_step: Callable[[str], None] | None = None,
    on_line: Callable[[str], None] | None = None,
    sudo_password: str = "",
    unlock_bl: bool = True,
) -> None:
    """
    Flash boot and system images to BOTH A and B partitions.

    This follows the official xiaoai-patch procedure exactly:

    1. (Optional) Unlock bootloader: setenv bootdelay + saveenv
    2. Flash boot0 AND boot1 with the same boot.img
    3. Flash system0 AND system1 with the same root.squashfs

    Flashing both slots ensures the device can boot from either,
    preventing soft-bricks from A/B slot switching.
    """
    # Step 0: Unlock bootloader
    if unlock_bl:
        if on_step:
            on_step("Unlocking bootloader...")
        try:
            await unlock_bootloader(
                tool,
                on_step=on_step,
                sudo_password=sudo_password,
            )
        except Exception as exc:
            if on_line:
                on_line(f"  [yellow]⚠ Bootloader unlock failed (non-fatal): {exc}[/]")

    # Step 1: Flash boot partitions (BOTH slots)
    if boot_image is not None and boot_image.exists():
        for slot in ("boot0", "boot1"):
            if on_step:
                on_step(f"Flashing {slot}...")
            await flash_partition(
                tool,
                partition_label=slot,
                image_path=boot_image,
                on_progress=on_line,
                sudo_password=sudo_password,
            )
            if on_line:
                on_line(f"  [green]✓ {slot} flashed successfully[/]")
    else:
        if on_line:
            on_line("  [yellow]⚠ No boot image provided, skipping boot partitions[/]")

    # Step 2: Flash system partitions (BOTH slots)
    for slot in ("system0", "system1"):
        if on_step:
            on_step(f"Flashing {slot}...")
        await flash_partition(
            tool,
            partition_label=slot,
            image_path=system_image,
            on_progress=on_line,
            min_size=MIN_SQUASHFS_SIZE_BYTES,
            sudo_password=sudo_password,
        )
        if on_line:
            on_line(f"  [green]✓ {slot} flashed successfully[/]")

    if on_step:
        on_step("All partitions flashed successfully!")


# ─── Post-Flash Verification ──────────────────────────────────────────────────

async def verify_flash(
    tool: AmlogicTool,
    partition_label: str,
    expected_size: int,
) -> None:
    """
    Post-flash verification: re-read partition and check size.

    This is optional — the official xiaoai-patch guide does not
    include a verification step.
    """
    # The flash command already reports success/failure via exit code.
    # A full verify would require re-dumping the partition which takes
    # several minutes over USB. For now, we trust the exit code.
    pass
