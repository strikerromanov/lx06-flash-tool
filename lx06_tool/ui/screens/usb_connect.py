"""USB Connect screen — device handshake via USB burning mode."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Input, Markdown, RichLog, Static

from lx06_tool.app import LX06App
from lx06_tool.config import LX06Device
from lx06_tool.modules.usb_scanner import (
    handshake_loop,
    install_udev_rules,
    udev_rules_installed,
)

logger = logging.getLogger(__name__)

USB_INSTRUCTIONS = """## USB Connection

### Steps to enter USB Burning Mode:

1. **Power off** the LX06 speaker completely (unplug power)
2. **Disconnect** any USB cable from the speaker
3. **Open** the speaker case to access the PCB test pads
4. **Short** the two test pads near the USB port (use tweezers or wire)
5. **While shorting**, connect USB-A cable from PC to speaker
6. **Release** the short after 2-3 seconds
7. The speaker should enter **Amlogic USB Burning Mode** (2-second window)

The tool will automatically detect the device during the handshake window.

\u26a0\ufe0f If detection fails, try again \u2014 timing is critical (2-second window).
"""


class USBConnectScreen(Screen):
    """USB connection screen \u2014 guides user through device handshake."""

    DEFAULT_CSS = """
    USBConnectScreen { padding: 1 2; }
    #usb-log { height: 1fr; border: solid $primary; margin: 1 0; }
    #usb-actions { height: auto; align: center middle; padding: 1 0; }
    #sudo-row {
        height: 3;
        padding: 0 1;
        align: center middle;
    }
    #sudo-row Static {
        width: auto;
        margin: 0 1 0 0;
    }
    #sudo-input {
        width: 30;
    }
    """

    scanning: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Markdown(USB_INSTRUCTIONS)
        with Horizontal(id="sudo-row"):
            yield Static("\U0001f512 Sudo Password:")
            yield Input(
                placeholder="Enter your sudo password...",
                password=True,
                id="sudo-input",
            )
        yield RichLog(id="usb-log", highlight=True, markup=True)
        with Vertical(id="usb-actions"):
            yield Button("Start USB Scan", variant="primary", id="scan-btn")
            yield Button("Cancel", variant="error", id="cancel-btn", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("Ready. Enter your sudo password above, then click 'Start USB Scan'.")

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
        if event.button.id == "scan-btn":
            self.app.run_worker(self._start_scan())
        elif event.button.id == "cancel-btn":
            self.scanning = False
            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True

    async def _start_scan(self) -> None:
        """Start the USB handshake loop."""
        self.scanning = True
        self.query_one("#scan-btn", Button).disabled = True
        self.query_one("#cancel-btn", Button).disabled = False
        log = self.query_one(RichLog)

        app = self.app
        if not isinstance(app, LX06App):
            return

        # Get password (from input field or app-level storage)
        pw = self._get_sudo_password()
        if not pw:
            pw = app.sudo_password

        log.write("[bold blue]Starting USB handshake scan...[/]")
        log.write("Waiting for device in USB burning mode...")

        try:
            if not udev_rules_installed():
                log.write("[dim]Installing udev rules...[/]")
                if not pw:
                    log.write("[bold red]Error: Sudo password required to install udev rules.[/]")
                    log.write("[dim]Enter your password above and try again.[/]")
                    self.query_one("#scan-btn", Button).disabled = False
                    self.query_one("#cancel-btn", Button).disabled = True
                    self.scanning = False
                    return
                await install_udev_rules(sudo_password=pw)
                log.write("[green]udev rules installed.[/]")
            else:
                log.write("[dim]udev rules already installed.[/]")

            # Get AmlogicTool and start handshake
            tool = app.get_aml_tool()

            def on_attempt(attempt: int, elapsed: int) -> None:
                if attempt % 10 == 1:  # Log every ~1 second
                    log.write(f"  [dim]Attempt {attempt} ({elapsed}s elapsed)...[/]")

            log.write("[bold]Polling for device (120s timeout)...[/]")
            device_info = await handshake_loop(
                tool,
                timeout=120,
                on_attempt=on_attempt,
            )

            # Device detected!
            log.write("\n[bold green]Device detected![/]")
            log.write(f"  Chip: {device_info.chip or 'N/A'}")
            log.write(f"  Serial: {device_info.serial or 'N/A'}")
            log.write(f"  Firmware: {device_info.firmware_version or 'N/A'}")

            # Create LX06Device and store in app
            device = LX06Device(
                connected=True,
                serial=device_info.serial,
                chip_id=device_info.chip,
                firmware_version=device_info.firmware_version,
            )

            app.update_status("Device connected!")
            app.device = device
            await app.on_usb_connected(device)

        except Exception as exc:
            if "not identified" in str(exc).lower() or "timeout" in str(exc).lower():
                log.write(f"\n[yellow]Device not detected within timeout.[/]")
                log.write("[dim]Power-cycle the speaker and try again.[/]")
            else:
                log.write(f"\n[bold red]Error:[/] {exc}")
                logger.error("USB scan failed: %s", exc, exc_info=True)

            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True

        self.scanning = False
