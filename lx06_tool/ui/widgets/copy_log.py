"""Mixin to add a 'Copy Log' button to any screen with a RichLog."""

from __future__ import annotations

import subprocess

from textual.widgets import RichLog


class CopyLogMixin:
    """Mixin that adds clipboard copy functionality for the screen's RichLog.

    Add to any screen class:
        class MyScreen(CopyLogMixin, Screen):
            ...

    Make sure the screen has a RichLog widget and add the Copy Log button
    in compose().
    """

    def copy_log_to_clipboard(self) -> None:
        """Copy the contents of the first RichLog widget to the system clipboard."""
        try:
            log = self.query_one(RichLog)

            # Use the debug sink's stored content if available
            if hasattr(self, '_debug_sink') and self._debug_sink:
                text = self._debug_sink.get_all_text()
            else:
                # Fallback: try to get content from RichLog (may not work in all Textual versions)
                text = "(empty log)"
                try:
                    # Try to access lines through public API if available
                    if hasattr(log, 'content'):
                        text = str(log.content)
                    elif hasattr(log, 'text'):
                        text = log.text
                    else:
                        text = "Unable to extract log content - use Download Log button instead"
                except Exception:
                    text = "Unable to extract log content - use Download Log button instead"

            # Try to copy to clipboard using available tools
            try:
                # Try xclip (X11)
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text.encode(),
                    check=True,
                    capture_output=True,
                )
            except FileNotFoundError:
                try:
                    # Try wl-copy (Wayland)
                    subprocess.run(
                        ["wl-copy"],
                        input=text.encode(),
                        check=True,
                        capture_output=True,
                    )
                except FileNotFoundError:
                    # Fallback: write to temp file
                    import tempfile
                    tmp = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", delete=False, prefix="lx06-log-"
                    )
                    tmp.write(text)
                    tmp.close()
                    raise RuntimeError(
                        f"No clipboard tool (xclip/wl-copy). Log saved to: {tmp.name}"
                    )

            # Brief visual feedback
            self.app.notify("Log copied to clipboard!", severity="information")

        except RuntimeError:
            raise
        except Exception as exc:
            self.app.notify(f"Copy failed: {exc}", severity="error")
