"""lx06_tool/utils/debug_log.py
----------------------------
Global debug/command history log.

Provides a module-level ``log_debug()`` function that can be called from
*anywhere* (runner, sudo, modules) without importing Textual widgets.

The actual widget (``DebugLogPanel``) registers itself on mount and
unregisters on unmount.  If no widget is registered the calls are
simply discarded – zero overhead when the panel is hidden.

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


# ─── Module-level state ──────────────────────────────────────────────────────

_lock = threading.Lock()
_sink: Optional[DebugSink] = None
_buffer: list[tuple[str, str, str]] = []  # (timestamp, tag, message)
_MAX_BUFFER = 500


def register_sink(sink: DebugSink) -> None:
    """Register the active debug-log widget."""
    with _lock:
        global _sink
        _sink = sink
        # Flush buffered messages that arrived before the widget mounted
        if _buffer:
            for ts, tag, msg in _buffer:
                try:
                    sink.write_line(tag, f"[{ts}] [{tag}] {msg}")
                except Exception:
                    pass
            _buffer.clear()


def unregister_sink() -> None:
    """Unregister the debug-log widget (on unmount)."""
    with _lock:
        global _sink
        _sink = None


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
        if _sink is not None:
            try:
                _sink.write_line(tag, line)
            except Exception:
                # Widget not ready / compositing – buffer briefly
                _buffer.append((ts, tag, message))
                if len(_buffer) > _MAX_BUFFER:
                    _buffer.pop(0)
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
    """Return the full debug log text (for clipboard copy)."""
    with _lock:
        if _sink is not None:
            try:
                return _sink.get_all_text()
            except Exception:
                pass
        # Fallback: return buffered text
        return "\n".join(f"[{ts}] [{tag}] {msg}" for ts, tag, msg in _buffer)
