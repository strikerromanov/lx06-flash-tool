"""
Media suite installer for LX06 Flash Tool (Phase 3).

Handles:
- Installation of AirPlay receiver (Shairport-Sync)
- DLNA/UPnP media renderer (Upmpdcli / MPD)
- Spotify Connect daemon (librespot / raspotify)
- Multi-room audio (Squeezelite / Snapcast)
- Configuration generation via Jinja2 templates
- Init script creation for auto-start

All binaries are expected to be pre-compiled for ARM64 (aarch64)
and stored in the resources/binaries/ directory. The installer
copies them into the rootfs and generates appropriate configs.

Reference: These are the same tools used by xiaoai-patch for
transforming the LX06 into a universal cast receiver.
"""

from __future__ import annotations

import logging
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lx06_tool.config import CustomizationChoices
from lx06_tool.exceptions import FirmwareError
from lx06_tool.utils.runner import AsyncRunner

logger = logging.getLogger(__name__)


# ── Binary / Config Definitions ─────────────────────────────────────────────

# Each media component: binary name(s), config template, init script
MEDIA_COMPONENTS = {
    "airplay": {
        "binaries": ["shairport-sync"],
        "config_template": "shairport-sync.conf.j2",
        "config_dest": "etc/shairport-sync.conf",
        "init_name": "S50shairport-sync",
        "description": "AirPlay receiver",
    },
    "dlna": {
        "binaries": ["upmpdcli", "mpd", "mpc"],
        "config_template": "upmpdcli.conf.j2",
        "config_dest": "etc/upmpdcli.conf",
        "init_name": "S50upmpdcli",
        "description": "DLNA/UPnP renderer",
    },
    "spotify": {
        "binaries": ["librespot"],
        "config_template": "librespot.toml.j2",
        "config_dest": "etc/librespot.toml",
        "init_name": "S50librespot",
        "description": "Spotify Connect daemon",
    },
    "squeezelite": {
        "binaries": ["squeezelite"],
        "config_template": None,
        "config_dest": None,
        "init_name": "S50squeezelite",
        "description": "Squeezelite (multi-room)",
    },
    "snapcast": {
        "binaries": ["snapclient"],
        "config_template": None,
        "config_dest": None,
        "init_name": "S50snapclient",
        "description": "Snapcast client (multi-room)",
    },
}


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class MediaInstallResult:
    """Result of media suite installation."""

    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_installed(self) -> int:
        return len(self.installed)


# ── Media Suite Installer ───────────────────────────────────────────────────


class MediaSuiteInstaller:
    """Installs media player components into the extracted LX06 rootfs.

    Copies pre-compiled ARM64 binaries and generates configuration files
    for the selected media components.

    Usage:
        installer = MediaSuiteInstaller(
            rootfs_dir=Path('./extracted/rootfs'),
            binaries_dir=Path('./resources/binaries'),
        )
        result = await installer.apply(choices=choices, on_output=callback)
    """

    def __init__(
        self,
        rootfs_dir: Path,
        binaries_dir: Path | None = None,
        runner: AsyncRunner | None = None,
    ):
        self._rootfs = rootfs_dir
        # Default binaries location relative to the package
        self._binaries_dir = binaries_dir or Path(__file__).parent.parent.parent / "resources" / "binaries"
        self._runner = runner or AsyncRunner(default_timeout=60.0, sudo=True)

    async def apply(
        self,
        choices: CustomizationChoices,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> MediaInstallResult:
        """Apply media suite selections to the rootfs.

        Args:
            choices: User's media suite selections.
            on_output: Callback for progress messages.

        Returns:
            MediaInstallResult with installation details.
        """
        result = MediaInstallResult()

        if on_output:
            on_output("stdout", "Installing media suite...")

        # Determine which components to install
        selected = self._get_selected_components(choices)

        if not selected:
            if on_output:
                on_output("stdout", "  No media components selected.")
            return result

        for comp_name in selected:
            comp = MEDIA_COMPONENTS.get(comp_name)
            if not comp:
                result.warnings.append(f"Unknown component: {comp_name}")
                continue

            if on_output:
                on_output("stdout", f"  Installing {comp['description']}...")

            # Install binaries
            bins_ok = await self._install_binaries(
                comp["binaries"], result=result, on_output=on_output,
            )

            if not bins_ok:
                result.warnings.append(
                    f"{comp_name}: Binaries not found in {self._binaries_dir}. "
                    f"Component will be non-functional."
                )
                result.skipped.append(comp_name)
                continue

            # Generate config
            if comp["config_template"]:
                await self._generate_config(
                    comp_name=comp_name,
                    template_name=comp["config_template"],
                    dest_path=comp["config_dest"],
                    choices=choices,
                    result=result,
                    on_output=on_output,
                )

            # Install init script
            await self._install_init_script(
                comp_name=comp_name,
                init_name=comp["init_name"],
                binaries=comp["binaries"],
                result=result,
                on_output=on_output,
            )

            result.installed.append(comp_name)
            if on_output:
                on_output("stdout", f"  ✅ {comp['description']} installed")

        # Ensure audio device permissions
        await self._setup_audio_permissions(result=result)

        if on_output:
            on_output(
                "stdout",
                f"✅ Media suite: {result.total_installed} components installed",
            )

        return result

    # ── Component Selection ──────────────────────────────────────────────────

    def _get_selected_components(self, choices: CustomizationChoices) -> list[str]:
        """Get list of selected media components from choices."""
        selected = []
        if choices.install_airplay:
            selected.append("airplay")
        if choices.install_dlna:
            selected.append("dlna")
        if choices.install_spotify:
            selected.append("spotify")
        if choices.install_squeezelite:
            selected.append("squeezelite")
        if choices.install_snapcast:
            selected.append("snapcast")
        return selected

    # ── Binary Installation ──────────────────────────────────────────────────

    async def _install_binaries(
        self,
        binary_names: list[str],
        *,
        result: MediaInstallResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> bool:
        """Copy pre-compiled ARM64 binaries into the rootfs.

        Returns True if ALL binaries were found and installed.
        """
        all_ok = True
        dest_bin = self._rootfs / "usr" / "bin"
        dest_bin.mkdir(parents=True, exist_ok=True)

        for binary in binary_names:
            src = self._binaries_dir / binary
            dst = dest_bin / binary

            if not src.exists():
                logger.warning("Binary not found: %s", src)
                all_ok = False
                continue

            try:
                shutil.copy2(src, dst)
                dst.chmod(0o755 | stat.S_IEXEC)
                logger.debug("Installed binary: %s", binary)
            except Exception as exc:
                logger.error("Failed to install %s: %s", binary, exc)
                all_ok = False

        return all_ok

    # ── Config Generation ────────────────────────────────────────────────────

    async def _generate_config(
        self,
        comp_name: str,
        template_name: str,
        dest_path: str,
        choices: CustomizationChoices,
        *,
        result: MediaInstallResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Generate a configuration file from a Jinja2 template.

        Falls back to a minimal default config if the template is missing.
        """
        template_path = Path(__file__).parent.parent.parent / "resources" / "configs" / template_name
        dest = self._rootfs / dest_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        if template_path.exists():
            try:
                import jinja2
                template_str = template_path.read_text()
                template = jinja2.Template(template_str)
                config_content = template.render(
                    device_name=choices.media_device_name or "LX06-Speaker",
                    spotify_username=choices.spotify_username or "",
                    spotify_password=choices.spotify_password or "",
                    audio_output=choices.audio_output or "default",
                )
                dest.write_text(config_content)
                logger.debug("Generated config: %s", dest_path)
                return
            except ImportError:
                logger.warning("Jinja2 not available, using default config")
            except Exception as exc:
                logger.warning("Template rendering failed for %s: %s", template_name, exc)

        # Fallback: write a minimal default config
        defaults = {
            "shairport-sync.conf.j2": "general = {\n  name = \"LX06-Speaker\";\n};\n",
            "upmpdcli.conf.j2": "upnpiface = \"\"\npidfile = /var/run/upmpdcli.pid\n",
            "librespot.toml.j2": "[credentials]\nusername = \"\"\npassword = \"\"\n",
        }
        default_content = defaults.get(template_name, f"# Auto-generated config for {comp_name}\n")
        dest.write_text(default_content)
        logger.debug("Wrote default config: %s", dest_path)

    # ── Init Script Installation ─────────────────────────────────────────────

    async def _install_init_script(
        self,
        comp_name: str,
        init_name: str,
        binaries: list[str],
        *,
        result: MediaInstallResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Create an init.d script for the media component.

        Uses a standard start/stop daemon pattern.
        """
        init_dir = self._rootfs / "etc" / "init.d"
        init_dir.mkdir(parents=True, exist_ok=True)
        init_file = init_dir / init_name

        primary_binary = binaries[0]

        script = f"""#!/bin/sh
# Auto-generated init script for {comp_name}
# Installed by LX06 Flash Tool

DAEMON=/usr/bin/{primary_binary}
PIDFILE=/var/run/{comp_name}.pid
NAME={comp_name}

case "$1" in
    start)
        echo "Starting $NAME..."
        start-stop-daemon -S -b -m -p $PIDFILE -x $DAEMON
        ;;
    stop)
        echo "Stopping $NAME..."
        start-stop-daemon -K -p $PIDFILE -x $DAEMON
        rm -f $PIDFILE
        ;;
    restart)
        $0 stop
        sleep 1
        $0 start
        ;;
    *)
        echo "Usage: $0 {{start|stop|restart}}"
        exit 1
        ;;
esac
"""

        init_file.write_text(script)
        init_file.chmod(0o755)
        logger.debug("Installed init script: %s", init_name)

    # ── Audio Permissions ────────────────────────────────────────────────────

    async def _setup_audio_permissions(
        self,
        *,
        result: MediaInstallResult,
    ) -> None:
        """Ensure audio device nodes and permissions are configured."""
        # Create audio group if it doesn't exist in the rootfs
        group_file = self._rootfs / "etc" / "group"
        if group_file.exists():
            content = group_file.read_text()
            if "audio:" not in content:
                with open(group_file, "a") as f:
                    f.write("audio:x:29:root\n")

        # Ensure /dev/snd device directory is referenced
        dev_snd = self._rootfs / "dev" / "snd"
        dev_snd.mkdir(parents=True, exist_ok=True)
