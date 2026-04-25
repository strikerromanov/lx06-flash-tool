"""
Async subprocess runner for LX06 Flash Tool.

Provides a unified interface for running external commands (shell, update.exe,
squashfs tools, docker) with:
- Non-blocking execution via asyncio
- Real-time stdout/stderr capture
- Timeout handling
- Output callback for UI progress updates
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

logger = logging.getLogger(__name__)


# ── Result Dataclass ────────────────────────────────────────────────────────


@dataclass
class CommandResult:
    """Result of a completed subprocess command."""

    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def success(self) -> bool:
        """Whether the command exited with code 0."""
        return self.returncode == 0

    @property
    def combined_output(self) -> str:
        """Combined stdout + stderr."""
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)

    def __repr__(self) -> str:
        cmd_str = " ".join(self.command[:3])
        if len(self.command) > 3:
            cmd_str += " ..."
        status = "OK" if self.success else f"RC={self.returncode}"
        return f"CommandResult({cmd_str}, {status})"


# ── Output Callback Type ────────────────────────────────────────────────────

OutputCallback = Callable[[str, str], None]  # (stream_name, line) -> None


# ── Async Runner ────────────────────────────────────────────────────────────


class AsyncRunner:
    """Manages async subprocess execution with output capture.

    Usage:
        runner = AsyncRunner()
        result = await runner.run(["ls", "-la"])
        if result.success:
            print(result.stdout)

    With real-time output:
        def on_output(stream: str, line: str):
            print(f"[{stream}] {line}")

        result = await runner.run(
            ["update.exe", "identify"],
            on_output=on_output,
            timeout=30,
        )
    """

    def __init__(
        self,
        default_timeout: float = 300.0,
        env: dict[str, str] | None = None,
        cwd: Path | str | None = None,
        sudo: bool = False,
    ):
        self._default_timeout = default_timeout
        self._env = {**os.environ, **(env or {})}
        self._cwd = str(cwd) if cwd else None
        self._sudo = sudo

    async def run(
        self,
        command: list[str] | str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool | None = None,
        check: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """Execute a command asynchronously and capture its output.

        Args:
            command: Command as a list of args or a shell string.
            timeout: Max seconds to wait. None uses default_timeout.
            on_output: Callback for any output line (stdout or stderr).
            on_stdout: Callback for stdout lines only.
            on_stderr: Callback for stderr lines only.
            cwd: Working directory override.
            env: Extra environment variables.
            sudo: Prepend 'sudo' to command. None uses instance default.
            check: If True, raise on non-zero exit code.
            input_text: Text to pipe to stdin.

        Returns:
            CommandResult with captured stdout, stderr, and returncode.

        Raises:
            CommandTimeoutError: If the command exceeds the timeout.
            CommandError: If check=True and returncode != 0.
        """
        # Build command list
        if isinstance(command, str):
            cmd_list = shlex.split(command)
        else:
            cmd_list = list(command)

        use_sudo = sudo if sudo is not None else self._sudo
        if use_sudo:
            cmd_list = ["sudo", *cmd_list]

        # Build environment
        run_env = {**self._env, **(env or {})}

        # Working directory
        run_cwd = str(cwd) if cwd else self._cwd

        effective_timeout = timeout if timeout is not None else self._default_timeout

        logger.debug("Running: %s (timeout=%s, cwd=%s)", " ".join(cmd_list), effective_timeout, run_cwd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdin=asyncio.subprocess.PIPE if input_text else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_cwd,
                env=run_env,
            )
        except FileNotFoundError as exc:
            raise CommandError(
                f"Command not found: {cmd_list[0]}. "
                f"Is it installed and in PATH?"
            ) from exc
        except PermissionError as exc:
            raise CommandError(
                f"Permission denied executing: {cmd_list[0]}"
            ) from exc

        # Collect output with optional callbacks
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def _read_stream(
            stream: asyncio.StreamReader,
            collector: list[str],
            stream_name: str,
            specific_cb: OutputCallback | None,
        ) -> None:
            """Read lines from a stream, calling callbacks and collecting."""
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                collector.append(line)
                if on_output:
                    try:
                        on_output(stream_name, line)
                    except Exception:
                        pass
                if specific_cb:
                    try:
                        specific_cb(stream_name, line)
                    except Exception:
                        pass

        try:
            # Feed stdin if provided
            stdin_task = None
            if input_text:
                stdin_task = asyncio.ensure_future(
                    self._write_stdin(proc, input_text)
                )

            # Read both streams concurrently
            await asyncio.gather(
                _read_stream(proc.stdout, stdout_lines, "stdout", on_stdout),
                _read_stream(proc.stderr, stderr_lines, "stderr", on_stderr),
            )

            # Wait for process to finish (with timeout)
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                result = CommandResult(
                    command=cmd_list,
                    returncode=-1,
                    stdout="\n".join(stdout_lines),
                    stderr="\n".join(stderr_lines),
                    timed_out=True,
                )
                logger.warning("Command timed out after %ss: %s", effective_timeout, " ".join(cmd_list))
                if check:
                    raise CommandTimeoutError(f"Command timed out: {' '.join(cmd_list)}")
                return result

        except (CommandTimeoutError, CommandError):
            raise
        except Exception as exc:
            raise CommandError(f"Failed to run {' '.join(cmd_list)}: {exc}") from exc

        result = CommandResult(
            command=cmd_list,
            returncode=returncode,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
        )

        logger.debug(
            "Command finished: rc=%d, stdout=%d lines, stderr=%d lines",
            returncode,
            len(stdout_lines),
            len(stderr_lines),
        )

        if check and not result.success:
            raise CommandError(
                f"Command failed (rc={returncode}): {' '.join(cmd_list)}\n"
                f"stderr: {result.stderr[:500]}"
            )

        return result

    async def run_simple(self, command: list[str] | str, **kwargs) -> str:
        """Run a command and return stdout as a string.

        Convenience wrapper around run() for simple commands where
        you just want the output.

        Raises:
            CommandError: If the command fails.
        """
        result = await self.run(command, **kwargs, check=True)
        return result.stdout

    @staticmethod
    async def _write_stdin(proc: asyncio.subprocess.Process, text: str) -> None:
        """Write text to a process stdin and close it."""
        if proc.stdin:
            proc.stdin.write(text.encode())
            await proc.stdin.drain()
            proc.stdin.close()


# ── Runner Errors ───────────────────────────────────────────────────────────


class CommandError(Exception):
    """A subprocess command failed."""


class CommandTimeoutError(CommandError):
    """A subprocess command exceeded its timeout."""
