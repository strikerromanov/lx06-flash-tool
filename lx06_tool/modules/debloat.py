"""
Debloat engine for LX06 Flash Tool (Phase 3).

Handles:
- Removal of Xiaomi telemetry and data collection services
- Disabling OTA auto-update mechanisms
- Optional removal of the stock Xiaoai voice engine
- Cleanup of cached data and logs
- Generation of removal manifest for audit trail

All modifications operate on the extracted rootfs directory — no changes
are made to the running device. The debloated rootfs is later repacked
into a squashfs image.

Key principle: REMOVE, don't break. We remove files cleanly rather than
leaving broken symlinks or half-configured services.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lx06_tool.config import CustomizationChoices
from lx06_tool.constants import (
    BLOAT_BINARIES,
    BLOAT_CONFIGS,
    BLOAT_DIRECTORIES,
    BLOAT_SERVICES,
    OTA_PACKAGES,
)
from lx06_tool.utils.compat import AsyncRunner

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class DebloatResult:
    """Result of the debloat operation."""

    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    freed_bytes: int = 0

    @property
    def total_removed(self) -> int:
        return len(self.removed)


# ── Debloat Engine ──────────────────────────────────────────────────────────


class DebloatEngine:
    """Removes bloatware from the extracted LX06 rootfs.

    Scans the rootfs for known Xiaomi services, binaries, and configurations
    and removes them based on user selections.

    Usage:
        engine = DebloatEngine(rootfs_dir=Path('./extracted/rootfs'))
        result = await engine.apply(choices=choices, on_output=callback)
        print(f"Removed {result.total_removed} items")
    """

    def __init__(
        self,
        rootfs_dir: Path,
        runner: AsyncRunner | None = None,
    ):
        self._rootfs = rootfs_dir
        self._runner = runner or AsyncRunner(default_timeout=60.0, sudo=True)

    # ── Main Apply ───────────────────────────────────────────────────────────

    async def apply(
        self,
        choices: CustomizationChoices,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> DebloatResult:
        """Apply debloat selections to the rootfs.

        Args:
            choices: User's debloat selections.
            on_output: Callback for progress messages.

        Returns:
            DebloatResult with removal details.
        """
        result = DebloatResult()

        if on_output:
            on_output("stdout", "Starting debloat...")

        # Step 1: Remove telemetry services
        if choices.remove_telemetry:
            await self._remove_services(
                BLOAT_SERVICES,
                result=result,
                on_output=on_output,
                label="telemetry",
            )

        # Step 2: Disable OTA updates
        if choices.remove_auto_updater:
            await self._remove_ota(
                result=result,
                on_output=on_output,
            )

        # Step 3: Remove stock voice engine (optional, aggressive)
        if choices.remove_xiaoai_voice:
            await self._remove_xiaoai(
                result=result,
                on_output=on_output,
            )

        # Step 4: Clean up binaries
        if choices.remove_telemetry or choices.remove_auto_updater:
            await self._remove_binaries(
                BLOAT_BINARIES,
                result=result,
                on_output=on_output,
            )

        # Step 5: Clean up configs
        if choices.remove_telemetry or choices.remove_auto_updater:
            await self._remove_configs(
                BLOAT_CONFIGS,
                result=result,
                on_output=on_output,
            )

        # Step 6: Clean up directories
        await self._remove_directories(
            BLOAT_DIRECTORIES,
            result=result,
            on_output=on_output,
        )

        # Step 7: Clean up cache and temp files
        await self._cleanup_cache(result=result, on_output=on_output)

        if on_output:
            on_output(
                "stdout",
                f"✅ Debloat complete: {result.total_removed} items removed, "
                f"{result.freed_bytes:,} bytes freed",
            )

        return result

    # ── Service Removal ──────────────────────────────────────────────────────

    async def _remove_services(
        self,
        services: list[str],
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
        label: str = "service",
    ) -> None:
        """Remove init.d services or systemd unit files."""
        for service in services:
            # Check common service locations
            service_paths = [
                self._rootfs / "etc" / "init.d" / service,
                self._rootfs / "etc" / "systemd" / "system" / f"{service}.service",
                self._rootfs / "usr" / "lib" / "systemd" / "system" / f"{service}.service",
                self._rootfs / "etc" / "init" / f"{service}.conf",
            ]

            for svc_path in service_paths:
                if svc_path.exists():
                    size = self._safe_size(svc_path)
                    self._safe_remove(svc_path)
                    result.removed.append(f"service:{service}({svc_path.relative_to(self._rootfs)})")
                    result.freed_bytes += size
                    logger.debug("Removed service: %s", svc_path)

        if on_output:
            on_output("stdout", f"  Services ({label}): checked {len(services)} entries")

    # ── OTA Removal ──────────────────────────────────────────────────────────

    async def _remove_ota(
        self,
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Disable OTA update mechanisms."""
        if on_output:
            on_output("stdout", "  Disabling OTA updates...")

        # Remove OTA binaries
        for ota_pkg in OTA_PACKAGES:
            ota_paths = [
                self._rootfs / "usr" / "bin" / ota_pkg,
                self._rootfs / "usr" / "sbin" / ota_pkg,
                self._rootfs / "opt" / "xiaomi" / ota_pkg,
                self._rootfs / "opt" / ota_pkg,
            ]
            for ota_path in ota_paths:
                if ota_path.exists():
                    size = self._safe_size(ota_path)
                    self._safe_remove(ota_path)
                    result.removed.append(f"ota:{ota_path.relative_to(self._rootfs)}")
                    result.freed_bytes += size

        # Disable OTA in config files
        ota_config_locations = [
            self._rootfs / "etc" / "xiaomi" / "ota.conf",
            self._rootfs / "etc" / "config" / "ota",
            self._rootfs / "etc" / "init.d" / "ota_update",
            self._rootfs / "etc" / "init.d" / "xiaoai_ota",
        ]

        for cfg in ota_config_locations:
            if cfg.exists():
                self._safe_remove(cfg)
                result.removed.append(f"ota_config:{cfg.relative_to(self._rootfs)}")

        # Write a dummy OTA blocker script
        blocker = self._rootfs / "etc" / "init.d" / "S99ota_blocker"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        try:
            blocker.write_text("#!/bin/sh\n# OTA updates disabled by LX06 Flash Tool\nexit 0\n")
            blocker.chmod(0o755)
            result.removed.append("ota_blocker:installed")
        except Exception as exc:
            result.warnings.append(f"Could not install OTA blocker: {exc}")

    # ── Xiaoai Voice Engine Removal ──────────────────────────────────────────

    async def _remove_xiaoai(
        self,
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Remove the stock Xiaoai voice engine (aggressive).

        WARNING: This disables the default wake word and voice assistant.
        Only use if installing an alternative AI brain (open-xiaoai).
        """
        if on_output:
            on_output("stdout", "  ⚠️ Removing stock Xiaoai voice engine...")

        xiaoai_paths = [
            self._rootfs / "opt" / "xiaomi",
            self._rootfs / "usr" / "bin" / "micclient",
            self._rootfs / "usr" / "bin" / "xiaoai",
            self._rootfs / "usr" / "bin" / "mibrain",
            self._rootfs / "usr" / "lib" / "libxiaoai*",
        ]

        for path in xiaoai_paths:
            if path.exists():
                if path.is_dir():
                    size = self._dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                    result.removed.append(f"xiaoai_dir:{path.relative_to(self._rootfs)}")
                    result.freed_bytes += size
                else:
                    size = self._safe_size(path)
                    self._safe_remove(path)
                    result.removed.append(f"xiaoai:{path.relative_to(self._rootfs)}")
                    result.freed_bytes += size

        # Handle glob patterns
        for glob_path in [self._rootfs / "usr" / "lib" / "libxiaoai*"]:
            parent = glob_path.parent
            prefix = glob_path.name.rstrip("*")
            if parent.exists():
                for child in parent.iterdir():
                    if child.name.startswith(prefix):
                        size = self._safe_size(child)
                        self._safe_remove(child)
                        result.removed.append(f"xiaoai_lib:{child.relative_to(self._rootfs)}")
                        result.freed_bytes += size

        result.warnings.append(
            "Stock Xiaoai voice engine removed. You MUST install an alternative AI brain "
            "or the speaker will have no voice input/output."
        )

    # ── Binary Removal ───────────────────────────────────────────────────────

    async def _remove_binaries(
        self,
        binaries: list[str],
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Remove specific bloat binaries from the rootfs."""
        for binary in binaries:
            binary_paths = [
                self._rootfs / "usr" / "bin" / binary,
                self._rootfs / "usr" / "sbin" / binary,
                self._rootfs / "bin" / binary,
                self._rootfs / "sbin" / binary,
            ]
            for bpath in binary_paths:
                if bpath.exists():
                    size = self._safe_size(bpath)
                    self._safe_remove(bpath)
                    result.removed.append(f"binary:{bpath.relative_to(self._rootfs)}")
                    result.freed_bytes += size

    # ── Config Removal ───────────────────────────────────────────────────────

    async def _remove_configs(
        self,
        configs: list[str],
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Remove specific bloat config files."""
        for config in configs:
            config_paths = [
                self._rootfs / "etc" / config,
                self._rootfs / "etc" / "config" / config,
                self._rootfs / "etc" / "xiaomi" / config,
            ]
            for cpath in config_paths:
                if cpath.exists():
                    self._safe_remove(cpath)
                    result.removed.append(f"config:{cpath.relative_to(self._rootfs)}")

    # ── Directory Cleanup ────────────────────────────────────────────────────

    async def _remove_directories(
        self,
        directories: list[str],
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Remove entire bloat directories."""
        for directory in directories:
            dir_path = self._rootfs / directory
            if dir_path.exists() and dir_path.is_dir():
                size = self._dir_size(dir_path)
                shutil.rmtree(dir_path, ignore_errors=True)
                result.removed.append(f"dir:{directory}")
                result.freed_bytes += size

    # ── Cache Cleanup ────────────────────────────────────────────────────────

    async def _cleanup_cache(
        self,
        *,
        result: DebloatResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Clean up cache and temporary files in the rootfs."""
        cache_dirs = [
            self._rootfs / "var" / "cache",
            self._rootfs / "var" / "tmp",
            self._rootfs / "var" / "log",
            self._rootfs / "tmp",
        ]

        for cache_dir in cache_dirs:
            if cache_dir.exists() and cache_dir.is_dir():
                for item in cache_dir.iterdir():
                    try:
                        if item.is_file():
                            size = item.stat().st_size
                            item.unlink()
                            result.freed_bytes += size
                        elif item.is_dir():
                            size = self._dir_size(item)
                            shutil.rmtree(item, ignore_errors=True)
                            result.freed_bytes += size
                    except Exception:
                        pass

    # ── Utility Methods ──────────────────────────────────────────────────────

    def _safe_remove(self, path: Path) -> bool:
        """Safely remove a file, ignoring errors."""
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                return True
        except Exception as exc:
            logger.debug("Could not remove %s: %s", path, exc)
        return False

    @staticmethod
    def _safe_size(path: Path) -> int:
        """Get file size safely."""
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return 0

    @staticmethod
    def _dir_size(path: Path) -> int:
        """Calculate total size of a directory tree."""
        total = 0
        with contextlib.suppress(Exception):
            for p in path.rglob("*"):
                if p.is_file():
                    with contextlib.suppress(OSError):
                        total += p.stat().st_size
        return total
