"""Environment screen — dependency checking, installation, and tool download."""

from __future__ import annotations

import logging
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Input, Markdown, RichLog, Static

from lx06_tool.app import LX06App
from lx06_tool.modules.environment import (
    OSInfo,
    DependencyStatus,
    detect_os,
    check_dependencies,
    install_dependencies,
    verify_docker,
    install_udev_rules,
)
from lx06_tool.ui.widgets.copy_log import CopyLogMixin
from lx06_tool.utils.debug_log import RichLogSink, register_sink, unregister_sink

logger = logging.getLogger(__name__)


class EnvironmentScreen(CopyLogMixin, Screen):
    """Environment setup screen — checks and installs dependencies."""

    DEFAULT_CSS = """
    EnvironmentScreen {
        padding: 1 2;
    }
    #env-log {
        height: 1fr;
        border: solid $primary;
        margin: 1 0;
    }
    #env-actions {
        height: auto;
        align: center middle;
        padding: 1 0;
    }
    #sudo-row {
        height: 3;
        margin: 0 0 1 0;
        align: center middle;
    }
    #sudo-row Static {
        width: auto;
        padding: 0 1;
    }
    #sudo-input {
        width: 30;
    }
    """

    check_started: reactive[bool] = reactive(False)
    check_complete: reactive[bool] = reactive(False)
    install_complete: reactive[bool] = reactive(False)
    tools_downloaded: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Markdown("## Phase 1: Environment Setup\nChecking your system for required dependencies...")
        yield RichLog(id="env-log", highlight=True, markup=True)
        with Horizontal(id="sudo-row"):
            yield Static("\U0001f512 Sudo Password:")
            yield Input(
                placeholder="Enter your sudo password...",
                password=True,
                id="sudo-input",
            )
        with Vertical(id="env-actions"):
            yield Button("\U0001f4cb Copy Log", variant="default", id="copy-btn")
            yield Button("Check Environment", variant="primary", id="check-btn")
            yield Button("Install Missing", variant="warning", id="install-btn", disabled=True)
            yield Button("Download Tools", variant="primary", id="download-btn", disabled=True)
            yield Button("Continue", variant="success", id="continue-btn", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write(
            "Ready to check environment. Click 'Check Environment' to start.\n"
            "Enter your sudo password above if packages need to be installed."
        )
        self._debug_sink = RichLogSink(log)
        register_sink(self._debug_sink)

    def on_unmount(self) -> None:
        unregister_sink(self._debug_sink)

    def _get_sudo_password(self) -> str:
        """Get the sudo password from the input field and sync to app."""
        try:
            pw = self.query_one("#sudo-input", Input).value.strip()
        except Exception:
            pw = ""
        # Sync to app-level SudoContext so other screens can use it
        app = self.app
        if isinstance(app, LX06App):
            app.sudo_password = pw
        return pw

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy-btn":
            try:
                self.copy_log_to_clipboard()
            except RuntimeError as exc:
                self.query_one(RichLog).write(f"\n[yellow]{exc}[/]")
            return
        if event.button.id == "check-btn":
            self.app.run_worker(self._run_check())
        elif event.button.id == "install-btn":
            self.app.run_worker(self._run_install())
        elif event.button.id == "download-btn":
            self.app.run_worker(self._run_download_tools())
        elif event.button.id == "continue-btn":
            self.app.run_worker(self._go_next())

    async def _run_check(self) -> None:
        """Run environment dependency check using standalone functions."""
        self.check_started = True
        log = self.query_one(RichLog)
        log.clear()
        log.write("[bold blue]Checking environment...[/]")

        app = self.app
        if not isinstance(app, LX06App):
            log.write("[red]Error: Invalid app instance[/]")
            return

        try:
            # Step 1: Detect OS
            os_info = detect_os()
            app.os_info = os_info
            log.write(f"\n[bold]OS:[/] {os_info.name} {os_info.version}")
            log.write(f"[bold]Family:[/] {os_info.family}")
            log.write(f"[bold]Package Manager:[/] {os_info.pkg_manager}")
            if os_info.aur_helper:
                log.write(f"[bold]AUR Helper:[/] {os_info.aur_helper}")

            # Step 2: Check dependencies
            deps = check_dependencies(os_info)
            app.dep_statuses = deps
            missing = [d for d in deps if not d.installed]
            installed = [d for d in deps if d.installed]

            log.write(f"\n[bold]Dependencies:[/] {len(installed)} installed, {len(missing)} missing")

            if missing:
                log.write("\n[bold yellow]Missing packages:[/]")
                for d in missing:
                    log.write(f"  \u2717 {d.package_name} ({d.logical_name})")
                    if d.notes:
                        for note_line in d.notes.split("\n"):
                            log.write(f"    [dim]{note_line}[/]")
                self.query_one("#install-btn", Button).disabled = False
            else:
                log.write("\n[bold green]All dependencies satisfied![/]")
                self.query_one("#install-btn", Button).disabled = True

            if installed:
                log.write("\n[green]Installed:[/]")
                for d in installed:
                    log.write(f"  \u2713 {d.package_name} ({d.logical_name})")

            # Step 3: Check Docker
            try:
                await verify_docker(os_info)
                app.docker_ok = True
                log.write("\n[green]Docker: OK[/]")
            except Exception as e:
                app.docker_ok = False
                log.write(f"\n[yellow]Docker: {e}[/]")

            # Step 4: Check AML tool
            tools_dir = Path(app.config.tools_dir)
            aml_path = tools_dir / "aml-flash-tool" / "update"
            # Also check common locations
            if not aml_path.exists():
                aml_path = Path("/usr/local/bin/aml-flash-tool/update")
            if not aml_path.exists():
                # Check config path
                aml_path = app.config.update_exe_path

            if aml_path and Path(aml_path).exists():
                log.write(f"\n[green]AML Tool: OK[/] ({aml_path})")
                app.config.update_exe_path = Path(aml_path)
                self.tools_downloaded = True
            else:
                log.write("\n[yellow]AML Tool: Not downloaded yet[/]")
                self.query_one("#download-btn", Button).disabled = False

            # Enable continue only when everything is ready
            can_continue = (
                len(missing) == 0
                and self.tools_downloaded
            )
            self.query_one("#continue-btn", Button).disabled = not can_continue

            self.check_complete = True
            app.update_status("Environment check complete")

        except Exception as exc:
            log.write(f"\n[bold red]Error:[/] {exc}")
            logger.error("Environment check failed: %s", exc, exc_info=True)

    async def _run_install(self) -> None:
        """Install missing dependencies using the new standalone functions."""
        log = self.query_one(RichLog)

        app = self.app
        if not isinstance(app, LX06App):
            return

        os_info = app.os_info
        if os_info is None:
            log.write("\n[red]Run 'Check Environment' first![/]")
            return

        # Get missing deps
        missing = [d for d in app.dep_statuses if not d.installed]
        if not missing:
            log.write("\n[green]No missing packages to install.[/]")
            return

        log.write(f"\n[bold blue]Installing {len(missing)} missing packages...[/]")
        for d in missing:
            log.write(f"  Installing: {d.package_name}")

        try:
            password = self._get_sudo_password()
            await install_dependencies(missing, os_info, sudo_password=password)
            log.write("\n[green]Dependencies installed successfully.[/]")
            self.install_complete = True

            # Re-check
            deps = check_dependencies(os_info)
            app.dep_statuses = deps
            still_missing = [d for d in deps if not d.installed]

            if not still_missing:
                log.write("\n[bold green]All dependencies now satisfied![/]")
                self.query_one("#install-btn", Button).disabled = True
                if self.tools_downloaded:
                    self.query_one("#continue-btn", Button).disabled = False
            else:
                log.write(f"\n[yellow]{len(still_missing)} packages still missing:[/]")
                for d in still_missing:
                    log.write(f"  \u2717 {d.package_name}")

            app.update_status("Installation complete")

        except Exception as exc:
            log.write(f"\n[bold red]Install error:[/] {exc}")
            logger.error("Install failed: %s", exc, exc_info=True)

    async def _run_download_tools(self) -> None:
        """Download aml-flash-tool from Radxa."""
        log = self.query_one(RichLog)
        app = self.app

        if not isinstance(app, LX06App):
            return

        log.write("\n[bold blue]Downloading aml-flash-tool...[/]")

        try:
            from lx06_tool.utils.downloader import AsyncDownloader
            from lx06_tool.constants import AML_FLASH_TOOL_REPO, UPDATE_EXE_RELPATH

            tools_dir = Path(app.config.tools_dir)
            tools_dir.mkdir(parents=True, exist_ok=True)
            aml_dir = tools_dir / "aml-flash-tool"

            if not aml_dir.exists():
                log.write(f"  Cloning {AML_FLASH_TOOL_REPO} (branch=master)...")
                dl = AsyncDownloader()
                await dl.clone_git_repo(
                    AML_FLASH_TOOL_REPO, aml_dir, branch="master"
                )
                log.write("  [green]Clone complete.[/]")
            else:
                log.write("  [dim]AML tool directory already exists, skipping clone.[/]")

            # Verify the binary exists
            update_bin = aml_dir / UPDATE_EXE_RELPATH
            if update_bin.exists():
                update_bin.chmod(0o755)
                app.config.update_exe_path = update_bin
                app.config.save()
                log.write(f"  [green]AML tool ready:[/] {update_bin}")
                self.tools_downloaded = True
                self.query_one("#download-btn", Button).disabled = True

                # Enable continue if all deps are met
                missing = [d for d in app.dep_statuses if not d.installed]
                if not missing:
                    self.query_one("#continue-btn", Button).disabled = False
            else:
                log.write(f"  [yellow]Warning: Binary not found at {update_bin}[/]")
                log.write("  [dim]You may need to build it manually.[/]")

            app.update_status("Tool download complete")

        except Exception as exc:
            log.write(f"\n[bold red]Download error:[/] {exc}")
            logger.error("Download failed: %s", exc, exc_info=True)

    async def _go_next(self) -> None:
        """Proceed to the next screen."""
        app = self.app
        if isinstance(app, LX06App):
            await app.on_environment_done(self.check_complete and self.tools_downloaded)
