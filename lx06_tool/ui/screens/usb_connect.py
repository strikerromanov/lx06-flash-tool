"""
lx06_tool/ui/screens/usb_connect.py
-------------------------------------
USB connection screen.

Provides instructions for entering USB burning mode, password entry,
and device detection via AmlogicTool.identify_loop().
"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Label, Static

from lx06_tool.constants import HANDSHAKE_DEFAULT_TIMEOUT_S
from lx06_tool.ui.widgets import ActionButton, LogPanel, PasswordInput, StatusLabel


class USBConnectScreen(Screen):
    """USB connection screen — instructions, password, and device detection."""

    DEFAULT_CSS = """
    USBConnectScreen {
        align: center middle;
    }
    USBConnectScreen > VerticalScroll {
        width: 80;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
    }
    USBConnectScreen .title {
        text-align: center;
        text-style: bold;
        color: $primary;
        padding: 1 0;
    }
    USBConnectScreen .section-label {
        text-style: bold;
        color: $primary;
        padding: 1 0 0 0;
    }
    USBConnectScreen .instruction {
        padding: 0 1;
        color: $text;
    }
    USBConnectScreen .warning {
        padding: 0 1;
        color: $warning;
    }
    USBConnectScreen .status {
        padding: 0 1;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("🔌 USB Connection", classes="title")

            yield Label("Opening the Speaker", classes="section-label")
            yield Label(
                "1. Remove the 6 screws under the bottom rubber cap.\n"
                "2. Disconnect the 2 internal cables.\n"
                "3. Push the inner chassis upward to access the board.\n"
                "4. Connect a micro USB cable from the speaker to this computer.",
                classes="instruction",
            )

            yield Label("Entering USB Burning Mode", classes="section-label")
            yield Label(
                "5. Power on the speaker (connect power cable).\n"
                "6. [bold]Immediately[/bold] click \"Detect Device\" below —\n"
                "   you have about 2 seconds after power-on to catch it!\n",
                classes="instruction",
            )
            yield Label(
                "⚠ If detection fails, power-cycle the speaker and try again.\n"
                "  You may need to try several times.",
                classes="warning",
            )

            yield Label("Sudo Password", classes="section-label")
            yield PasswordInput(id="sudo_password")

            yield StatusLabel(text="Enter your sudo password, then click Detect Device.", id="detect_status")
            yield LogPanel(title="Detection Log", id="detect_log")

            with Center():
                yield ActionButton(
                    label="Detect Device",
                    variant="primary",
                    id="detect_btn",
                )
                yield ActionButton(
                    label="Continue",
                    variant="success",
                    id="continue_btn",
                    disabled=True,
                )

    def on_mount(self) -> None:
        """Initialize screen state."""
        self._log = self.query_one("#detect_log", LogPanel)
        self._status = self.query_one("#detect_status", StatusLabel)
        self._device_found = False

    def _set_status(self, text: str, status_class: str = "") -> None:
        try:
            self._status.status_text = text
            if status_class:
                self._status.status_class = status_class
        except Exception:
            pass

    # ─── Password handling ──────────────────────────────────────────────

    def on_password_input_password_submitted(self, event: PasswordInput.PasswordSubmitted) -> None:
        """Store the sudo password when submitted."""
        self.app.sudo_ctx.password = event.password  # type: ignore[attr-defined]
        self._log.write("[green]Password stored.[/green]")
        self._set_status("Password accepted. Click \"Detect Device\" to proceed.")

    # ─── Button handlers ────────────────────────────────────────────────

    def on_action_button_pressed(self, event: ActionButton.Pressed) -> None:
        """Handle button presses."""
        btn = event.action_button
        if btn.id == "detect_btn":
            btn.set_loading(True)
            self.query_one("#continue_btn", ActionButton).set_enabled(False)
            self.run_worker(self._detect_device(), exclusive=True)
        elif btn.id == "continue_btn" and self._device_found:
            self.app.push_screen("backup_flash")

    # ─── Device Detection ───────────────────────────────────────────────

    async def _detect_device(self) -> None:
        """Run the identify loop to detect the Amlogic device in USB burning mode."""
        # Ensure password is stored
        sudo_ctx = self.app.sudo_ctx  # type: ignore[attr-defined]
        if not sudo_ctx.has_password:
            # Try to validate without password (root / NOPASSWD)
            valid = await sudo_ctx.validate()
            if not valid:
                self._log.write("[red]Please enter your sudo password first.[/red]")
                self._set_status("❌ Enter sudo password first", "error")
                self.query_one("#detect_btn", ActionButton).set_loading(False)
                return

        self._log.write("Starting device detection...")
        self._log.write("Power on the speaker NOW! (you have ~2 seconds)")
        self._set_status("🔍 Listening for device... (power on the speaker now!)")

        try:
            aml_tool = self.app.get_aml_tool()  # type: ignore[attr-defined]

            def on_status(attempt: int, remaining: int) -> None:
                self._set_status(
                    f"🔍 Attempt {attempt} — {remaining}s remaining..."
                )
                self._log.write(f"Attempt {attempt}: no device yet ({remaining}s remaining)")

            result = await aml_tool.identify_loop(
                timeout_seconds=HANDSHAKE_DEFAULT_TIMEOUT_S,
                on_status=on_status,
            )

            if result.success:
                self._device_found = True
                self._log.write(
                    f"[bold green]✓ Device detected![/bold green]\n"
                    f"  Firmware version: {result.firmware_version}"
                )
                self._set_status(
                    f"✅ Device found! Firmware: {result.firmware_version}"
                )

                # Store firmware version in device state
                config = self.app.config  # type: ignore[attr-defined]
                config.device.connected = True
                config.device.firmware_version = result.firmware_version

                # Set bootdelay for safety
                self._log.write("Setting bootdelay to 15 for safety...")
                try:
                    bd_result = await aml_tool.set_bootdelay(15)
                    if bd_result.ok:
                        self._log.write("[green]✓ Bootdelay set to 15 (safety net enabled).[/green]")
                    else:
                        self._log.write(
                            f"[yellow]⚠ Bootdelay setting returned {bd_result.returncode}[/yellow]"
                        )
                except Exception as bd_exc:
                    self._log.write(f"[yellow]⚠ Bootdelay warning: {bd_exc}[/yellow]")

                self.query_one("#detect_btn", ActionButton).set_loading(False)
                self.query_one("#continue_btn", ActionButton).set_enabled(True)

            else:
                self._log.write(
                    f"[red]Device not detected after {HANDSHAKE_DEFAULT_TIMEOUT_S}s.[/red]\n"
                    f"[yellow]Power-cycle the speaker and try again.[/yellow]"
                )
                self._set_status(
                    f"❌ Timeout — power-cycle the speaker and click Detect again",
                    "error",
                )
                self.query_one("#detect_btn", ActionButton).set_loading(False)

        except Exception as exc:
            self._log.write(f"[red]Detection error: {exc}[/red]")
            self._set_status(f"❌ Error: {exc}", "error")
            self.query_one("#detect_btn", ActionButton).set_loading(False)
