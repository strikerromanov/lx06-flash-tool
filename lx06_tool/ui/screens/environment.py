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

logger = logging.getLogger(__name__)


class EnvironmentScreen(Screen):
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
            yield Static("🔒 Sudo Password:")
            yield Input(
                placeholder="Enter your sudo password...",
                password=True,
                id="sudo-input",
            )
        with Vertical(id="env-actions"):
            yield Button("Check Environment", variant="primary", id="check-btn")
            yield Button("Install Missing", variant="warning", id="install-btn", disabled=True)
            yield Button("Download Tools", variant="primary", id="download-btn", disabled=True)
            yield Button("Continue", variant="success", id="continue-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one(RichLog).write(
            "Ready to check environment. Click 'Check Environment' to start.\n"
            "Enter your sudo password above if packages need to be installed."
        )

    def _get_sudo_password(self) -> str:
        """Get the sudo password from the input field."""
        try:
            return self.query_one("#sudo-input", Input).value.strip()
        except Exception:
            return ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check-btn":
            self.app.run_worker(self._run_check())
        elif event.button.id == "install-btn":
            self.app.run_worker(self._run_install())
        elif event.button.id == "download-btn":
            self.app.run_worker(self._run_download_tools())
        elif event.button.id == "continue-btn":
            self.app.run_worker(self._go_next())

    async def _run_check(self) -> None:
        """Run environment dependency check."""
        self.check_started = True
        log = self.query_one(RichLog)
        log.clear()
        log.write("[bold blue]Checking environment...[/]")

        app = self.app
        if not isinstance(app, LX06App):
            log.write("[red]Error: Invalid app instance[/]")
            return

        try:
            mgr = app.get_env_manager(sudo_password=self._get_sudo_password())
            tools_dir = Path(app.config.tools_dir)
            report = await mgr.check(tools_dir=tools_dir)

            log.write(f"\n[bold]OS:[/] {report.os_info.name} {report.os_info.version}")
            log.write(f"[bold]Package Manager:[/] {report.pkg_manager or 'Not found'}")

            if report.missing_packages:
                log.write(f"\n[bold yellow]Missing packages:[/]")
                for pkg in report.missing_packages:
                    log.write(f"  - {pkg}")
                self.query_one("#install-btn", Button).disabled = False
            else:
                log.write("\n[bold green]All dependencies satisfied![/]")
                self.query_one("#install-btn", Button).disabled = True

            if report.docker_available:
                log.write("\n[green]Docker: OK[/]")
            else:
                log.write("\n[yellow]Docker: Not available (needed for isolated builds)[/]")

            if report.aml_tool_installed:
                log.write(f"\n[green]AML Tool: OK[/] ({report.aml_tool_path})")
                self.tools_downloaded = True
            else:
                log.write("\n[yellow]AML Tool: Not downloaded yet[/]")
                self.query_one("#download-btn", Button).disabled = False

            # Enable continue only when everything is ready
            can_continue = report.all_ready or (
                len(report.missing_packages) == 0 and self.tools_downloaded
            )
            self.query_one("#continue-btn", Button).disabled = not can_continue

            self.check_complete = True
            app.update_status("Environment check complete")

        except Exception as exc:
            log.write(f"\n[bold red]Error:[/] {exc}")
            logger.error("Environment check failed: %s", exc, exc_info=True)

    async def _run_install(self) -> None:
        """Install missing dependencies using sudo password."""
        log = self.query_one(RichLog)
        sudo_pass = self._get_sudo_password()

        if not sudo_pass:
            log.write("\n[bold red]Please enter your sudo password above first![/]")
            return

        log.write("\n[bold blue]Installing missing dependencies (using sudo)...[/]")
        app = self.app
        if not isinstance(app, LX06App):
            return

        try:
            mgr = app.get_env_manager(sudo_password=sudo_pass)
            report = await mgr.check()

            if report.missing_packages:
                await mgr.install_dependencies(
                    report.pkg_manager,
                    report.missing_packages,
                    on_output=lambda lvl, line: log.write(line),
                )
                log.write("\n[green]Dependencies installed.[/]")

            # Re-check
            report = await mgr.check()
            if report.all_ready or len(report.missing_packages) == 0:
                log.write("\n[bold green]All dependencies now satisfied![/]")
                self.query_one("#install-btn", Button).disabled = True
                if self.tools_downloaded:
                    self.query_one("#continue-btn", Button).disabled = False
            else:
                log.write("\n[yellow]Some dependencies still missing.[/]")

            self.install_complete = True
            app.update_status("Installation complete")

        except Exception as exc:
            log.write(f"\n[bold red]Install error:[/] {exc}")
            logger.error("Install failed: %s", exc, exc_info=True)

    async def _run_download_tools(self) -> None:
        """Download aml-flash-tool from Radxa."""
        log = self.query_one(RichLog)
        log.write("\n[bold blue]Downloading aml-flash-tool...[/]")
        app = self.app
        if not isinstance(app, LX06App):
            return

        try:
            mgr = app.get_env_manager(sudo_password=self._get_sudo_password())
            tools_dir = Path(app.config.tools_dir)

            update_path = await mgr.download_aml_tool(
                tools_dir,
                on_output=lambda lvl, line: log.write(line),
            )

            # Save the path to app config so other modules can find it
            app.config.update_exe_path = str(update_path)
            log.write(f"\n[green]AML tool downloaded to: {update_path}[/]")

            # Re-check to verify
            report = await mgr.check(tools_dir=tools_dir)
            if report.aml_tool_installed:
                log.write("[bold green]AML Tool: Verified OK[/]")
                self.tools_downloaded = True
                self.query_one("#download-btn", Button).disabled = True

                # Enable continue if deps are also met
                if not report.missing_packages:
                    self.query_one("#continue-btn", Button).disabled = False
            else:
                log.write("[yellow]AML tool downloaded but not detected in re-check.[/]")

            app.update_status("Tools downloaded")

        except Exception as exc:
            log.write(f"\n[bold red]Download error:[/] {exc}")
            logger.error("Tool download failed: %s", exc, exc_info=True)

    async def _go_next(self) -> None:
        """Proceed to USB connection screen."""
        app = self.app
        if isinstance(app, LX06App):
            await app.on_environment_done(True)
