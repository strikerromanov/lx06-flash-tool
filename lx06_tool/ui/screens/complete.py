"""Complete screen — final success/failure summary and next steps."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Markdown

from lx06_tool.app import FlashResult, LX06App


class CompleteScreen(Screen):
    """Complete screen — shows flash result and next steps."""

    DEFAULT_CSS = """
    CompleteScreen {
        align: center middle;
        padding: 1 2;
    }
    #complete-content {
        width: 80;
        max-width: 100;
        height: auto;
        max-height: 90%;
    }
    #complete-actions {
        height: auto;
        align: center middle;
        padding: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="complete-content")
        with Center(id="complete-actions"):
            yield Button("Exit", variant="primary", id="exit-btn")

    def on_mount(self) -> None:
        self._render_result()

    def _render_result(self) -> None:
        """Render the flash result summary."""
        app = self.app
        content = self.query_one("#complete-content")
        result: FlashResult | None = None

        if isinstance(app, LX06App):
            result = app.flash_result

        if result and result.success:
            md_text = """
# Flash Complete!

Your LX06 has been successfully flashed with custom firmware.

---

### What was flashed:

| Component | Status |
|-----------|--------|
| Boot partition | {boot} |
| System partition | {system} |
| Verification | {verify} |

**Duration:** {duration:.1f} seconds

---

### Next Steps:

1. **Unplug USB** from the speaker
2. **Power cycle** the speaker (unplug/replug power)
3. The speaker will boot from the **newly flashed partition**
4. If it doesn't boot, the original partition is still intact
   - You can recover via serial/U-boot (bootdelay=15)

### Recovery:

If the new firmware doesn't work:
- Power cycle the device — it may fall back to the active partition
- Use the backup files in `{backup_dir}` to restore
- Connect via serial and use U-boot to switch active partition

### Enjoy your custom smart speaker!
""".format(
                boot="Flashed" if result.boot_flashed else "Skipped",
                system="Flashed" if result.system_flashed else "Failed",
                verify="Passed" if result.verified else "Unverified",
                duration=result.duration_sec,
                backup_dir=str(app.config.backup_dir) if isinstance(app, LX06App) else "./backups/",
            )
        else:
            md_text = """
# Flash Failed

The firmware flashing process encountered errors.

---

### What happened:
{errors}

### Recovery Options:

1. **Try again** — restart the tool and re-attempt flashing
2. **Restore backup** — flash your original backup to recover
3. **Check connections** — ensure USB cable is properly connected

Your device should still boot from the **original active partition**,
so it is not bricked. The backup files are preserved in `{backup_dir}`.
""".format(
                errors="\n".join(f"- {e}" for e in (result.errors if result else ["Unknown error"])),
                backup_dir=str(app.config.backup_dir) if isinstance(app, LX06App) else "./backups/",
            )

        content.mount(Markdown(md_text))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "exit-btn":
            self.app.exit()
