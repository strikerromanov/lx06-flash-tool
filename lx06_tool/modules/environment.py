"""
Host environment setup module for LX06 Flash Tool (Phase 1).

Handles:
- Host OS detection (Debian/Ubuntu, Fedora/RHEL, Arch/Manjaro)
- Package manager auto-detection (apt, dnf, pacman)
- Dependency checking and installation
- Docker availability verification
- aml-flash-tool download and setup

All operations are async to keep the TUI responsive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lx06_tool.constants import (
    AML_FLASH_TOOL_REPO,
    AML_FLASH_TOOL_VERSION,
    SUPPORTED_PACKAGE_MANAGERS,
)
from lx06_tool.exceptions import (
    DockerNotAvailableError,
    EnvironmentError,
    PackageNotFoundError,
    PermissionDeniedError,
    UnsupportedOSError,
)
from lx06_tool.utils.runner import AsyncRunner, CommandResult
from lx06_tool.utils.downloader import AsyncDownloader

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class OSInfo:
    """Detected host operating system information."""

    id: str = ""                # e.g. "ubuntu", "fedora", "arch"
    id_like: str = ""           # e.g. "debian", "rhel", "arch"
    name: str = ""              # e.g. "Ubuntu 24.04 LTS"
    version: str = ""           # e.g. "24.04"
    version_codename: str = ""  # e.g. "noble"
    arch: str = ""              # e.g. "x86_64", "aarch64"
    kernel: str = ""            # e.g. "6.5.0-44-generic"


@dataclass
class PackageStatus:
    """Status of a single system dependency."""

    name: str                  # Generic name (e.g. "git")
    package_name: str          # Disto-specific name (e.g. "git")
    installed: bool = False
    version: str = ""


@dataclass
class EnvironmentReport:
    """Complete environment check report.

    Returned by EnvironmentManager.check() to give a full picture
    of the host's readiness for LX06 flashing.
    """

    os_info: OSInfo = field(default_factory=OSInfo)
    pkg_manager: str = ""          # "apt", "dnf", or "pacman"
    packages: list[PackageStatus] = field(default_factory=list)
    docker_available: bool = False
    docker_user_perm: bool = False  # User in docker group
    aml_tool_installed: bool = False
    aml_tool_path: str = ""
    all_ready: bool = False
    missing_packages: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Human-readable one-line summary."""
        parts = [f"OS: {self.os_info.name or 'unknown'}"]
        parts.append(f"PM: {self.pkg_manager or 'none'}")
        if self.missing_packages:
            parts.append(f"Missing: {len(self.missing_packages)} pkgs")
        else:
            parts.append("All deps installed")
        parts.append(f"Docker: {'✓' if self.docker_available else '✗'}")
        parts.append(f"AML tool: {'✓' if self.aml_tool_installed else '✗'}")
        return " | ".join(parts)


# ── Environment Manager ────────────────────────────────────────────────────


class EnvironmentManager:
    """Manages host environment detection, dependency installation, and tool setup.

    Usage:
        mgr = EnvironmentManager(config)
        report = await mgr.check()
        if report.missing_packages:
            await mgr.install_dependencies(report, on_output=ui_callback)
        if not report.aml_tool_installed:
            await mgr.download_aml_tool(tools_dir, on_output=ui_callback)
    """

    # Required dependencies mapped by generic name
    REQUIRED_DEPS = ["libusb", "git", "squashfs_tools", "docker"]

    def __init__(
        self,
        runner: AsyncRunner | None = None,
        downloader: AsyncDownloader | None = None,
        sudo_password: str | None = None,
    ):
        self._runner = runner or AsyncRunner(default_timeout=60.0, sudo=True, sudo_password=sudo_password)
        self._downloader = downloader or AsyncDownloader()
        self._sudo_password = sudo_password

    # ── OS Detection ─────────────────────────────────────────────────────────

    async def detect_os(self) -> OSInfo:
        """Detect the host operating system.

        Reads /etc/os-release for distro information and uname for kernel/arch.

        Returns:
            OSInfo with detected system details.

        Raises:
            UnsupportedOSError: If the OS cannot be identified.
        """
        info = OSInfo(
            arch=platform.machine(),
            kernel=platform.release(),
        )

        # Try /etc/os-release first (standard across modern Linux)
        os_release = Path("/etc/os-release")
        if os_release.exists():
            parsed = self._parse_os_release(os_release)
            info.id = parsed.get("ID", "").strip('"')
            info.id_like = parsed.get("ID_LIKE", "").strip('"')
            info.name = parsed.get("PRETTY_NAME", "").strip('"')
            info.version = parsed.get("VERSION_ID", "").strip('"')
            info.version_codename = parsed.get("VERSION_CODENAME", "").strip('"')
        else:
            # Fallback: try lsb_release
            try:
                result = await self._runner.run(["lsb_release", "-a"], timeout=5)
                if result.success:
                    for line in result.stdout.splitlines():
                        if "Distributor ID:" in line:
                            info.id = line.split(":", 1)[1].strip().lower()
                        elif "Description:" in line:
                            info.name = line.split(":", 1)[1].strip()
                        elif "Release:" in line:
                            info.version = line.split(":", 1)[1].strip()
                        elif "Codename:" in line:
                            info.version_codename = line.split(":", 1)[1].strip()
            except Exception:
                pass

        logger.info(
            "Detected OS: %s (id=%s, arch=%s, kernel=%s)",
            info.name, info.id, info.arch, info.kernel,
        )
        return info

    async def detect_package_manager(self, os_info: OSInfo | None = None) -> str:
        """Detect the system package manager.

        Checks for apt, dnf, or pacman on the system, matching against
        known distro-to-PM mappings.

        Returns:
            Package manager name: "apt", "dnf", or "pacman".

        Raises:
            UnsupportedOSError: If no supported package manager is found.
        """
        for pm_name, pm_config in SUPPORTED_PACKAGE_MANAGERS.items():
            for detect_path in pm_config["detect"]:
                if Path(detect_path).exists():
                    logger.info("Detected package manager: %s (via %s)", pm_name, detect_path)
                    return pm_name

        raise UnsupportedOSError(
            f"No supported package manager found. "
            f"Supported: {list(SUPPORTED_PACKAGE_MANAGERS.keys())}",
            details="This tool requires apt, dnf, or pacman.",
        )

    # ── Dependency Checks ────────────────────────────────────────────────────

    async def check_dependency(self, dep_name: str, pkg_manager: str) -> PackageStatus:
        """Check if a single dependency is installed.

        Args:
            dep_name: Generic dependency name (e.g. "git").
            pkg_manager: Package manager to look up distro-specific package name.

        Returns:
            PackageStatus with installation state.
        """
        pm_config = SUPPORTED_PACKAGE_MANAGERS.get(pkg_manager, {})
        packages = pm_config.get("packages", {})
        package_name = packages.get(dep_name, dep_name)

        status = PackageStatus(name=dep_name, package_name=package_name)

        # Check if the binary/command is available
        binary_map = {
            "libusb": None,         # Library, not a binary — check via pkg-config or ldconfig
            "git": "git",
            "squashfs_tools": "mksquashfs",
            "docker": "docker",
        }

        binary = binary_map.get(dep_name)
        if binary:
            found = shutil.which(binary)
            if found:
                status.installed = True
                # Try to get version
                try:
                    ver_result = await self._runner.run(
                        [binary, "--version"], timeout=5, sudo=False,
                    )
                    if ver_result.success:
                        # Take first line of version output
                        status.version = ver_result.stdout.splitlines()[0].strip()[:80]
                except Exception:
                    pass
        elif dep_name == "libusb":
            # Check for libusb via ldconfig or pkg-config
            try:
                result = await self._runner.run(
                    ["ldconfig", "-p"], timeout=5, sudo=False,
                )
                if result.success and "libusb" in result.stdout:
                    status.installed = True
            except Exception:
                # Fallback: check if the shared lib file exists
                for lib_path in ["/usr/lib/x86_64-linux-gnu/libusb-0.1.so.4",
                                  "/usr/lib64/libusb-0.1.so.4",
                                  "/usr/lib/libusb-0.1.so.4"]:
                    if Path(lib_path).exists():
                        status.installed = True
                        break

        logger.debug(
            "Dependency %s (%s): %s",
            dep_name, package_name, "installed" if status.installed else "MISSING",
        )
        return status

    async def check_all_dependencies(self, pkg_manager: str) -> list[PackageStatus]:
        """Check all required dependencies.

        Args:
            pkg_manager: Detected package manager name.

        Returns:
            List of PackageStatus for all required dependencies.
        """
        statuses = []
        for dep in self.REQUIRED_DEPS:
            status = await self.check_dependency(dep, pkg_manager)
            statuses.append(status)
        return statuses

    # ── Docker Verification ──────────────────────────────────────────────────

    async def verify_docker(self) -> tuple[bool, bool]:
        """Verify Docker daemon is running and user has permissions.

        Returns:
            Tuple of (daemon_running, user_has_permissions).
        """
        daemon_running = False
        user_has_perm = False

        # Check if docker binary exists
        if not shutil.which("docker"):
            return False, False

        # Check daemon
        result = await self._runner.run(
            ["docker", "info"], timeout=10, sudo=False,
        )
        if result.success:
            daemon_running = True
            user_has_perm = True
        else:
            # Try with sudo
            result_sudo = await self._runner.run(
                ["docker", "info"], timeout=10, sudo=True,
            )
            if result_sudo.success:
                daemon_running = True
                user_has_perm = False
                logger.warning(
                    "Docker daemon is running but user lacks permissions. "
                    "Consider: sudo usermod -aG docker $USER"
                )

        return daemon_running, user_has_perm

    # ── Installation ─────────────────────────────────────────────────────────

    async def install_dependencies(
        self,
        pkg_manager: str,
        packages: list[str] | None = None,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult:
        """Install missing system dependencies.

        Args:
            pkg_manager: Package manager to use (apt/dnf/pacman).
            packages: Specific package generic names to install. None = all missing.
            on_output: Callback for real-time output lines.

        Returns:
            CommandResult from the package manager.

        Raises:
            EnvironmentError: If the package manager fails.
        """
        pm_config = SUPPORTED_PACKAGE_MANAGERS.get(pkg_manager)
        if not pm_config:
            raise UnsupportedOSError(f"Unknown package manager: {pkg_manager}")

        # Resolve generic names to distro-specific package names
        target_pkgs = packages or self.REQUIRED_DEPS
        pkg_names = []
        for dep in target_pkgs:
            name = pm_config["packages"].get(dep, dep)
            if name:
                pkg_names.append(name)

        if not pkg_names:
            logger.info("No packages to install")
            return CommandResult(command=["echo", "nothing to install"], returncode=0)

        # Update package index first
        update_cmd = pm_config["update_cmd"]
        logger.info("Updating package index: %s", update_cmd)
        await self._runner.run(
            update_cmd.split(),
            timeout=120,
            on_output=on_output,
            sudo=True,
        )

        # Install packages
        install_cmd = pm_config["install_cmd"].split()
        full_cmd = [*install_cmd, *pkg_names]

        logger.info("Installing packages: %s", " ".join(pkg_names))
        result = await self._runner.run(
            full_cmd,
            timeout=300,
            on_output=on_output,
            sudo=True,
        )

        if not result.success:
            raise EnvironmentError(
                f"Failed to install packages: {' '.join(pkg_names)}",
                details=result.stderr[:500],
            )

        logger.info("Successfully installed: %s", " ".join(pkg_names))
        return result

    # ── aml-flash-tool Download ──────────────────────────────────────────────

    async def download_aml_tool(
        self,
        tools_dir: Path,
        *,
        on_output: Callable[[str, str], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download and set up aml-flash-tool from Radxa.

        Clones the repository and locates the update binary.

        Args:
            tools_dir: Directory to install tools into.
            on_output: Callback for real-time output lines.
            on_progress: Callback for download progress (bytes_done, bytes_total).

        Returns:
            Path to the update binary.

        Raises:
            EnvironmentError: If download or setup fails.
        """
        tools_dir.mkdir(parents=True, exist_ok=True)
        aml_dir = tools_dir / "aml-flash-tool"

        logger.info("Downloading aml-flash-tool → %s", aml_dir)

        if on_output:
            on_output("stdout", f"Cloning {AML_FLASH_TOOL_REPO}...")

        try:
            await AsyncDownloader.clone_git_repo(
                repo_url=AML_FLASH_TOOL_REPO,
                dest_dir=aml_dir,
                branch=AML_FLASH_TOOL_VERSION,
            )
        except Exception as exc:
            raise EnvironmentError(
                f"Failed to download aml-flash-tool: {exc}",
                details="Check internet connection and GitHub availability.",
            ) from exc

        # Locate the update binary
        update_exe = await self._find_update_binary(aml_dir)
        if not update_exe:
            raise EnvironmentError(
                f"Could not find 'update' binary in {aml_dir}",
                details="The aml-flash-tool repo structure may have changed.",
            )

        # Make it executable
        update_exe.chmod(0o755)

        logger.info("aml-flash-tool ready: %s", update_exe)
        if on_output:
            on_output("stdout", f"aml-flash-tool installed: {update_exe}")

        return update_exe

    async def _find_update_binary(self, aml_dir: Path) -> Path | None:
        """Search for the update binary in the aml-flash-tool directory.

        The binary may be at different locations depending on the repo version:
        - aml-flash-tool/update
        - aml-flash-tool/bin/update
        - aml-flash-tool/build/update
        """
        candidates = [
            aml_dir / "update",
            aml_dir / "bin" / "update",
            aml_dir / "build" / "update",
            aml_dir / "aml-flash-tool" / "update",  # If cloned into subfolder
        ]

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                logger.debug("Found update binary: %s", candidate)
                return candidate

        # Fallback: search recursively
        for p in aml_dir.rglob("update"):
            if p.is_file() and not p.name.endswith(".py"):
                # Verify it's an ELF binary
                try:
                    with open(p, "rb") as f:
                        magic = f.read(4)
                    if magic == b"\x7fELF":
                        logger.debug("Found update binary (recursive): %s", p)
                        return p
                except Exception:
                    continue

        return None

    # ── Full Environment Check ───────────────────────────────────────────────

    async def check(self, tools_dir: Path | None = None) -> EnvironmentReport:
        """Perform a complete environment readiness check.

        Checks OS, package manager, all dependencies, Docker, and aml-flash-tool.

        Args:
            tools_dir: Directory where aml-flash-tool should be installed.

        Returns:
            EnvironmentReport with full status.
        """
        report = EnvironmentReport()

        # Detect OS
        try:
            report.os_info = await self.detect_os()
        except Exception as exc:
            logger.error("OS detection failed: %s", exc)
            report.os_info = OSInfo(arch=platform.machine(), kernel=platform.release())

        # Detect package manager
        try:
            report.pkg_manager = await self.detect_package_manager(report.os_info)
        except UnsupportedOSError as exc:
            logger.error("Package manager detection failed: %s", exc)
            report.all_ready = False
            return report

        # Check dependencies
        report.packages = await self.check_all_dependencies(report.pkg_manager)
        report.missing_packages = [
            p.name for p in report.packages if not p.installed
        ]

        # Check Docker
        report.docker_available, report.docker_user_perm = await self.verify_docker()

        # Check aml-flash-tool
        if tools_dir:
            update_exe = await self._find_update_binary(tools_dir / "aml-flash-tool")
            if update_exe:
                report.aml_tool_installed = True
                report.aml_tool_path = str(update_exe)

        # Determine overall readiness
        report.all_ready = (
            len(report.missing_packages) == 0
            and report.docker_available
            and report.aml_tool_installed
        )

        logger.info("Environment check: %s", report.summary)
        return report

    # ── Setup Helpers ────────────────────────────────────────────────────────

    async def setup_docker_permissions(self) -> None:
        """Add current user to the docker group.

        Requires sudo. The user must log out/in for this to take effect.
        """
        user = os.environ.get("USER", "root")
        if user == "root":
            logger.debug("Running as root, Docker permissions already available")
            return

        logger.info("Adding user '%s' to docker group...", user)
        result = await self._runner.run(
            ["usermod", "-aG", "docker", user],
            sudo=True,
        )
        if result.success:
            logger.info(
                "User added to docker group. Log out and back in for this to take effect."
            )
        else:
            logger.warning("Failed to add user to docker group: %s", result.stderr)

    # ── Parsing Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_os_release(path: Path) -> dict[str, str]:
        """Parse /etc/os-release into a key-value dict.

        Handles quoted and unquoted values.
        """
        data: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, _, value = line.partition("=")
                        data[key.strip()] = value.strip().strip('"')
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", path, exc)
        return data
