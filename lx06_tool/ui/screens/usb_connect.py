"""USB Connect screen — device handshake via USB burning mode."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Markdown, RichLog

from lx06_tool.app import LX06App
from lx06_tool.config import LX06Device

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

⚠️ If detection fails, try again — timing is critical (2-second window).
"""


class USBConnectScreen(Screen):
    """USB connection screen — guides user through device handshake."""

    DEFAULT_CSS = """
    USBConnectScreen { padding: 1 2; }
    #usb-log { height: 1fr; border: solid $primary; margin: 1 0; }
    #usb-actions { height: auto; align: center middle; padding: 1 0; }
    """

    scanning: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Markdown(USB_INSTRUCTIONS)
        yield RichLog(id="usb-log", highlight=True, markup=True)
        with Vertical(id="usb-actions"):
            yield Button("Start USB Scan", variant="primary", id="scan-btn")
            yield Button("Cancel", variant="error", id="cancel-btn", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("Ready. Follow the instructions above, then click 'Start USB Scan'.")

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

        log.write("[bold blue]Starting USB handshake scan...[/]")
        log.write("Waiting for device in USB burning mode...")

        async def on_status(msg: str) -> None:
            log.write(f"  {msg}")

        try:
            scanner = app.get_usb_scanner()

            # Install udev rules first
            log.write("[dim]Installing udev rules...[/]")
            await scanner.install_udev_rules(on_status=on_status)

            # Start handshake loop
            device = await scanner.wait_for_device(
                on_status=on_status,
                on_output=lambda s, l: log.write(f"  [{s}] {l}"),
            )

            if device:
                log.write(f"\n[bold green]Device detected![/]")
                log.write(f"  Chip ID: {device.chip_id or 'N/A'}")
                log.write(f"  Serial: {device.serial or 'N/A'}")
                app.update_status("Device connected!")
                app.device = device
                await app.on_usb_connected(device)
            else:
                log.write("\n[yellow]Scan cancelled or timed out.[/]")
                self.query_one("#scan-btn", Button).disabled = False
                self.query_one("#cancel-btn", Button).disabled = True

        except Exception as exc:
            log.write(f"\n[bold red]Error:[/] {exc}")
            logger.error("USB scan failed: %s", exc, exc_info=True)
            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True

        self.scanning = False
