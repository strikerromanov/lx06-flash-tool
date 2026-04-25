"""Welcome screen — introduction and safety warnings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Markdown, Static

WELCOME_MARKDOWN = """
# LX06 Flash Tool

### Xiaomi Xiaoai Speaker Pro — Custom Firmware Installer

---

**What this tool does:**

1. **Sets up** your Linux environment (deps, Docker, USB rules)
2. **Connects** to your LX06 via USB burning mode
3. **Backs up** all 7 MTD partitions (safety first!)
4. **Customizes** your firmware (debloat, media, AI)
5. **Flashes** the custom firmware to the inactive partition

---

**Before you begin:**

- Ensure your LX06 is powered off and disconnected from USB
- Have a USB-A to USB-A cable ready (or USB-A to micro-USB with adapter)
- Know your Linux sudo password
- **Back up your data** — this process modifies device firmware

---

⚠️ **Warning:** This tool modifies device firmware. Although A/B partitioning
provides a safety net, there is always a small risk of bricking your device.
Ensure you follow all instructions carefully.
"""


class WelcomeScreen(Screen):
    """Welcome screen with intro and start button."""

    DEFAULT_CSS = """
    WelcomeScreen {
        align: center middle;
    }
    #welcome-content {
        width: 80;
        max-width: 100;
        height: auto;
        max-height: 90%;
    }
    #welcome-actions {
        height: auto;
        align: center middle;
        padding: 1 0;
    }
    .start-btn {
        width: 40;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="welcome-content"):
            yield Markdown(WELCOME_MARKDOWN)
        with Center(id="welcome-actions"):
            yield Button("Start Setup", variant="success", id="start-btn", classes="start-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            # Navigate to environment screen via the app
            app = self.app
            app.run_worker(self._go_to_environment())

    async def _go_to_environment(self) -> None:
        from lx06_tool.app import LX06App
        if isinstance(self.app, LX06App):
            await self.app._go_to_screen("environment")
