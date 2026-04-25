"""
lx06_tool/utils/sudo.py
------------------------
Sudo password context with PTY-based command execution.

Uses pseudo-terminals (PTY) instead of stdin pipes because
``sudo -S`` is unreliable across different PAM configurations.
A PTY provides a real terminal that sudo expects, making
password authentication work reliably on all distros.

Usage::

    from lx06_tool.utils.sudo import SudoContext

    ctx = SudoContext("my_password")
    result = await ctx.sudo_run(["cp", "src", "/etc/dest"])
    # or one-shot:
    result = await sudo_run(["cp", "src", "/etc/dest"], password="pw")
"""

from __future__ import annotations

import asyncio
import os
import pty
import select
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class SudoResult:
    """Result from a sudo command executed via PTY."""
    returncode: int
    output: str
    password_sent: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SudoContext:
    """Holds the sudo password and executes commands via PTY.

    The password is sent to the PTY when sudo prompts for it,
    making authentication work reliably on all distros including
    CachyOS, Arch, Ubuntu, and Fedora.
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

    async def sudo_run(
        self,
        cmd: list[str],
        *,
        timeout: int = 30,
        expect_password: bool = True,
    ) -> SudoResult:
        """Run a command via sudo using PTY for password auth.

        Args:
            cmd: Command WITHOUT "sudo" prefix — we add it.
            timeout: Seconds before killing the process.
            expect_password: If True, watch for password prompt.
        """
        full_cmd = ["sudo"] + [str(c) for c in cmd]
        return await _pty_exec(full_cmd, self._password if expect_password else "", timeout)

    async def sudo_write_file(self, content: str, dest: str, *, timeout: int = 15) -> SudoResult:
        """Write content to a file using sudo via PTY.

        Strategy: write to temp file first, then sudo cp to destination.
        """
        import tempfile
        from pathlib import Path

        dest_path = Path(dest)
        dest_dir = str(dest_path.parent)
        dest_name = dest_path.name

        # Write to temp file (no sudo needed)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tmp", dir=dest_dir if os.access(dest_dir, os.W_OK) else None,
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            result = await self.sudo_run(["cp", tmp_path, str(dest_path)], timeout=timeout)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        return result

    async def validate(self) -> bool:
        """Validate that the stored password works with sudo.

        Returns True if sudo accepts the password, False otherwise.
        """
        result = await self.sudo_run(["true"], timeout=10)
        return result.ok


# ─── PTY Execution Engine ─────────────────────────────────────────────────────

async def _pty_exec(cmd: list[str], password: str, timeout: int = 30) -> SudoResult:
    """Execute a command via PTY, sending password when prompted.

    This is the core function that makes sudo work reliably.
    PTY provides a real terminal so sudo's PAM modules work correctly.
    """
    master_fd: Optional[int] = None
    proc: Optional[asyncio.subprocess.Process] = None

    try:
        master_fd, slave_fd = pty.openpty()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
        )

        os.close(slave_fd)  # Close slave in parent — only master reads

        output = b""
        password_sent = False
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if proc.returncode is not None:
                break

            # Check if master fd has data to read
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
            except (ValueError, OSError):
                break

            if ready:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    output += data

                    # Detect password prompt and send password
                    if (
                        not password_sent
                        and password
                        and (b"assword" in data or b"Password" in data)
                    ):
                        await asyncio.sleep(0.15)  # Brief pause after prompt appears
                        os.write(master_fd, (password + "\n").encode())
                        password_sent = True
                except OSError:
                    break

        # Wait for process to finish
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

        return SudoResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            output=output.decode(errors="replace").strip(),
            password_sent=password_sent,
        )

    except Exception as exc:
        return SudoResult(
            returncode=-1,
            output=f"PTY execution failed: {exc}",
            password_sent=False,
        )
    finally:
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass


# ─── Convenience Functions ─────────────────────────────────────────────────────

async def sudo_run(
    cmd: list[str],
    *,
    password: str = "",
    timeout: int = 30,
) -> SudoResult:
    """One-shot sudo command via PTY.

    Args:
        cmd: Command WITHOUT "sudo" prefix.
        password: sudo password.
        timeout: Kill after this many seconds.
    """
    ctx = SudoContext(password)
    return await ctx.sudo_run(cmd, timeout=timeout)


async def sudo_write_file(
    content: str,
    dest: str,
    *,
    password: str = "",
    timeout: int = 15,
) -> SudoResult:
    """One-shot write file via sudo."""
    ctx = SudoContext(password)
    return await ctx.sudo_write_file(content, dest, timeout=timeout)
