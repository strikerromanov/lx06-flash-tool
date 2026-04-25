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
    test_usb_detection,
    udev_rules_installed,
)
from lx06_tool.ui.widgets.copy_log import CopyLogMixin
from lx06_tool.utils.debug_log import RichLogSink, register_sink, unregister_sink

logger = logging.getLogger(__name__)

USB_INSTRUCTIONS = """## \U0001f50c USB Connection — Burning Mode

### How to enter USB Burning Mode on the LX06:

1. **Power off** the speaker completely (unplug power cable)
2. **Disconnect** any USB cable from the speaker
3. **Open** the speaker case to access the PCB test pads
4. **Short** the two test pads near the USB port (use tweezers or wire)
5. **While holding the short**, plug USB-A cable from PC to speaker
6. **Release** the short after 2-3 seconds
7. The tool will automatically detect the device

### Detection Strategy:
- **Phase 1**: Fast kernel-level scan via sysfs (50ms intervals)
- **Phase 2**: Amlogic `update identify` handshake

\u26a0\ufe0f The bootloader window is only **~2 seconds** — the tool uses ultra-fast
polling to catch it. If detection fails, power-cycle and try again.

\U0001f4a1 **Tip**: Enter your sudo password below — needed for USB permissions on CachyOS.
"""


def _status_icon(ok: bool) -> str:
    """Return a green check or red X icon."""
    return "\u2705" if ok else "\u274c"


class USBConnectScreen(CopyLogMixin, Screen):
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
            yield Button("\U0001f4cb Copy Log", variant="default", id="copy-btn")
            yield Button("\U0001f9ea Run Diagnostics", variant="default", id="diag-btn")
            yield Button("Start USB Scan", variant="primary", id="scan-btn")
            yield Button("Cancel", variant="error", id="cancel-btn", disabled=True)

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("Ready. Enter your sudo password above, then click 'Start USB Scan'.")
        log.write("[dim]The sudo password enables USB access and udev rule installation.[/]")
        log.write("[dim]Click 'Run Diagnostics' to check USB setup before scanning.[/]")
        # Register RichLog as a debug sink so all debug messages appear here
        self._debug_sink = RichLogSink(log)
        register_sink(self._debug_sink)

    def on_unmount(self) -> None:
        # Unregister our debug sink when leaving this screen
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
        if event.button.id == "diag-btn":
            self.app.run_worker(self._run_diagnostics())
        elif event.button.id == "scan-btn":
            self.app.run_worker(self._start_scan())
        elif event.button.id == "cancel-btn":
            self.scanning = False
            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True

    # ─── Diagnostics ────────────────────────────────────────────────────────

    async def _run_diagnostics(self) -> None:
        """Run USB diagnostics and display pre-flight checklist."""
        log = self.query_one(RichLog)
        log.clear()

        app = self.app
        if not isinstance(app, LX06App):
            return

        pw = self._get_sudo_password()
        log.write("[bold blue]\U0001f9ea Running USB Diagnostics...[/]\n")

        # Get the update exe path from config if available
        exe_path = None
        if app.config.update_exe_path:
            exe_path = str(app.config.update_exe_path)

        diag = await test_usb_detection(
            update_exe_path=exe_path,
            sudo_password=pw,
        )

        # ── Pre-flight checklist ─────────────────────────────────────────
        log.write("[bold]Pre-flight Checklist:[/]")
        log.write(
            f"  {_status_icon(diag['sysfs_available'])} "
            f"sysfs available (/sys/bus/usb/devices)"
        )
        log.write(
            f"  {_status_icon(diag['lsusb_installed'])} "
            f"lsusb installed"
        )
        log.write(
            f"  {_status_icon(diag['update_exe_found'])} "
            f"update binary found"
            + (f" ({diag['update_exe_path']})" if diag['update_exe_path'] else "")
        )
        log.write(
            f"  {_status_icon(diag['update_exe_executable'])} "
            f"update binary is executable"
        )
        log.write(
            f"  {_status_icon(diag['libusb_available'])} "
            f"libusb available"
            + (f" ({diag['libusb_detail']})" if diag['libusb_detail'] else "")
        )
        log.write(
            f"  {_status_icon(diag['udev_rules_installed'])} "
            f"udev rules installed ({diag['udev_rules_path']})"
        )
        log.write(
            f"  {_status_icon(diag['device_present_sysfs'] or diag['device_present_lsusb'])} "
            f"Device currently visible (USB)"
        )

        # ── Binary test output ────────────────────────────────────────────
        if diag['update_exe_test_output']:
            log.write(f"\n[bold]Binary Test:[/]")
            log.write(f"  [dim]{diag['update_exe_test_output']}[/]")

        # ── Old rules warning ─────────────────────────────────────────────
        if diag['old_rules_found']:
            log.write(f"\n[yellow]\u26a0 Old conflicting udev rules found:[/]")
            for rule in diag['old_rules_found']:
                log.write(f"  [yellow]\u274c {rule}[/]")
            log.write("  [dim]These will be cleaned up when you click 'Start USB Scan'.[/]")

        # ── udev rules content ────────────────────────────────────────────
        if diag['udev_rules_content']:
            log.write(f"\n[bold]Current udev rule:[/]")
            for line in diag['udev_rules_content'].splitlines():
                log.write(f"  [dim]{line}[/]")

        # ── Issues ────────────────────────────────────────────────────────
        if diag['issues']:
            log.write(f"\n[bold red]Issues ({len(diag['issues'])}):[/]")
            for issue in diag['issues']:
                log.write(f"  \u274c {issue}")

        # ── Overall status ────────────────────────────────────────────────
        if diag['ready_to_scan']:
            log.write(
                "\n[bold green]\u2705 All checks passed — ready to scan![/]"
            )
        else:
            log.write(
                "\n[bold yellow]\u26a0 Some checks failed — see issues above.[/]"
            )
            log.write(
                "[dim]Fix the issues above, then try 'Start USB Scan'.[/]"
            )

    # ─── USB Scan ───────────────────────────────────────────────────────────

    async def _start_scan(self) -> None:
        """Start the two-phase USB handshake loop."""
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

        # ── Pre-flight diagnostics (quick) ────────────────────────────────
        exe_path = None
        if app.config.update_exe_path:
            exe_path = str(app.config.update_exe_path)
        diag = await test_usb_detection(
            update_exe_path=exe_path,
            sudo_password=pw,
        )

        # Show critical issues
        critical_issues = [
            i for i in diag['issues']
            if 'Old conflicting' not in i
        ]
        if critical_issues:
            log.write("[bold red]Pre-flight check failed:[/]")
            for issue in critical_issues:
                log.write(f"  \u274c {issue}")
            log.write("\n[dim]Fix these issues before scanning. "
                      "Click 'Run Diagnostics' for details.[/]")
            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True
            self.scanning = False
            return

        # Check if update binary exists
        try:
            tool = app.get_aml_tool()
        except FileNotFoundError as exc:
            log.write(f"[bold red]Error:[/] {exc}")
            log.write("[dim]Run the environment setup first to download aml-flash-tool.[/]")
            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True
            self.scanning = False
            return

        try:
            # Install udev rules if not present
            if not udev_rules_installed():
                log.write("[dim]Installing udev rules for USB access...[/]")
                if not pw:
                    log.write("[bold red]Error: Sudo password required to install udev rules.[/]")
                    log.write("[dim]Enter your password above and try again.[/]")
                    self.query_one("#scan-btn", Button).disabled = False
                    self.query_one("#cancel-btn", Button).disabled = True
                    self.scanning = False
                    return
                await install_udev_rules(sudo_password=pw)
                log.write("[green]\u2713 udev rules installed (MODE=0666 + uaccess).[/]")
            else:
                log.write("[dim]\u2713 udev rules already installed.[/]")

            # Track scan state for live display
            current_phase = "fast"
            fast_attempts = 0
            identify_attempts = 0

            def on_attempt(attempt: int, elapsed: int, phase: str) -> None:
                nonlocal fast_attempts, identify_attempts
                if phase == "fast":
                    fast_attempts = attempt
                    if attempt % 20 == 1:  # Log every ~1 second (20 × 50ms)
                        log.write(
                            f"  [dim]\U0001f50d Phase 1: Scanning sysfs... "
                            f"attempt {attempt} ({elapsed}s)[/]"
                        )
                elif phase == "identify":
                    identify_attempts += 1
                    if identify_attempts <= 3 or identify_attempts % 5 == 0:
                        log.write(
                            f"  [cyan]\U0001f517 Phase 2: Handshake attempt "
                            f"{identify_attempts} ({elapsed}s)[/]"
                        )

            def on_phase(phase: str) -> None:
                nonlocal current_phase
                current_phase = phase
                if phase == "identify":
                    log.write(
                        "[bold green]\u26a1 Device detected on USB! "
                        "Starting Amlogic handshake...[/]"
                    )
                elif phase == "fast":
                    log.write(
                        "[yellow]\u21bb Handshake failed \u2014 resetting to scan mode[/]"
                    )

            log.write("[bold]Phase 1: Fast sysfs polling (50ms intervals)...[/]")
            log.write("[dim]Waiting for device with VID=1b8e PID=c003[/]")

            device_info = await handshake_loop(
                tool,
                timeout=120,
                sudo_password=pw,
                on_attempt=on_attempt,
                on_phase=on_phase,
            )

            # Device detected!
            log.write("\n[bold green]\U0001f389 Device successfully identified![/]")
            log.write(f"  \U0001f4bb Chip: [bold]{device_info.chip or 'N/A'}[/]")
            log.write(f"  \U0001f511 Serial: {device_info.serial or 'N/A'}")
            log.write(f"  \U0001f4e6 Firmware: {device_info.firmware_version or 'N/A'}")

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
            exc_str = str(exc).lower()
            if "not identified" in exc_str or "timeout" in exc_str:
                log.write(f"\n[yellow]\u26a0 Device not detected within 120s timeout.[/]")
                log.write("[dim]Power-cycle the speaker and try again.[/]")
                log.write("[dim]Make sure you hold the test-pad short while plugging in USB.[/]")
            else:
                log.write(f"\n[bold red]Error:[/] {exc}")
                logger.error("USB scan failed: %s", exc, exc_info=True)

            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#cancel-btn", Button).disabled = True

        self.scanning = False
