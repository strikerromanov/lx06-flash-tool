"""
Docker utilities for LX06 Flash Tool.

Provides helpers for building and running Docker containers
used in the firmware build process. Docker isolates squashfs
operations from the host to avoid permission and compatibility issues.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from lx06_tool.constants import DOCKER_BUILD_IMAGE_NAME, DOCKER_BUILD_IMAGE_TAG
from lx06_tool.exceptions import DockerBuildError, DockerImageNotFoundError
from lx06_tool.utils.runner import AsyncRunner

logger = logging.getLogger(__name__)


class DockerUtils:
    """Async Docker operations for isolated firmware builds.

    Usage:
        docker = DockerUtils()
        await docker.build_image(dockerfile_path=Path("resources/docker/Dockerfile.firmware-builder"))
        exit_code = await docker.run_in_container(
            image="lx06-firmware-builder:latest",
            command=["mksquashfs", "/build/rootfs", "/build/output/root.squashfs"],
            volumes={"/host/build": "/build"},
        )
    """

    def __init__(
        self,
        runner: AsyncRunner | None = None,
        image_name: str = DOCKER_BUILD_IMAGE_NAME,
        image_tag: str = DOCKER_BUILD_IMAGE_TAG,
    ):
        self._runner = runner or AsyncRunner(default_timeout=300.0)
        self._image_name = image_name
        self._image_tag = image_tag

    @property
    def full_image_name(self) -> str:
        """Fully qualified Docker image name with tag."""
        return f"{self._image_name}:{self._image_tag}"

    async def is_docker_available(self) -> bool:
        """Check if Docker daemon is running and accessible.

        Returns:
            True if Docker is available.
        """
        result = await self._runner.run(
            ["docker", "info"],
            timeout=10,
        )
        return result.success

    async def image_exists(self, image: str | None = None) -> bool:
        """Check if a Docker image exists locally.

        Args:
            image: Image name:tag. Defaults to the firmware builder image.
        """
        target = image or self.full_image_name
        result = await self._runner.run(
            ["docker", "image", "inspect", target],
            timeout=10,
        )
        return result.success

    async def build_image(
        self,
        dockerfile_path: Path,
        context_dir: Path | None = None,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> str:
        """Build the firmware builder Docker image.

        Args:
            dockerfile_path: Path to the Dockerfile.
            context_dir: Build context directory. Defaults to dockerfile parent.
            on_output: Callback for build output lines.

        Returns:
            The built image name:tag.

        Raises:
            DockerBuildError: If the build fails.
        """
        if not dockerfile_path.exists():
            raise DockerBuildError(f"Dockerfile not found: {dockerfile_path}")

        context = context_dir or dockerfile_path.parent
        tag = self.full_image_name

        logger.info("Building Docker image %s from %s", tag, dockerfile_path)

        result = await self._runner.run(
            [
                "docker", "build",
                "-t", tag,
                "-f", str(dockerfile_path),
                str(context),
            ],
            timeout=600,  # Image build can take a while
            on_output=on_output,
        )

        if not result.success:
            raise DockerBuildError(
                f"Docker build failed: {result.stderr}",
                details="Check Dockerfile and available disk space.",
            )

        logger.info("Docker image built: %s", tag)
        return tag

    async def run_in_container(
        self,
        command: list[str],
        *,
        image: str | None = None,
        volumes: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        workdir: str = "/build",
        remove: bool = True,
        on_output: Callable[[str, str], None] | None = None,
    ) -> int:
        """Run a command inside a Docker container.

        Args:
            command: Command and arguments to execute.
            image: Docker image to use. Defaults to firmware builder.
            volumes: Host-path → container-path volume mounts.
            env: Environment variables for the container.
            workdir: Working directory inside the container.
            remove: Remove container after execution.
            on_output: Callback for output lines.

        Returns:
            Container exit code.

        Raises:
            DockerBuildError: If the container fails to start.
        """
        target_image = image or self.full_image_name

        if not await self.image_exists(target_image):
            raise DockerImageNotFoundError(
                f"Docker image not found: {target_image}. Build it first."
            )

        cmd = ["docker", "run"]

        if remove:
            cmd.append("--rm")

        cmd.extend(["--workdir", workdir])

        # Volume mounts
        for host_path, container_path in (volumes or {}).items():
            cmd.extend(["-v", f"{host_path}:{container_path}"])

        # Environment variables
        for key, value in (env or {}).items():
            cmd.extend(["-e", f"{key}={value}"])

        cmd.append(target_image)
        cmd.extend(command)

        logger.info("Running in container: %s", " ".join(command))

        result = await self._runner.run(
            cmd,
            timeout=300,
            on_output=on_output,
        )

        if not result.success:
            raise DockerBuildError(
                f"Container command failed (rc={result.returncode}): {result.stderr}"
            )

        return result.returncode

    async def ensure_image(
        self,
        dockerfile_path: Path,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> str:
        """Ensure the firmware builder image exists, building if necessary.

        Args:
            dockerfile_path: Path to the Dockerfile.
            on_output: Callback for build output.

        Returns:
            The image name:tag.
        """
        if await self.image_exists():
            logger.debug("Docker image %s already exists", self.full_image_name)
            return self.full_image_name

        logger.info("Docker image not found, building...")
        return await self.build_image(dockerfile_path, on_output=on_output)
