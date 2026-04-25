"""
Structured logging for LX06 Flash Tool.

Configures both console (Rich-formatted) and file logging.
Console output is human-readable with colors; file output includes
timestamps and debug-level detail for troubleshooting.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from lx06_tool.constants import DEFAULT_LOG_DIR, LOG_FORMAT, LOG_DATE_FORMAT, LOG_MAX_FILES, LOG_MAX_SIZE_MB


# Track whether logging has been initialized to avoid duplicate handlers
_initialized = False


def setup_logging(
    level: int = logging.INFO,
    log_dir: Path | None = None,
    console: bool = True,
    file_log: bool = True,
) -> None:
    """Initialize the application logging system.

    Configures:
    - Console handler with colored output (if Rich is available)
    - Rotating file handler with DEBUG level capture

    Args:
        level: Minimum log level for console output.
        log_dir: Directory for log files. Defaults to DEFAULT_LOG_DIR.
        console: Enable console logging.
        file_log: Enable file logging.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    # Root logger configuration
    root = logging.getLogger("lx06_tool")
    root.setLevel(logging.DEBUG)  # Capture everything; handlers filter

    # Prevent propagation to root logger to avoid double output
    root.propagate = False

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(_get_console_formatter())
        root.addHandler(console_handler)

    # File handler (rotating)
    if file_log:
        target_dir = log_dir or DEFAULT_LOG_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file = target_dir / "lx06-tool.log"

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_SIZE_MB * 1024 * 1024,
            backupCount=LOG_MAX_FILES,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger under the lx06_tool namespace.

    Args:
        name: Module name (e.g. "modules.environment").

    Returns:
        Logger instance prefixed with "lx06_tool.".
    """
    return logging.getLogger(f"lx06_tool.{name}")


def _get_console_formatter() -> logging.Formatter:
    """Get a console-friendly log formatter.

    Tries to use Rich's ConsoleHandler if available for colors,
    falls back to a simple format otherwise.
    """
    try:
        from rich.logging import RichHandler
        # RichHandler provides its own formatting
        # We return None and let Rich handle it
        return None  # type: ignore[return-value]
    except ImportError:
        return logging.Formatter("%(levelname)-8s [%(name)s] %(message)s")


def get_console_handler(level: int = logging.INFO) -> logging.Handler:
    """Create a Rich-aware console handler for Textual integration.

    Returns a handler suitable for adding to a logger when
    running inside the Textual TUI.
    """
    try:
        from rich.logging import RichHandler
        return RichHandler(
            level=level,
            show_time=True,
            show_path=False,
            markup=True,
        )
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        return handler
