"""
lx06_tool/ui/screens/environment.py
-------------------------------------
Environment setup screen.

Detects OS, installs packages, clones aml-flash-tool, sets up udev rules,
and creates the update.exe symlink.
"""

from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Label, Static

from lx06_tool.constants import (
    AML_FLASH_TOOL_DIR,
    AML_FLASH_TOOL_REPO,
    DISTRO_PACKAGES,
    OLD_UDEV_RULES,
    OS_FAMILY_MAP,
    OS_LIKE_MAP,
    UDEV_RULE_LINE,
    UDEV_RULES_DEST,
    UPDATE_EXE_RELPATH,
)
from lx06_tool.ui.widgets import ActionButton, LogPanel, PasswordInput, StepProgress


# ─── OS Detection ──────────────────────────────────────────────────────────────

def _detect_os_family() -> tuple[str, str]:
    """Detect the OS family from /etc/os-release.

    Returns:
        (family, distro_name) e.g. ("debian", "Ubuntu 24.04")
    """
    release_path = Path("/etc/os-release")
    if not release_path.exists():
        return ("unknown", f"{platform.system()} (no /etc/os-release)")

    data: dict[str, str] = {}
    with open(release_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                data[key] = value.strip('"').strip("'")

    distro_id = data.get("ID", "").lower()
    distro_name = data.get("PRETTY_NAME", distro_id or "Unknown")

    # Direct match
    if distro_id in OS_FAMILY_MAP:
        return (OS_FAMILY_MAP[distro_id], distro_name)

    # Check ID_LIKE
    id_like = data.get("ID_LIKE", "").lower().split()
    for like in id_like:
        if like in OS_LIKE_MAP:
            return (OS_LIKE_MAP[like], distro_name)

    return ("unknown", distro_name)


# ─── Package install commands ──────────────────────────────────────────────────

def _get_install_command(family: str) -> tuple[list[str], list[str]] | None:
    """Get the package install command and package list for the given family.

    Returns:
        (command_prefix, package_names) or None if unsupported.
    """
    pkgs = DISTRO_PACKAGES.get(family)
    if not pkgs:
        return None

    # Collect non-None packages
    names: list[str] = []
    for val in pkgs.values():
        if val is None:
            continue
        names.extend(val.split())

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)

    if family == "debian":
        return (["apt-get", "install", "-y"] + unique, unique)
    elif family == "fedora":
        return (["dnf", "install", "-y"] + unique, unique)
    elif family == "arch":
        return (["pacman", "-S", "--needed", "--noconfirm"] + unique, unique)

    return None


class EnvironmentScreen(Screen):
    """Environment setup screen — detects OS, installs deps, clones tools."""

    DEFAULT_CSS = """
    EnvironmentScreen {
        align: center middle;
    }
    EnvironmentScreen > VerticalScroll {
        width: 80;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
    }
    EnvironmentScreen .title {
        text-align: center;
        text-style: bold;
        color: $primary;
        padding: 1 0;
    }
    EnvironmentScreen .status {
        padding: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("⚙ Environment Setup", classes="title")
            yield StepProgress(
                steps=["Detect OS", "Install Packages", "Clone Tools", "Udev Rules", "Create Symlink"],
                id="env_steps",
            )
            yield Static("", id="env_status", classes="status")
            yield PasswordInput(id="env_password")
            yield LogPanel(title="Setup Log", id="env_log")
            with Center():
                yield ActionButton(
                    label="Setup Environment",
                    variant="primary",
                    id="setup_btn",
                )
                yield ActionButton(
                    label="Continue",
                    variant="success",
                    id="continue_btn",
                    disabled=True,
                )

    def on_mount(self) -> None:
        """Initialize screen state."""
        self._setup_done = False
        self._log = self.query_one("#env_log", LogPanel)
        self._status = self.query_one("#env_status", Static)
        self._steps = self.query_one("#env_steps", StepProgress)

    def _set_status(self, text: str) -> None:
        try:
            self._status.update(text)
        except Exception:
            pass

    def on_password_input_password_submitted(self, event: PasswordInput.PasswordSubmitted) -> None:
        """Store the sudo password when submitted."""
        self.app.sudo_ctx.password = event.password  # type: ignore[attr-defined]
        self._log.write("[green]Password stored.[/green]")

    def _ensure_password(self) -> bool:
        """Ensure sudo password is available, try reading from input."""
        sudo_ctx = self.app.sudo_ctx  # type: ignore[attr-defined]
        if sudo_ctx.has_password:
            return True
        try:
            from textual.widgets import Input
            pw_input = self.query_one("#env_password PasswordInput Input", Input)
            pw = pw_input.value.strip()
            if pw:
                sudo_ctx.password = pw
                self._log.write("[green]Password captured.[/green]")
                return True
        except Exception:
            pass
        return False

    # ─── Button handlers ────────────────────────────────────────────────

    def on_action_button_pressed(self, event: ActionButton.Pressed) -> None:
        """Handle button presses."""
        btn = event.action_button
        if btn.id == "setup_btn":
            btn.set_loading(True)
            self.run_worker(self._run_setup(), exclusive=True)
        elif btn.id == "continue_btn" and self._setup_done:
            self.app.push_screen("usb_connect")

    # ─── Setup Pipeline ─────────────────────────────────────────────────

    async def _run_setup(self) -> None:
        """Run the full environment setup pipeline."""
        if not self._ensure_password():
            self._log.write("[red]Please enter your sudo password first.[/red]")
            self._set_status("❌ Enter your sudo password first")
            self.query_one("#setup_btn", ActionButton).set_loading(False)
            return

        try:
            family, distro_name = _detect_os_family()
            self._set_status(f"Detected: {distro_name} (family: {family})")

            if family == "unknown":
                self._log.write(f"[red]Unsupported OS: {distro_name}[/red]")
                self._log.write("[yellow]Supported: Debian/Ubuntu, Fedora/RHEL, Arch/Manjaro[/yellow]")
                self._set_status("❌ Unsupported operating system")
                self.query_one("#setup_btn", ActionButton).set_loading(False)
                return

            # Step 1: Detect OS (already done)
            self._steps.set_current(0)
            self._log.write(f"[green]✓ OS detected: {distro_name} ({family})[/green]")
            await asyncio.sleep(0.3)

            # Step 2: Install packages
            self._steps.set_current(1)
            await self._install_packages(family)

            # Step 3: Clone aml-flash-tool
            self._steps.set_current(2)
            await self._clone_tools()

            # Step 4: Udev rules
            self._steps.set_current(3)
            await self._setup_udev()

            # Step 5: Create symlink
            self._steps.set_current(4)
            await self._create_symlink()

            # Done
            self._steps.set_current(5)  # past last = all done
            self._setup_done = True
            self._set_status("✅ Environment setup complete!")
            self._log.write("[bold green]All setup steps completed successfully![/bold green]")
            self.query_one("#setup_btn", ActionButton).set_loading(False)
            self.query_one("#continue_btn", ActionButton).set_enabled(True)

        except Exception as exc:
            self._log.write(f"[red]Setup failed: {exc}[/red]")
            self._set_status(f"❌ Setup failed: {exc}")
            self.query_one("#setup_btn", ActionButton).set_loading(False)

    async def _install_packages(self, family: str) -> None:
        """Install required packages for the detected OS family."""
        result = _get_install_command(family)
        if result is None:
            self._log.write("[yellow]⚠ No packages to install for this OS family.[/yellow]")
            return

        cmd, pkg_names = result
        self._log.write(f"Installing packages: {', '.join(pkg_names)}")
        self._set_status(f"Installing packages: {', '.join(pkg_names)}")

        sudo_ctx = self.app.sudo_ctx  # type: ignore[attr-defined]
        try:
            sr = await sudo_ctx.sudo_run(cmd, timeout=120)
            if sr.ok:
                self._log.write("[green]✓ Packages installed successfully.[/green]")
            else:
                self._log.write(f"[yellow]⚠ Package install returned exit code {sr.returncode}[/yellow]")
                self._log.write(f"  Output: {sr.output[:500]}")
        except Exception as exc:
            self._log.write(f"[red]Package install error: {exc}[/red]")
            # Non-fatal — continue setup
            self._log.write("[yellow]Continuing setup — packages may already be installed.[/yellow]")

    async def _clone_tools(self) -> None:
        """Clone aml-flash-tool repository."""
        config = self.app.config  # type: ignore[attr-defined]
        tools_dir = config.tools_dir
        aml_dir = tools_dir / AML_FLASH_TOOL_DIR

        self._set_status(f"Cloning aml-flash-tool to {aml_dir}...")
        self._log.write(f"Cloning {AML_FLASH_TOOL_REPO}...")

        try:
            from lx06_tool.utils.downloader import AsyncDownloader
            await AsyncDownloader.clone_git_repo(
                repo_url=AML_FLASH_TOOL_REPO,
                dest_dir=aml_dir,
                branch="master",
                depth=1,
            )
            self._log.write("[green]✓ aml-flash-tool cloned successfully.[/green]")
        except Exception as exc:
            # If already exists, that's fine
            if aml_dir.exists() and (aml_dir / ".git").exists():
                self._log.write("[green]✓ aml-flash-tool already present (pull attempted).[/green]")
            else:
                self._log.write(f"[red]Failed to clone aml-flash-tool: {exc}[/red]")
                raise

    async def _setup_udev(self) -> None:
        """Install udev rules for Amlogic USB device access."""
        self._set_status("Setting up udev rules...")
        self._log.write("Installing udev rules for USB device access...")
        sudo_ctx = self.app.sudo_ctx  # type: ignore[attr-defined]

        try:
            # Write new udev rule
            sr = await sudo_ctx.sudo_write_file(
                UDEV_RULE_LINE + "\n", UDEV_RULES_DEST, timeout=15
            )
            if sr.ok:
                self._log.write(f"[green]✓ Udev rule written to {UDEV_RULES_DEST}[/green]")
            else:
                self._log.write(f"[yellow]⚠ Udev rule write returned {sr.returncode}[/yellow]")

            # Remove old rules
            for old_rule in OLD_UDEV_RULES:
                if Path(old_rule).exists():
                    sr = await sudo_ctx.sudo_run(["rm", "-f", old_rule], timeout=10)
                    if sr.ok:
                        self._log.write(f"  Removed old rule: {old_rule}")

            # Reload udev rules
            sr = await sudo_ctx.sudo_run(
                ["udevadm", "control", "--reload-rules"], timeout=10
            )
            if sr.ok:
                self._log.write("[green]✓ Udev rules reloaded.[/green]")

            sr = await sudo_ctx.sudo_run(["udevadm", "trigger"], timeout=10)
            if sr.ok:
                self._log.write("[green]✓ Udev trigger executed.[/green]")

            self._log.write(
                "[yellow]Note: If USB detection fails later, reboot your computer "
                "for udev rules to take full effect.[/yellow]"
            )

        except Exception as exc:
            self._log.write(f"[yellow]⚠ Udev setup warning: {exc}[/yellow]")
            self._log.write("[yellow]USB detection may require running with sudo or rebooting.[/yellow]")

    async def _create_symlink(self) -> None:
        """Create update.exe symlink in aml-flash-tool tools directory."""
        config = self.app.config  # type: ignore[attr-defined]
        tools_dir = config.tools_dir
        aml_dir = tools_dir / AML_FLASH_TOOL_DIR

        # The update binary lives at aml-flash-tool/tools/linux-x86/update
        linux_tools = aml_dir / "tools" / "linux-x86"
        update_src = linux_tools / "update"
        update_exe = linux_tools / "update.exe"

        self._set_status("Creating update.exe symlink...")

        if not update_src.exists():
            self._log.write(f"[yellow]⚠ update binary not found at {update_src}[/yellow]")
            self._log.write("[yellow]Symlink creation skipped — will resolve at flash time.[/yellow]")
            return

        try:
            # Create symlink: update.exe → update
            if update_exe.is_symlink():
                update_exe.unlink()
            os.symlink("update", str(update_exe))
            self._log.write(f"[green]✓ Symlink created: {update_exe} → update[/green]")
        except OSError as exc:
            # If we can't create symlink (permissions), try with sudo
            self._log.write(f"Symlink creation failed ({exc}), trying with sudo...")
            try:
                sudo_ctx = self.app.sudo_ctx  # type: ignore[attr-defined]
                sr = await sudo_ctx.sudo_run(
                    ["ln", "-sf", "update", str(update_exe)], timeout=10
                )
                if sr.ok:
                    self._log.write("[green]✓ Symlink created with sudo.[/green]")
                else:
                    self._log.write(f"[yellow]⚠ Sudo symlink returned {sr.returncode}[/yellow]")
            except Exception as exc2:
                self._log.write(f"[yellow]⚠ Symlink creation error: {exc2}[/yellow]")
