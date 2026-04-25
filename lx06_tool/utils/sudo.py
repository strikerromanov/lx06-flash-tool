"""
lx06_tool/utils/sudo.py
------------------------
Sudo password context for password-aware command execution.

Provides SudoContext that wraps commands with ``sudo -S`` when a
password is configured, piping the password via stdin.  All backend
modules that invoke ``sudo`` should accept an optional
``sudo_password`` parameter and use the helper here.
"""

from __future__ import annotations

from typing import Optional

from lx06_tool.utils.runner import RunResult, run


class SudoContext:
    """Holds the sudo password and provides helpers for wrapping commands.

    Usage::

        ctx = SudoContext("my_password")
        result = await ctx.run(["sudo", "pacman", "-S", "vim"])
    """

    def __init__(self, password: str = "") -> None:
        self._password = password

    @property
    def password(self) -> str:
        return self._password

    @password.setter
    def password(self, value: str) -> None:
        self._password = value

    @property
    def has_password(self) -> bool:
        return bool(self._password)

    def wrap_cmd(self, cmd: list[str]) -> tuple[list[str], Optional[str]]:
        """Wrap a sudo command with ``-S`` flag if a password is set.

        Parameters
        ----------
        cmd :
            Command list that **already starts with** ``"sudo"``.

        Returns
        -------
        (wrapped_cmd, stdin_data)
            *wrapped_cmd* has ``-S`` inserted after ``sudo`` when a
            password is set; *stdin_data* is ``password + "\n"`` or
            ``None``.
        """
        if not self._password or not cmd or cmd[0] != "sudo":
            return cmd, None
        return ["sudo", "-S"] + cmd[1:], self._password + "\n"

    async def run(
        self,
        cmd: list[str],
        *,
        timeout: int = 60,
        stdin_data: Optional[str] = None,
        **kwargs: object,
    ) -> RunResult:
        """Run a sudo command with automatic password injection.

        If *cmd* starts with ``"sudo"`` and a password is configured,
        ``-S`` is inserted and the password is piped via stdin.
        If the caller already provides *stdin_data*, it is prepended
        with the password line.
        """
        actual_cmd = cmd
        pw_stdin: Optional[str] = None

        if self._password and cmd and cmd[0] == "sudo":
            actual_cmd = ["sudo", "-S"] + cmd[1:]
            pw_stdin = self._password + "\n"
            if stdin_data is not None:
                pw_stdin += stdin_data

        final_stdin = pw_stdin if pw_stdin is not None else stdin_data
        return await run(actual_cmd, timeout=timeout, stdin_data=final_stdin, **kwargs)  # type: ignore[arg-type]


# ── Module-level convenience ──────────────────────────────────────────────────

async def sudo_run(
    cmd: list[str],
    *,
    sudo_password: str = "",
    timeout: int = 60,
    stdin_data: Optional[str] = None,
    **kwargs: object,
) -> RunResult:
    """Run a sudo command with an optional password."""
    ctx = SudoContext(sudo_password)
    return await ctx.run(cmd, timeout=timeout, stdin_data=stdin_data, **kwargs)
