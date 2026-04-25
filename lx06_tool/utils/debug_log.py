"""lx06_tool/utils/debug_log.py
----------------------------
Global debug/command history log.

Provides a module-level ``log_debug()`` function that can be called from
*anywhere* (runner, sudo, modules) without importing Textual widgets.

Supports **multiple sinks** — both the dedicated DebugLogPanel AND any
screen's RichLog can receive debug messages simultaneously.

Sinks register themselves on mount and unregister on unmount.
If no sinks are registered the calls are simply buffered – zero overhead.

Log line format::

    [HH:MM:SS] [TAG] message

Tags: CMD  ERR  OK  INFO
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# ─── Protocol (what the widget must implement) ────────────────────────────────

class DebugSink(Protocol):
    """Protocol for the widget that receives debug log entries."""
    def write_line(self, tag: str, message: str) -> None: ...
    def get_all_text(self) -> str: ...


# ─── RichLog Sink Adapter ─────────────────────────────────────────────────────

_TAG_COLORS: dict[str, str] = {
    "CMD": "cyan",
    "OK": "green",
    "ERR": "red",
    "INFO": "yellow",
}


class RichLogSink:
    """Adapter that makes a Textual RichLog widget act as a DebugSink.

    Usage in any Screen::

        from lx06_tool.utils.debug_log import RichLogSink, register_sink, unregister_sink

        class MyScreen(Screen):
            def on_mount(self):
                self._debug_sink = RichLogSink(self.query_one(RichLog))
                register_sink(self._debug_sink)

            def on_unmount(self):
                unregister_sink(self._debug_sink)
    """

    def __init__(self, rich_log) -> None:
        self._log = rich_log
        self._lines: list[str] = []

    def write_line(self, tag: str, message: str) -> None:
        """Write a color-coded debug line to the RichLog."""
        color = _TAG_COLORS.get(tag, "dim")
        try:
            self._log.write(f"[{color}]{message}[/]")
        except Exception:
            pass  # Widget not ready / compositing
        # Also store plain text for clipboard copy
        self._lines.append(message)
        if len(self._lines) > 500:
            self._lines.pop(0)

    def get_all_text(self) -> str:
        """Return all stored lines as plain text."""
        return "\n".join(self._lines) if self._lines else "(empty log)"


# ─── Module-level state ──────────────────────────────────────────────────────

_lock = threading.Lock()
_sinks: list[DebugSink] = []
_buffer: list[tuple[str, str, str]] = []  # (timestamp, tag, message)
_MAX_BUFFER = 500


def register_sink(sink: DebugSink) -> None:
    """Register a debug-log sink (additive — supports multiple sinks)."""
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)
        # Flush buffered messages that arrived before the widget mounted
        if _buffer:
            for ts, tag, msg in _buffer:
                try:
                    sink.write_line(tag, f"[{ts}] [{tag}] {msg}")
                except Exception:
                    pass
            _buffer.clear()


def unregister_sink(sink: DebugSink) -> None:
    """Unregister a specific debug-log sink (on unmount)."""
    with _lock:
        try:
            _sinks.remove(sink)
        except ValueError:
            pass


def log_debug(tag: str, message: str) -> None:
    """Append a timestamped debug entry.

    Thread-safe.  Safe to call from any thread or asyncio context.

    Args:
        tag:   One of CMD, ERR, OK, INFO.
        message: The log text.
    """
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag}] {message}"
    with _lock:
        if _sinks:
            for sink in list(_sinks):  # Copy list to avoid mutation during iteration
                try:
                    sink.write_line(tag, line)
                except Exception:
                    pass
        else:
            _buffer.append((ts, tag, message))
            if len(_buffer) > _MAX_BUFFER:
                _buffer.pop(0)
    # Also emit to Python logging at DEBUG level
    logger.debug("%s %s", tag, message)


def log_cmd(cmd: list[str] | str) -> None:
    """Log a command being executed."""
    text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    log_debug("CMD", f"$ {text}")


def log_ok(cmd: list[str] | str, returncode: int, output: str = "") -> None:
    """Log a successful command result."""
    text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    parts = [f"✓ {text}  (rc={returncode})"]
    if output:
        parts.append(output[:500])
    log_debug("OK", "\n".join(parts))


def log_err(cmd: list[str] | str, returncode: int, output: str = "") -> None:
    """Log a failed command result."""
    text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    parts = [f"✗ {text}  (rc={returncode})"]
    if output:
        parts.append(output[:500])
    log_debug("ERR", "\n".join(parts))


def log_exception(message: str, exc: Exception) -> None:
    """Log an exception with traceback info."""
    log_debug("ERR", f"{message}: {type(exc).__name__}: {exc}")


def get_all_text() -> str:
    """Return the full debug log text (for clipboard copy).

    Tries the first available sink, then falls back to the buffer.
    """
    with _lock:
        for sink in _sinks:
            try:
                return sink.get_all_text()
            except Exception:
                continue
        # Fallback: return buffered text
        return "\n".join(f"[{ts}] [{tag}] {msg}" for ts, tag, msg in _buffer)
