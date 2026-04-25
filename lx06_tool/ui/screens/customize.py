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
    """

    def compose(self) -> ComposeResult:
        yield Markdown(CUSTOMIZE_INFO)
        with VerticalScroll(id="customize-scroll"):

            # ── Debloat Section ──
            with Collapsible(title="Debloat", collapsed=False):
                with Vertical(classes="section"):
                    yield Checkbox("Enable Debloat", id="debloat-enabled", value=True)
                    yield Checkbox("Remove Xiaomi Telemetry", id="remove-telemetry", value=True)
                    yield Checkbox("Remove OTA Updater", id="remove-ota", value=True)
                    yield Checkbox("Remove Xiaoai Voice Engine (aggressive)", id="remove-xiaoai", value=False)

            # ── Media Section ──
            with Collapsible(title="Media Player Suite", collapsed=False):
                with Vertical(classes="section"):
                    yield Checkbox("Enable Media Suite", id="media-enabled", value=True)
                    yield Checkbox("AirPlay (Shairport-Sync)", id="install-airplay", value=True)
                    yield Checkbox("DLNA/UPnP (Upmpdcli/MPD)", id="install-dlna", value=True)
                    yield Checkbox("Spotify Connect (librespot)", id="install-spotify", value=True)
                    yield Checkbox("Multi-room Audio (Snapcast)", id="install-snapcast", value=False)
                    yield Checkbox("Squeezelite (Logitech Squeezebox)", id="install-squeezelite", value=False)

            # ── Spotify Config ──
            with Collapsible(title="Spotify Configuration", collapsed=True):
                with Vertical(classes="section"):
                    yield Static("Leave blank for anonymous mode (no account needed with librespot).")
                    yield Input(placeholder="Spotify Username", id="spotify-user")
                    yield Input(placeholder="Spotify Password", id="spotify-pass", password=True)

            # ── AI Brain Section ──
            with Collapsible(title="AI Brain / Voice Intelligence", collapsed=False):
                with Vertical(classes="section"):
                    yield Checkbox("Enable AI Integration", id="ai-enabled", value=True)
                    yield Checkbox(
                        "Soft Patch (xiaogpt — keeps Xiaomi wake word, routes to LLM)",
                        id="ai-soft", value=True,
                    )
                    yield Checkbox(
                        "Hard Patch (open-xiaoai — custom wake word, fully local)",
                        id="ai-hard", value=False,
                    )
                    yield Static("\nLLM Configuration:", classes="section-title")
                    yield Input(placeholder="LLM Provider (openai/google/kimi)", id="llm-provider", value="openai")
                    yield Input(placeholder="API Key", id="llm-api-key", password=True)
                    yield Input(placeholder="API Base URL (optional)", id="llm-api-base")
                    yield Input(placeholder="Model name (optional)", id="llm-model")
                    yield Input(placeholder="Custom Wake Word (hard patch only)", id="wake-word", value="hey_assistant")

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

    async def _confirm(self) -> None:
        """Collect all selections and pass to the app."""
        choices = CustomizationChoices(
            # Debloat
            debloat_enabled=self._get_checkbox("debloat-enabled"),
            remove_telemetry=self._get_checkbox("remove-telemetry"),
            remove_ota=self._get_checkbox("remove-ota"),
            remove_xiaoai=self._get_checkbox("remove-xiaoai"),
            # Media
            media_enabled=self._get_checkbox("media-enabled"),
            install_airplay=self._get_checkbox("install-airplay"),
            install_dlna=self._get_checkbox("install-dlna"),
            install_spotify=self._get_checkbox("install-spotify"),
            install_snapcast=self._get_checkbox("install-snapcast"),
            install_squeezelite=self._get_checkbox("install-squeezelite"),
            spotify_username=self._get_input("spotify-user"),
            spotify_password=self._get_input("spotify-pass"),
            # AI
            ai_enabled=self._get_checkbox("ai-enabled"),
            ai_soft_patch=self._get_checkbox("ai-soft"),
            ai_hard_patch=self._get_checkbox("ai-hard"),
            llm_provider=self._get_input("llm-provider") or "openai",
            llm_api_key=self._get_input("llm-api-key"),
            llm_api_base=self._get_input("llm-api-base") or None,
            llm_model=self._get_input("llm-model") or None,
            wake_word=self._get_input("wake-word") or None,
        )

        app = self.app
        if isinstance(app, LX06App):
            await app.on_customize_done(choices)
