"""
lx06_tool/modules/environment.py
---------------------------------
Phase 1: Host environment detection and dependency installation.

CachyOS notes
─────────────
• CachyOS reports ID=cachyos or ID=arch in /etc/os-release, with ID_LIKE=arch.
• Package manager: pacman (base). AUR helpers paru/yay are preferred for AUR pkgs.
• Python is 'python', not 'python3'. venv is built-in — no separate package needed.
• Docker daemon is not auto-started; user must be in the 'docker' group.
• libusb-compat provides the libusb-0.1 ABI that Amlogic's update binary needs.
• PEP 668 applies: pip into system Python is blocked — always use a venv.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from lx06_tool.constants import DISTRO_PACKAGES, OS_FAMILY_MAP, OS_LIKE_MAP
from lx06_tool.exceptions import (
    DependencyInstallError,
    DockerNotRunningError,
    HostEnvironmentError,
    UnsupportedDistroError,
)
from lx06_tool.utils.runner import run

# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class OSInfo:
    """Detected host OS information."""
    name: str           # Pretty name from /etc/os-release
    id: str             # ID field (e.g. "arch", "cachyos", "ubuntu")
    id_like: str        # ID_LIKE field (e.g. "arch", "debian")
    version: str        # VERSION_ID (may be empty on rolling distros)
    family: str         # Normalised: "arch" | "debian" | "fedora"
    pkg_manager: str    # "pacman" | "apt" | "dnf"
    aur_helper: str | None = None  # "paru" | "yay" | None (Arch only)

    @property
    def is_arch_family(self) -> bool:
        return self.family == "arch"

    @property
    def is_cachyos(self) -> bool:
        return "cachyos" in self.id.lower()

    @property
    def install_cmd_prefix(self) -> list[str]:
        """Returns the base install command (without package names)."""
        cmds = {
            "pacman": ["sudo", "pacman", "-S", "--noconfirm", "--needed"],
            "apt":    ["sudo", "apt-get", "install", "-y"],
            "dnf":    ["sudo", "dnf", "install", "-y"],
        }
        return cmds[self.pkg_manager]

    @property
    def aur_install_cmd_prefix(self) -> list[str]:
        """Returns the AUR install command prefix (Arch only)."""
        if self.aur_helper:
            return [self.aur_helper, "-S", "--noconfirm", "--needed"]
        return ["sudo", "pacman", "-S", "--noconfirm", "--needed"]


@dataclass
class DependencyStatus:
    """Result of checking a single dependency."""
    logical_name: str          # e.g. "squashfs_tools"
    package_name: str          # e.g. "squashfs-tools"
    installed: bool = False
    binary: str = ""           # Binary to look for in PATH (empty = skip check)
    notes: str = ""


# ─── OS Detection ─────────────────────────────────────────────────────────────

def detect_os() -> OSInfo:
    """
    Parse /etc/os-release to determine the Linux distribution and family.

    Raises UnsupportedDistroError for non-Linux or unknown distros.
    """
    if platform.system() != "Linux":
        raise UnsupportedDistroError(
            f"This tool only runs on Linux. Detected: {platform.system()}"
        )

    os_release = _parse_os_release()

    raw_id     = os_release.get("ID", "").strip('"').lower()
    raw_like   = os_release.get("ID_LIKE", "").strip('"').lower()
    name       = os_release.get("PRETTY_NAME", raw_id).strip('"')
    version    = os_release.get("VERSION_ID", "").strip('"')

    family = _resolve_family(raw_id, raw_like)
    if family is None:
        raise UnsupportedDistroError(
            f"Unsupported distribution: {name!r} (ID={raw_id!r}, ID_LIKE={raw_like!r}). "
            "Supported families: Debian/Ubuntu, Fedora/RHEL, Arch/CachyOS."
        )

    pkg_manager = _family_to_pm(family)
    aur_helper  = _detect_aur_helper() if family == "arch" else None

    return OSInfo(
        name=name,
        id=raw_id,
        id_like=raw_like,
        version=version,
        family=family,
        pkg_manager=pkg_manager,
        aur_helper=aur_helper,
    )


def _parse_os_release() -> dict[str, str]:
    candidates = [Path("/etc/os-release"), Path("/usr/lib/os-release")]
    for path in candidates:
        if path.exists():
            result: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip().strip('"')
            return result
    return {}


def _resolve_family(raw_id: str, raw_like: str) -> str | None:
    # 1. Direct ID match
    if raw_id in OS_FAMILY_MAP:
        return OS_FAMILY_MAP[raw_id]

    # 2. ID_LIKE match — ID_LIKE can be space-separated list
    for like_token in raw_like.split():
        if like_token in OS_LIKE_MAP:
            return OS_LIKE_MAP[like_token]

    # 3. Substring fallback (e.g. "cachyos" contains "arch" implicitly)
    for key, family in OS_FAMILY_MAP.items():
        if key in raw_id:
            return family

    return None


def _family_to_pm(family: str) -> str:
    return {"arch": "pacman", "debian": "apt", "fedora": "dnf"}[family]


def _detect_aur_helper() -> str | None:
    """Check for paru or yay AUR helpers (Arch family only)."""
    for helper in ("paru", "yay"):
        if shutil.which(helper):
            return helper
    return None


# ─── Dependency Checking ──────────────────────────────────────────────────────

# Which binaries to probe for each logical dependency
_DEPENDENCY_BINARIES: dict[str, str] = {
    "git":            "git",
    "squashfs_tools": "unsquashfs",
    "docker":         "docker",
}


def check_dependencies(os_info: OSInfo) -> list[DependencyStatus]:
    """
    Check which required packages are installed on the host.

    Returns a list of DependencyStatus objects.
    """
    pkg_table = DISTRO_PACKAGES[os_info.family]
    results: list[DependencyStatus] = []

    logical_deps = ["git", "libusb", "squashfs_tools", "docker"]

    for dep in logical_deps:
        pkg_name = pkg_table.get(dep)
        if pkg_name is None:
            # Package not needed on this distro (e.g. python3_venv on Arch)
            continue

        binary = _DEPENDENCY_BINARIES.get(dep, "")
        installed = _is_installed(dep, pkg_name, binary, os_info)
        notes = _dep_notes(dep, os_info)

        results.append(DependencyStatus(
            logical_name=dep,
            package_name=pkg_name,
            installed=installed,
            binary=binary,
            notes=notes,
        ))

    return results


def _is_installed(
    logical: str,
    pkg_name: str,
    binary: str,
    os_info: OSInfo,
) -> bool:
    # For tools with a known binary, check PATH first (fastest)
    if binary and shutil.which(binary):
        return True

    # Fall back to package-manager query
    try:
        if os_info.pkg_manager == "pacman":
            r = subprocess.run(
                ["pacman", "-Q", pkg_name],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0

        elif os_info.pkg_manager == "apt":
            r = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg_name],
                capture_output=True, text=True, timeout=5,
            )
            return "install ok installed" in r.stdout

        elif os_info.pkg_manager == "dnf":
            r = subprocess.run(
                ["rpm", "-q", pkg_name],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0

    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return False


def _dep_notes(logical: str, os_info: OSInfo) -> str:
    notes: dict[str, str] = {}
    if os_info.is_arch_family:
        notes = {
            "libusb": (
                "libusb-compat provides the libusb-0.1 ABI needed by "
                "Amlogic's update binary."
            ),
            "docker": (
                "After install, enable and start the daemon:\n"
                "  sudo systemctl enable --now docker\n"
                "Then add yourself to the docker group:\n"
                "  sudo usermod -aG docker $USER\n"
                "(Log out and back in for this to take effect.)"
            ),
        }
    return notes.get(logical, "")


# ─── Dependency Installation ───────────────────────────────────────────────────

async def install_dependencies(
    missing: list[DependencyStatus],
    os_info: OSInfo,
    *,
    sudo_password: str = "",
) -> None:
    """
    Install all missing packages via the host package manager.

    Raises DependencyInstallError on failure.
    """
    if not missing:
        return

    from lx06_tool.utils.sudo import sudo_run

    pkg_names = [d.package_name for d in missing]

    if os_info.pkg_manager == "apt":
        result = await sudo_run(
            ["apt-get", "update", "-q"],
            password=sudo_password, timeout=120,
        )
        if not result.ok:
            raise DependencyInstallError(
                f"apt-get update failed:\n{result.output}"
            )

    # install_cmd_prefix includes "sudo" prefix; PTY sudo_run adds it, so skip [0]
    cmd = os_info.install_cmd_prefix[1:] + pkg_names
    result = await sudo_run(cmd, password=sudo_password, timeout=300)
    if not result.ok:
        raise DependencyInstallError(
            f"Package installation failed (exit {result.returncode}):\n"
            f"{result.output}"
        )


# ─── Docker Readiness ─────────────────────────────────────────────────────────

async def verify_docker(os_info: OSInfo) -> None:
    """
    Verify Docker is installed, the daemon is running, and the current user
    can communicate with it (no permission error).

    Raises DockerNotRunningError with actionable advice.
    """
    if not shutil.which("docker"):
        raise DockerNotRunningError(
            "Docker binary not found in PATH. Install docker via your package manager."
        )

    result = await run(["docker", "info"], timeout=10)
    if result.returncode != 0:
        stderr = result.stderr.lower()

        if "permission denied" in stderr or "connect: no such file" in stderr:
            if os_info.is_arch_family:
                advice = (
                    "Start the Docker daemon and add your user to the docker group:\n\n"
                    "  sudo systemctl enable --now docker\n"
                    "  sudo usermod -aG docker $USER\n\n"
                    "Then log out and back in (or run: newgrp docker)."
                )
            else:
                advice = (
                    "Start the Docker daemon:\n"
                    "  sudo systemctl start docker\n\n"
                    "Or add your user to the docker group:\n"
                    "  sudo usermod -aG docker $USER"
                )
            raise DockerNotRunningError(
                f"Cannot connect to Docker daemon.\n{advice}"
            )

        raise DockerNotRunningError(
            f"Docker is not working correctly:\n{result.stderr}"
        )


# ─── udev Rules ───────────────────────────────────────────────────────────────

async def install_udev_rules(
    rules_content: str,
    dest: str,
    *,
    sudo_password: str = "",
) -> None:
    """
    Write the Amlogic USB udev rule and reload the rule set.

    Uses PTY-based sudo for reliable password authentication on all distros.
    """
    # Write rules file via PTY-based sudo
    from lx06_tool.exceptions import UdevRulesError
    from lx06_tool.utils.sudo import sudo_run, sudo_write_file

    result = await sudo_write_file(
        rules_content, dest, password=sudo_password, timeout=10,
    )
    if not result.ok:
        raise UdevRulesError(f"Failed to write udev rules to {dest}: {result.output}")

    # Reload and trigger — same on all modern systemd distros
    for cmd in (
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=usb"],
    ):
        result = await sudo_run(cmd, password=sudo_password, timeout=15)
        if not result.ok:
            raise UdevRulesError(
                f"udevadm command failed: sudo {' '.join(cmd)}\n{result.output}"
            )


# ─── Python Environment ───────────────────────────────────────────────────────

def check_python_version(min_major: int = 3, min_minor: int = 10) -> None:
    """Raise if the running Python is too old."""
    v = sys.version_info
    if (v.major, v.minor) < (min_major, min_minor):
        raise HostEnvironmentError(
            f"Python {min_major}.{min_minor}+ is required. "
            f"Running: {v.major}.{v.minor}.{v.micro}"
        )


def is_pep668_managed() -> bool:
    """
    Return True if the current Python is externally managed (PEP 668).
    Relevant on Arch/CachyOS where system pip is blocked.
    """
    marker = Path(sys.prefix) / "EXTERNALLY-MANAGED"
    return marker.exists()


def check_venv() -> bool:
    """Return True if running inside a virtual environment."""
    return sys.prefix != sys.base_prefix or os.environ.get("VIRTUAL_ENV") is not None
