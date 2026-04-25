"""Environment screen — dependency checking and installation."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Markdown, Static, RichLog

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
    """

    check_started: reactive[bool] = reactive(False)
    check_complete: reactive[bool] = reactive(False)
    install_complete: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Markdown("## Phase 1: Environment Setup\nChecking your system for required dependencies...")
        yield RichLog(id="env-log", highlight=True, markup=True)
        with Vertical(id="env-actions"):
            yield Button("Check Environment", variant="primary", id="check-btn")
            yield Button("Install Missing", variant="warning", id="install-btn", disabled=True)
            yield Button("Continue", variant="success", id="continue-btn", disabled=True)

    def on_mount(self) -> None:
        self.query_one(RichLog).write("Ready to check environment. Click 'Check Environment' to start.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check-btn":
            self.app.run_worker(self._run_check())
        elif event.button.id == "install-btn":
            self.app.run_worker(self._run_install())
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
            mgr = app.get_env_manager()
            report = await mgr.check()

            log.write(f"\n[bold]OS:[/] {report.os_info.name} {report.os_info.version}")
            log.write(f"[bold]Package Manager:[/] {report.pkg_manager or 'Not found'}")

            if report.missing_packages:
                log.write(f"\n[bold yellow]Missing packages:[/]")
                for pkg in report.missing_packages:
                    log.write(f"  - {pkg}")
                self.query_one("#install-btn", Button).disabled = False
            else:
                log.write("\n[bold green]All dependencies satisfied![/]")
                self.query_one("#continue-btn", Button).disabled = False
                self.query_one("#install-btn", Button).disabled = True

            if report.docker_available:
                log.write("\n[green]Docker: OK[/]")
            else:
                log.write("\n[yellow]Docker: Not available (needed for isolated builds)[/]")
            self.check_complete = True
            app.update_status("Environment check complete")

        except Exception as exc:
            log.write(f"\n[bold red]Error:[/] {exc}")
            logger.error("Environment check failed: %s", exc, exc_info=True)

    async def _run_install(self) -> None:
        """Install missing dependencies."""
        log = self.query_one(RichLog)
        log.write("\n[bold blue]Installing missing dependencies...[/]")
        app = self.app
        if not isinstance(app, LX06App):
            return

        try:
            mgr = app.get_env_manager()
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
            if report.all_ready:
                log.write("\n[bold green]All dependencies now satisfied![/]")
                self.query_one("#continue-btn", Button).disabled = False
                self.query_one("#install-btn", Button).disabled = True
            else:
                log.write("\n[yellow]Some dependencies still missing.[/]")

            self.install_complete = True
            app.update_status("Installation complete")

        except Exception as exc:
            log.write(f"\n[bold red]Install error:[/] {exc}")
            logger.error("Install failed: %s", exc, exc_info=True)

    async def _go_next(self) -> None:
        """Proceed to USB connection screen."""
        app = self.app
        if isinstance(app, LX06App):
            await app.on_environment_done(True)
