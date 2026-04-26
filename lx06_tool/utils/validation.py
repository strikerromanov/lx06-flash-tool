"""
lx06_tool/utils/validation.py
-------------------------------
Input validation framework for security hardening.

Provides sanitization and validation for user inputs to prevent
command injection, path traversal, and other security vulnerabilities.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Command Injection Prevention ────────────────────────────────────────

ALLOWED_COMMAND_PATTERN = re.compile(r'^[a-zA-Z0-9_\-./\s=:]+$')

def sanitize_command_input(cmd: str) -> str:
    """Sanitize command input to prevent injection attacks.

    Args:
        cmd: Raw command input from user

    Returns:
        Sanitized command string

    Raises:
        ValueError: If command contains potentially dangerous characters
    """
    if not isinstance(cmd, str):
        raise ValueError(f"Command must be string, got {type(cmd)}")

    # Remove null bytes
    cmd = cmd.replace('\x00', '')

    # Check for dangerous patterns
    dangerous_patterns = [
        r'[;&|`$]',  # Shell metacharacters
        r'\$\(.*\)',  # Command substitution
        r'`.*`',       # Backtick command substitution
        r'>|<',        # I/O redirection
        r'\n|\r',      # Newlines/carriage returns
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, cmd):
            raise ValueError(
                f"Command contains dangerous characters: {pattern}. "
                f"Input: {cmd[:100]}"
            )

    # Validate against allowed pattern
    if not ALLOWED_COMMAND_PATTERN.match(cmd):
        raise ValueError(
            f"Command contains invalid characters. "
            f"Only alphanumeric, spaces, and _-./=: are allowed. "
            f"Input: {cmd[:100]}"
        )

    return cmd


# ── Path Traversal Prevention ─────────────────────────────────────────────

def validate_path_safe(
    path: Path,
    allowed_dir: Path,
    *,
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> Path:
    """Ensure path is within allowed directory to prevent path traversal.

    Args:
        path: Path to validate
        allowed_dir: Directory that path must be within
        must_exist: Whether path must exist
        must_be_file: Whether path must be a file
        must_be_dir: Whether path must be a directory

    Returns:
        Resolved absolute path

    Raises:
        ValueError: If path is outside allowed directory or fails constraints
    """
    try:
        resolved = path.resolve()
        allowed = allowed_dir.resolve()

        # Check if path is within allowed directory
        try:
            resolved.relative_to(allowed)
        except ValueError:
            raise ValueError(
                f"Path outside allowed directory: {path} "
                f"(allowed: {allowed_dir})"
            )

        # Check existence if required
        if must_exist and not resolved.exists():
            raise ValueError(f"Path does not exist: {path}")

        # Check if it's a file if required
        if must_be_file and resolved.exists() and not resolved.is_file():
            raise ValueError(f"Path is not a file: {path}")

        # Check if it's a directory if required
        if must_be_dir and resolved.exists() and not resolved.is_dir():
            raise ValueError(f"Path is not a directory: {path}")

        return resolved

    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"Invalid path {path}: {e}")


def validate_filename(filename: str) -> str:
    """Validate filename to prevent path traversal via filename.

    Args:
        filename: Raw filename from user

    Returns:
        Validated filename

    Raises:
        ValueError: If filename is invalid
    """
    if not isinstance(filename, str):
        raise ValueError(f"Filename must be string, got {type(filename)}")

    # Remove null bytes but check for path separators first
    filename = filename.replace('\x00', '')

    # Check for path separators before stripping
    if '/' in filename or '\\' in filename:
        raise ValueError(
            f"Filename contains path separator. "
            f"Filenames must not contain '/' or '\\': {filename[:100]}"
        )

    # Check for dangerous patterns
    if '..' in filename:
        raise ValueError(f"Filename contains path traversal sequence: {filename}")

    # Check length
    if len(filename) > 255:
        raise ValueError(f"Filename too long: {len(filename)} > 255")

    # Check for empty filename
    if not filename or filename.isspace():
        raise ValueError("Filename cannot be empty or whitespace")

    return filename


# ── General Input Validation ─────────────────────────────────────────────

def validate_string_input(
    value: str,
    *,
    max_length: int = 1000,
    allow_empty: bool = False,
    allowed_chars: str | None = None,
) -> str:
    """Validate string input.

    Args:
        value: Input string to validate
        max_length: Maximum allowed length
        allow_empty: Whether empty string is allowed
        allowed_chars: Regex pattern of allowed characters (None = any)

    Returns:
        Validated string

    Raises:
        ValueError: If validation fails
    """
    if not isinstance(value, str):
        raise ValueError(f"Input must be string, got {type(value)}")

    # Remove null bytes
    value = value.replace('\x00', '')

    # Check length
    if len(value) > max_length:
        raise ValueError(f"Input too long: {len(value)} > {max_length}")

    # Check empty
    if not allow_empty and not value.strip():
        raise ValueError("Input cannot be empty")

    # Check allowed characters
    if allowed_chars and not re.match(allowed_chars, value):
        raise ValueError(f"Input contains invalid characters. Allowed: {allowed_chars}")

    return value


def validate_int_input(
    value: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Validate and convert integer input.

    Args:
        value: String input to convert to int
        min_value: Minimum allowed value
        max_value: Maximum allowed value

    Returns:
        Validated integer

    Raises:
        ValueError: If validation fails
    """
    try:
        int_value = int(value)
    except ValueError:
        raise ValueError(f"Invalid integer: {value}")

    if min_value is not None and int_value < min_value:
        raise ValueError(f"Value {int_value} below minimum {min_value}")

    if max_value is not None and int_value > max_value:
        raise ValueError(f"Value {int_value} above maximum {max_value}")

    return int_value


# ── Security Context for Validation ───────────────────────────────────────────

class SecurityContext:
    """Security validation context with configurable policies."""

    def __init__(
        self,
        *,
        base_dir: Path,
        max_file_size: int = 1024 * 1024 * 1024,  # 1 GB default
        allowed_commands: set[str] | None = None,
        strict_mode: bool = True,
    ):
        """Initialize security context.

        Args:
            base_dir: Base directory for file operations
            max_file_size: Maximum allowed file size
            allowed_commands: Set of allowed command names
            strict_mode: Whether to use strict validation
        """
        self.base_dir = base_dir
        self.max_file_size = max_file_size
        self.allowed_commands = allowed_commands
        self.strict_mode = strict_mode

    def validate_file_path(
        self,
        path: Path,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Validate file path within security context."""
        return validate_path_safe(
            path,
            self.base_dir,
            must_exist=must_exist,
        )

    def validate_command(self, cmd: str) -> str:
        """Validate command within security context."""
        sanitized = sanitize_command_input(cmd)

        if self.allowed_commands:
            # Extract command name (first word)
            cmd_name = sanitized.split()[0] if sanitized.split() else ""
            if cmd_name not in self.allowed_commands:
                raise ValueError(
                    f"Command '{cmd_name}' not in allowed list: "
                    f"{self.allowed_commands}"
                )

        return sanitized
