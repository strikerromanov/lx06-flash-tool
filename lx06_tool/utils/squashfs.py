"""
SquashFS tool wrapper for LX06 Flash Tool.

Provides async wrappers around unsquashfs and mksquashfs for
extracting, modifying, and repacking firmware rootfs images.

Supports both direct host execution and Docker-based builds
for permission isolation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from lx06_tool.constants import (
    SQUASHFS_BLOCK_SIZE,
    SQUASHFS_COMPRESSION,
    SQUASHFS_EXCLUDE,
    SQUASHFS_XATTRS,
)
from lx06_tool.exceptions import (
    InvalidFirmwareError,
    SquashFSExtractError,
    SquashFSRepackError,
)
from lx06_tool.utils.compat import AsyncRunner

logger = logging.getLogger(__name__)


class SquashFSTool:
    """Async wrapper for squashfs operations (extract/repack).

    Usage:
        sqfs = SquashFSTool()
        rootfs_dir = await sqfs.extract(Path("system0.img"), Path("./build/rootfs"))
        # ... modify rootfs_dir ...
        output = await sqfs.repack(rootfs_dir, Path("./build/root.squashfs"))
    """

    def __init__(
        self,
        runner: AsyncRunner | None = None,
        compression: str = SQUASHFS_COMPRESSION,
        block_size: int = SQUASHFS_BLOCK_SIZE,
    ):
        self._runner = runner or AsyncRunner(default_timeout=120.0)
        self._compression = compression
        self._block_size = block_size

    # ── Extract ──────────────────────────────────────────────────────────────

    async def extract(
        self,
        image_path: Path,
        output_dir: Path,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> Path:
        """Extract a squashfs image to a directory.

        Args:
            image_path: Path to the .squashfs or partition image.
            output_dir: Destination directory for extracted rootfs.
            on_output: Callback for real-time output lines.

        Returns:
            Path to the extracted rootfs directory.

        Raises:
            InvalidFirmwareError: If the image is not a valid squashfs.
            SquashFSExtractError: If extraction fails.
        """
        if not image_path.exists():
            raise InvalidFirmwareError(f"Firmware image not found: {image_path}")

        # Remove existing extraction to avoid conflicts
        # Use sudo to handle root-owned directories from previous extractions
        import shutil
        if output_dir.exists():
            logger.debug("Removing existing extraction directory with sudo: %s", output_dir)
            result = await self._runner.run(
                ["rm", "-rf", str(output_dir)],
                timeout=30,
                sudo=True,
            )
            # Fallback to shutil if sudo rm fails (shouldn't happen with password)
            if result.returncode != 0:
                shutil.rmtree(output_dir, ignore_errors=True)

        # Also check if there's a file at the output path (from failed extraction)
        if output_dir.is_file():
            logger.warning("Removing file at extraction directory path with sudo: %s", output_dir)
            await self._runner.run(
                ["rm", "-f", str(output_dir)],
                timeout=10,
                sudo=True,
            )

        output_dir.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Extracting squashfs: %s → %s", image_path.name, output_dir)

        # Try extraction without sudo first to avoid root ownership issues
        # This allows subsequent file operations to work without permission errors
        result = await self._runner.run(
            ["unsquashfs", "-d", str(output_dir), str(image_path)],
            timeout=120,
            on_output=on_output,
            sudo=False,  # Try without sudo to preserve user ownership
        )

        # If extraction failed without sudo, retry with sudo
        # (Some images might need sudo for device nodes)
        if not result.success:
            logger.info("Extraction without sudo failed, retrying with sudo...")
            result = await self._runner.run(
                ["unsquashfs", "-d", str(output_dir), str(image_path)],
                timeout=120,
                on_output=on_output,
                sudo=True,  # Retry with sudo for device nodes
            )

        if not result.success:
            raise SquashFSExtractError(
                f"Failed to extract {image_path}: {result.stderr}",
                details="Ensure squashfs-tools is installed and the image is valid.",
            )

        if not output_dir.exists():
            raise SquashFSExtractError(
                f"Extraction output directory not created: {output_dir}"
            )

        file_count = sum(1 for _ in output_dir.rglob("*"))
        logger.info("Extracted %d items to %s", file_count, output_dir)
        return output_dir

    # ── Repack ───────────────────────────────────────────────────────────────

    async def repack(
        self,
        rootfs_dir: Path,
        output_path: Path,
        *,
        compression: str | None = None,
        block_size: int | None = None,
        exclude: list[str] | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> Path:
        """Repack a directory into a squashfs image.

        Args:
            rootfs_dir: Directory containing the modified rootfs.
            output_path: Destination .squashfs file path.
            compression: Compression algorithm (default from config).
            block_size: Block size in bytes (default from config).
            exclude: Glob patterns to exclude from the image.
            on_output: Callback for real-time output lines.

        Returns:
            Path to the created squashfs image.

        Raises:
            SquashFSRepackError: If repacking fails.
        """
        if not rootfs_dir.exists():
            raise SquashFSRepackError(f"Rootfs directory not found: {rootfs_dir}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing image to avoid appending
        if output_path.exists():
            output_path.unlink()

        comp = compression or self._compression
        bsize = block_size or self._block_size
        excl = exclude or SQUASHFS_EXCLUDE

        cmd: list[str] = [
            "mksquashfs",
            str(rootfs_dir),
            str(output_path),
            "-comp", comp,
            "-b", str(bsize),
            "-noappend",
            "-no-progress",
        ]

        # Add xattr support
        if SQUASHFS_XATTRS:
            cmd.append("-xattrs")
        else:
            cmd.append("-no-xattrs")

        # Add exclusions
        for pattern in excl:
            cmd.extend(["-e", pattern])

        logger.info(
            "Repacking squashfs: %s → %s (comp=%s, bs=%d)",
            rootfs_dir.name, output_path.name, comp, bsize,
        )

        result = await self._runner.run(
            cmd,
            timeout=300,  # Large rootfs can take a while
            on_output=on_output,
            sudo=True,  # Need root to preserve ownership/permissions
        )

        if not result.success:
            raise SquashFSRepackError(
                f"Failed to repack {rootfs_dir}: {result.stderr}",
                details="Check disk space and permissions.",
            )

        if not output_path.exists():
            raise SquashFSRepackError(
                f"Output squashfs not created: {output_path}"
            )

        size = output_path.stat().st_size
        logger.info("Created squashfs: %s (%d bytes)", output_path.name, size)
        return output_path

    # ── Info / Validation ────────────────────────────────────────────────────

    async def info(self, image_path: Path) -> dict[str, str | int]:
        """Get information about a squashfs image.

        Runs unsquashfs -s to retrieve metadata.

        Returns:
            Dict with keys like 'compression', 'block_size', 'inode_count', etc.
        """
        result = await self._runner.run(
            ["unsquashfs", "-s", str(image_path)],
            check=True,
        )
        return self._parse_info_output(result.stdout)

    async def validate(self, image_path: Path) -> bool:
        """Check if a file is a valid squashfs image."""
        # Quick magic-byte check first
        if not self.check_magic_bytes(image_path):
            return False
        result = await self._runner.run(
            ["unsquashfs", "-s", str(image_path)],
            timeout=10,
        )
        return result.success

    @staticmethod
    def _parse_info_output(output: str) -> dict[str, str | int]:
        """Parse unsquashfs -s output into a structured dict."""
        info: dict[str, str | int] = {}
        for line in output.splitlines():
            line = line.strip()
            if "Compression" in line:
                info["compression"] = line.split()[-1]
            elif "Block size" in line:
                try:
                    info["block_size"] = int(line.split()[-1])
                except ValueError:
                    pass
            elif "inodes" in line.lower():
                try:
                    info["inode_count"] = int(line.split()[0].replace(",", ""))
                except ValueError:
                    pass
        return info

    @staticmethod
    def check_magic_bytes(path: Path) -> bool:
        """Quick check if a file starts with squashfs magic bytes.

        SquashFS magic: bytes 0-3 = b'hsqs' (0x68737173) for little-endian,
        or b'sqsh' (0x73717368) for big-endian.

        Returns True if the file appears to be a valid squashfs.
        """
        try:
            with open(path, 'rb') as f:
                magic = f.read(4)
            return magic in (b'hsqs', b'sqsh')
        except OSError:
            return False
