"""
lx06_tool/utils/sudo.py
------------------------
Sudo password context with reliable command execution.

Strategy (in order of preference):
1. **No password needed** — running as root or NOPASSWD sudo (most common in Docker)
2. **sudo -S** — pipe password via stdin (works when stdin is not needed by the command)
3. **PTY via os.forkpty()** — spawn in a real pseudo-terminal (fallback for strict PAM)

The ``sudo -S`` approach works reliably because we use ``cp`` (not ``tee``) to
write files, so stdin is free for the password.

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
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class SudoResult:
    """Result from a sudo command."""
    returncode: int
    output: str
    password_sent: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SudoContext:
    """Holds the sudo password and executes commands.

    Tries multiple approaches to ensure sudo works on all distros:
    1. No password (root / NOPASSWD)  2. sudo -S  3. PTY fallback
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
    ) -> SudoResult:
        """Run a command via sudo.

        Args:
            cmd: Command WITHOUT "sudo" prefix — we add it.
            timeout: Seconds before killing the process.
        """
        return await _sudo_exec(cmd, self._password, timeout)

    async def sudo_write_file(self, content: str, dest: str, *, timeout: int = 15) -> SudoResult:
        """Write content to a file using sudo.

        Strategy: write to temp file first, then sudo cp to destination.
        """
        import tempfile
        from pathlib import Path

        dest_path = Path(dest)
        dest_dir = str(dest_path.parent)

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


# ─── Execution Engine ──────────────────────────────────────────────────────────

async def _sudo_exec(cmd: list[str], password: str, timeout: int = 30) -> SudoResult:
    """Execute a command via sudo, trying multiple approaches.

    Attempt order:
      1. Plain sudo (works when root or NOPASSWD)
      2. sudo -S with password piped via stdin
      3. PTY-based execution (os.forkpty) for strict PAM configs
    """
    full_cmd = ["sudo"] + [str(c) for c in cmd]

    # --- Attempt 1: Plain sudo (no password) ---
    # Works when running as root or sudoers has NOPASSWD
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        if proc.returncode == 0:
            return SudoResult(
                returncode=0,
                output=stdout.decode(errors="replace").strip(),
                password_sent=False,
            )

        # Check if it failed because of password requirement
        err_text = stderr.decode(errors="replace")
        needs_pw = _needs_password(err_text)
        if not needs_pw:
            # Failed for another reason (command not found, etc.)
            combined = (stdout + stderr).decode(errors="replace").strip()
            return SudoResult(
                returncode=proc.returncode or 1,
                output=combined,
                password_sent=False,
            )
    except asyncio.TimeoutError:
        return SudoResult(returncode=-1, output="Command timed out", password_sent=False)
    except Exception:
        pass  # Fall through to next approach

    # No password available and sudo needs one
    if not password:
        return SudoResult(
            returncode=1,
            output=f"sudo requires a password but none was provided",
            password_sent=False,
        )

    # --- Attempt 2: sudo -S (stdin pipe) ---
    # This works because our commands (cp, udevadm, etc.) don't need stdin
    try:
        result = await _sudo_with_stdin(full_cmd, password, timeout)
        if result.ok:
            return result
        # If -S failed with "a terminal is required", try PTY
        if "terminal is required" not in result.output:
            return result
    except Exception:
        pass

    # --- Attempt 3: PTY via os.forkpty() ---
    # Last resort for distros with strict PAM that reject -S
    try:
        return await _sudo_with_pty(full_cmd, password, timeout)
    except Exception as exc:
        return SudoResult(
            returncode=-1,
            output=f"All sudo methods failed: {exc}",
            password_sent=False,
        )


def _needs_password(stderr_text: str) -> bool:
    """Check if stderr indicates sudo needs a password."""
    indicators = [
        "a terminal is required",
        "no password",
        "a password is required",
        "sorry, try again",
        "authentication failure",
        "incorrect password",
        "user not in sudoers",
    ]
    lower = stderr_text.lower()
    return any(ind in lower for ind in indicators)


async def _sudo_with_stdin(
    full_cmd: list[str], password: str, timeout: int
) -> SudoResult:
    """Run sudo -S with password piped via stdin.

    This is the most reliable approach for commands that don't need stdin
    (like cp, udevadm, etc.).
    """
    cmd_with_s = ["sudo", "-S"] + full_cmd[1:]  # Replace 'sudo' with 'sudo -S'

    proc = await asyncio.create_subprocess_exec(
        *cmd_with_s,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=(password + "\n").encode()),
        timeout=timeout,
    )

    combined = (stdout + stderr).decode(errors="replace").strip()

    return SudoResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=combined,
        password_sent=True,
    )


async def _sudo_with_pty(
    full_cmd: list[str], password: str, timeout: int
) -> SudoResult:
    """Run sudo in a real PTY using os.forkpty().

    This runs subprocess in a thread to avoid blocking the event loop.
    Uses os.forkpty() which creates a proper PTY pair at the OS level.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _pty_sync, full_cmd, password, timeout
    )


def _pty_sync(full_cmd: list[str], password: str, timeout: int) -> SudoResult:
    """Synchronous PTY execution (runs in thread pool).

    Uses os.forkpty() which properly creates a pseudo-terminal pair
    and forks the process. This is more reliable than asyncio subprocess
    with raw FDs because the child process genuinely has a controlling terminal.
    """
    import pty
    import select
    import time
    import signal

    pid, master_fd = pty.fork()

    if pid == 0:
        # --- Child process ---
        try:
            os.execvp(full_cmd[0], full_cmd)
        except Exception as e:
            os._exit(127)

    # --- Parent process ---
    output = b""
    password_sent = False
    deadline = time.monotonic() + timeout

    try:
        # Set master_fd to non-blocking
        import fcntl
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        while time.monotonic() < deadline:
            # Check if child exited
            pid_result, status = os.waitpid(pid, os.WNOHANG)
            if pid_result != 0:
                # Child exited, read remaining output
                try:
                    while True:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        output += data
                except OSError:
                    pass
                break

            # Read available data
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
                        time.sleep(0.1)  # Brief pause after prompt appears
                        os.write(master_fd, (password + "\n").encode())
                        password_sent = True
                except OSError:
                    break

        else:
            # Timeout — kill child
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            os.waitpid(pid, 0)

        # Get return code
        if pid_result != 0:
            exitcode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        else:
            exitcode = -1

        return SudoResult(
            returncode=exitcode,
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
    """One-shot sudo command.

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
