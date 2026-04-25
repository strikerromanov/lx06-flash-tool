"""
Docker-based firmware builder for LX06 Flash Tool (Phase 3).

Handles:
- Building the firmware builder Docker image
- Running firmware modifications inside an isolated container
- Mounting host directories for input/output
- Privileged container management for device access

Using Docker for firmware builds isolates the host system from:
- Permission issues with squashfs rootfs ownership
- Dependency conflicts between host and target packages
- Filesystem permission mismatches (ARM rootfs vs x86 host)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lx06_tool.constants import FIRMWARE_BUILDER_IMAGE, DOCKERFILE_PATH
from lx06_tool.exceptions import DockerBuildError
from lx06_tool.utils.docker_utils import DockerUtils
from lx06_tool.utils.compat import AsyncRunner

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class BuildResult:
    """Result of a Docker-based firmware build operation."""

    success: bool
    image: str = ""
    container_id: str = ""
    output_files: list[str] = None
    logs: str = ""
    duration_sec: float = 0.0

    def __post_init__(self):
        if self.output_files is None:
            self.output_files = []


# ── Docker Builder ──────────────────────────────────────────────────────────


class DockerBuilder:
    """Manages Docker-based firmware build operations.

    Provides isolated environment for squashfs manipulation,
    preventing host permission and dependency issues.

    Usage:
        builder = DockerBuilder()
        await builder.ensure_image()
        result = await builder.run_build(
            script_path=Path('./scripts/customize.sh'),
            firmware_dir=Path('./firmware'),
            output_dir=Path('./output'),
        )
    """

    def __init__(
        self,
        runner: AsyncRunner | None = None,
        docker: DockerUtils | None = None,
    ):
        self._runner = runner or AsyncRunner(default_timeout=300.0, sudo=True)
        self._docker = docker or DockerUtils(runner=self._runner)

    # ── Image Management ─────────────────────────────────────────────────────

    async def ensure_image(
        self,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> str:
        """Ensure the firmware builder Docker image is available.

        Builds the image from the Dockerfile if it doesn't exist.

        Args:
            on_output: Callback for build output.

        Returns:
            Image name/tag.

        Raises:
            DockerBuildError: If image build fails.
        """
        if await self._docker.image_exists(FIRMWARE_BUILDER_IMAGE):
            logger.info("Firmware builder image already exists: %s", FIRMWARE_BUILDER_IMAGE)
            return FIRMWARE_BUILDER_IMAGE

        logger.info("Building firmware builder image: %s", FIRMWARE_BUILDER_IMAGE)
        if on_output:
            on_output("stdout", f"Building Docker image {FIRMWARE_BUILDER_IMAGE}...")

        dockerfile = Path(DOCKERFILE_PATH)
        if not dockerfile.exists():
            raise DockerBuildError(
                f"Dockerfile not found: {dockerfile}",
                details="The firmware builder Dockerfile is required for isolated builds.",
            )

        build_context = dockerfile.parent
        await self._docker.build_image(
            dockerfile_path=dockerfile,
            context_dir=build_context,
            on_output=on_output,
        )

        logger.info("Firmware builder image built: %s", FIRMWARE_BUILDER_IMAGE)
        if on_output:
            on_output("stdout", f"✅ Docker image ready: {FIRMWARE_BUILDER_IMAGE}")

        return FIRMWARE_BUILDER_IMAGE

    # ── Build Execution ──────────────────────────────────────────────────────

    async def run_build(
        self,
        firmware_dir: Path,
        output_dir: Path,
        script_path: Path | None = None,
        env_vars: dict[str, str] | None = None,
        *,
        on_output: Callable[[str, str], None] | None = None,
        timeout: float = 600.0,
    ) -> BuildResult:
        """Run a firmware build inside a Docker container.

        The container mounts the firmware directory (containing the
        extracted squashfs) and an output directory for results.

        Args:
            firmware_dir: Path to extracted firmware rootfs.
            output_dir: Path for build output files.
            script_path: Optional build script to execute inside container.
            env_vars: Environment variables to pass to the container.
            on_output: Callback for container output.
            timeout: Maximum build time in seconds.

        Returns:
            BuildResult with success status and output files.

        Raises:
            DockerBuildError: If the build fails.
        """
        import time as _time

        start = _time.monotonic()

        firmware_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Construct docker run command
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{firmware_dir.resolve()}:/firmware:rw",
            "-v", f"{output_dir.resolve()}:/output:rw",
        ]

        # Add environment variables
        if env_vars:
            for key, value in env_vars.items():
                cmd.extend(["-e", f"{key}={value}"])

        # Add the image
        cmd.append(FIRMWARE_BUILDER_IMAGE)

        # Add the command to run
        if script_path:
            # Mount script into container
            cmd.insert(-1, "-v")
            cmd.insert(-1, f"{script_path.resolve()}:/build.sh:ro")
            cmd.extend(["bash", "/build.sh"])
        else:
            # Default: just validate the firmware structure
            cmd.extend(["bash", "-c", "echo 'Build container ready' && ls -la /firmware/"])

        logger.info("Running Docker build: %s", " ".join(cmd[:8]))
        if on_output:
            on_output("stdout", "Starting containerized firmware build...")

        result = await self._runner.run(
            cmd,
            timeout=timeout,
            on_output=on_output,
            sudo=False,  # Docker handles its own permissions
        )

        duration = _time.monotonic() - start

        # Collect output files
        output_files = []
        if output_dir.exists():
            output_files = [str(p) for p in output_dir.iterdir() if p.is_file()]

        build_result = BuildResult(
            success=result.success,
            image=FIRMWARE_BUILDER_IMAGE,
            logs=result.combined_output,
            output_files=output_files,
            duration_sec=round(duration, 2),
        )

        if result.success:
            logger.info(
                "Docker build completed in %.1fs, %d output files",
                duration, len(output_files),
            )
            if on_output:
                on_output("stdout", f"✅ Build completed in {duration:.1f}s")
        else:
            logger.error("Docker build failed: %s", result.stderr[:200])
            if on_output:
                on_output("stdout", f"❌ Build failed: {result.stderr[:200]}")

        return build_result

    # ── SquashFS Repack in Container ──────────────────────────────────────────

    async def repack_squashfs(
        self,
        rootfs_dir: Path,
        output_squashfs: Path,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> Path:
        """Repack a modified rootfs into a squashfs image inside Docker.

        This is the preferred method for repacking because Docker
        handles the root permissions needed for proper squashfs ownership.

        Args:
            rootfs_dir: Path to the extracted/modified rootfs directory.
            output_squashfs: Path for the output squashfs file.
            on_output: Callback for progress.

        Returns:
            Path to the created squashfs file.

        Raises:
            DockerBuildError: If repack fails.
        """
        output_dir = output_squashfs.parent
        output_dir.mkdir(parents=True, exist_ok=True)

        mksquashfs_cmd = (
            f"mksquashfs /firmware {output_squashfs.name} "
            f"-comp lz4 -Xhc -noappend -root-owned"
        )

        env_vars = {
            "MKSQUASHFS_CMD": mksquashfs_cmd,
        }

        build_result = await self.run_build(
            firmware_dir=rootfs_dir,
            output_dir=output_dir,
            env_vars=env_vars,
            on_output=on_output,
        )

        if not build_result.success:
            raise DockerBuildError(
                f"SquashFS repack failed in Docker container",
                details=build_result.logs[:500],
            )

        if not output_squashfs.exists():
            raise DockerBuildError(
                f"SquashFS output not found: {output_squashfs}",
                details="The mksquashfs command may have failed silently.",
            )

        logger.info(
            "SquashFS repacked: %s (%d bytes)",
            output_squashfs, output_squashfs.stat().st_size,
        )
        return output_squashfs
