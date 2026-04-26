"""
Compatibility shim for legacy AsyncRunner / CommandResult imports.

The reviewed runner.py exports only `RunResult`, `run()`, `run_streaming()`
and `which_async()`.  Several pre-existing modules still reference the old
`AsyncRunner` class and `CommandResult` type.  This module provides a thin
wrapper so those modules continue to work without rewriting every call site.
"""

from __future__ import annotations

from collections.abc import Callable

from lx06_tool.utils.runner import RunResult, run, run_streaming


class CommandResult(RunResult):
    """RunResult with extra convenience properties expected by legacy code."""

    @property
    def success(self) -> bool:
        return self.ok

    @property
    def combined_output(self) -> str:
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts)


def _to_command_result(result: RunResult) -> CommandResult:
    """Promote a RunResult to a CommandResult."""
    return CommandResult(
        cmd=result.cmd,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
    )


class AsyncRunner:
    """Compatibility wrapper exposing the old AsyncRunner interface.

    Delegates to the new ``run()`` / ``run_streaming()`` functions.
    """

    def __init__(
        self,
        default_timeout: float = 60.0,
        sudo: bool = False,
        sudo_password: str = "",
    ) -> None:
        self._default_timeout = default_timeout
        self._sudo = sudo
        self._sudo_password = sudo_password

    async def run(  # noqa: D401
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
        sudo: bool | None = None,
        sudo_password: str | None = None,
        check: bool = False,
    ) -> CommandResult:
        """Execute *cmd* and return a CommandResult."""
        use_sudo = sudo if sudo is not None else self._sudo
        pw = sudo_password if sudo_password else self._sudo_password
        str_cmd = [str(c) for c in cmd]
        actual_cmd = ["sudo"] + str_cmd if use_sudo else str_cmd

        effective_timeout = int(timeout or self._default_timeout)

        # Build stdin_data when sudo -S is needed
        # Also preserve HOME environment to avoid .cache permission issues
        stdin_data: str | None = None
        if use_sudo and pw:
            # Use -S to read password from stdin, -H to preserve HOME environment
            actual_cmd = ["sudo", "-S", "-H"] + str_cmd
            stdin_data = pw + "\n"
        elif use_sudo:
            # Preserve HOME even without password (for passwordless sudo)
            actual_cmd = ["sudo", "-H"] + str_cmd

        if on_output is not None:
            def _on_stdout(line: str) -> None:
                on_output("stdout", line)

            def _on_stderr(line: str) -> None:
                on_output("stderr", line)

            rr = await run_streaming(
                actual_cmd,
                on_stdout=_on_stdout,
                on_stderr=_on_stderr,
                timeout=effective_timeout,
            )
        else:
            rr = await run(
                actual_cmd,
                timeout=effective_timeout,
                stdin_data=stdin_data,
            )

        cr = _to_command_result(rr)

        if check and not cr.ok:
            raise RuntimeError(
                f"Command failed (exit {cr.returncode}): {' '.join(str_cmd)}\n"
                f"{cr.stderr or cr.stdout}"
            )

        return cr
