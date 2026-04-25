"""Customize screen — interactive feature selection for custom firmware."""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    Markdown,
    Static,
)

from lx06_tool.app import LX06App
from lx06_tool.config import CustomizationChoices

logger = logging.getLogger(__name__)

CUSTOMIZE_INFO = """## Phase 3: Firmware Customization

Select the features you want in your custom firmware. Each section can be
toggled independently. Your choices will be applied when building the firmware.
"""


class CustomizeScreen(Screen):
    """Customize screen — interactive feature selection."""

    DEFAULT_CSS = """
    CustomizeScreen { padding: 1 2; }
    #customize-scroll { height: 1fr; }
    .section { margin: 1 0; padding: 1; border: solid $primary-darken-2; }
    .section-title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    .option-row { height: auto; margin: 0 0 0 1; }
    #customize-actions { height: auto; align: center middle; padding: 1 0; }
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
        margin: 1 0;
        border: solid $warning;
    }
    """

    def compose(self) -> ComposeResult:
        yield Markdown(CUSTOMIZE_INFO)
        with Horizontal(id="sudo-row"):
            yield Static("\U0001f512 Sudo Password:")
            yield Input(
                placeholder="\U0001f510 Sudo password...",
                password=True,
                id="sudo-input",
            )
        with VerticalScroll(id="customize-scroll"):

            # ── Debloat Section ──
            with Collapsible(title="Debloat", collapsed=False):
                with Vertical(classes="section"):
                    yield Checkbox("Remove Xiaomi Telemetry", id="remove-telemetry", value=True)
                    yield Checkbox("Remove OTA Updater", id="remove-ota", value=True)
                    yield Checkbox("Remove Xiaoai Voice Engine (aggressive)", id="remove-xiaoai", value=False)

            # ── Media Section ──
            with Collapsible(title="Media Player Suite", collapsed=False):
                with Vertical(classes="section"):
                    yield Checkbox("AirPlay (Shairport-Sync)", id="install-airplay", value=False)
                    yield Checkbox("DLNA/UPnP (Upmpdcli/MPD)", id="install-dlna", value=False)
                    yield Checkbox("Spotify Connect (librespot)", id="install-spotify", value=False)
                    yield Checkbox("Multi-room Audio (Snapcast)", id="install-snapcast", value=False)

            # ── AI Brain Section ──
            with Collapsible(title="AI Brain / Voice Intelligence", collapsed=False):
                with Vertical(classes="section"):
                    yield Checkbox(
                        "Soft Patch (xiaogpt — keeps Xiaomi wake word, routes to LLM)",
                        id="ai-soft", value=False,
                    )
                    yield Checkbox(
                        "Hard Patch (open-xiaoai — custom wake word, fully local)",
                        id="ai-hard", value=False,
                    )
                    yield Static("\nLLM Configuration:", classes="section-title")
                    yield Input(placeholder="LLM Provider (openai/gemini/kimi)", id="llm-provider", value="openai")
                    yield Input(placeholder="API Key", id="llm-api-key", password=True)
                    yield Input(placeholder="Model name (optional)", id="llm-model")
                    yield Input(placeholder="Custom Wake Word (hard patch only)", id="wake-word", value="hey_assistant")
                    yield Input(placeholder="AI Server URL (hard patch only)", id="ai-server-url")

        with Horizontal(id="customize-actions"):
            yield Button("Confirm Selections", variant="success", id="confirm-btn")
            yield Button("Back", variant="default", id="back-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-btn":
            self.app.run_worker(self._confirm())
        elif event.button.id == "back-btn":
            self.app.pop_screen()

    def _get_checkbox(self, widget_id: str) -> bool:
        """Get checkbox value safely."""
        try:
            return self.query_one(f"#{widget_id}", Checkbox).value
        except Exception:
            return False

    def _get_input(self, widget_id: str) -> str:
        """Get input value safely."""
        try:
            return self.query_one(f"#{widget_id}", Input).value or ""
        except Exception:
            return ""

    def _get_sudo_password(self) -> str:
        """Get the sudo password from the input field and sync to app."""
        try:
            pw = self.query_one("#sudo-input", Input).value.strip()
        except Exception:
            pw = ""
        # Sync to app-level so other screens can use it
        app = self.app
        if isinstance(app, LX06App):
            app.sudo_password = pw
        return pw

    async def _confirm(self) -> None:
        """Collect all selections and pass to the app."""

        # Sync sudo password to app
        self._get_sudo_password()

        # Determine AI mode from checkboxes
        ai_soft = self._get_checkbox("ai-soft")
        ai_hard = self._get_checkbox("ai-hard")

        if ai_hard:
            ai_mode = "hard"
        elif ai_soft:
            ai_mode = "soft"
        else:
            ai_mode = "none"

        # If soft AI mode is selected, keep xiaoai voice
        remove_xiaoai = self._get_checkbox("remove-xiaoai")
        if ai_mode == "soft" and remove_xiaoai:
            # Soft AI requires xiaoai voice — force it off
            remove_xiaoai = False

        choices = CustomizationChoices(
            # Debloat
            remove_telemetry=self._get_checkbox("remove-telemetry"),
            remove_auto_updater=self._get_checkbox("remove-ota"),
            remove_xiaoai_voice=remove_xiaoai,
            # Media
            install_airplay=self._get_checkbox("install-airplay"),
            install_dlna=self._get_checkbox("install-dlna"),
            install_spotify=self._get_checkbox("install-spotify"),
            install_snapcast=self._get_checkbox("install-snapcast"),
            # AI
            ai_mode=ai_mode,
            llm_provider=self._get_input("llm-provider") or "openai",
            llm_api_key=self._get_input("llm-api-key"),
            llm_model=self._get_input("llm-model"),
            custom_wake_word=self._get_input("wake-word"),
            ai_server_url=self._get_input("ai-server-url"),
        )

        # Validate
        errors = choices.validate()
        if errors:
            # Show validation errors — don't proceed
            # For now just log; could add a modal later
            logger.warning("Customization validation errors: %s", errors)

        app = self.app
        if isinstance(app, LX06App):
            await app.on_customize_done(choices)
