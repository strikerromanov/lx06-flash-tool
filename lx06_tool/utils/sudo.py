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

from lx06_tool.utils.debug_log import log_debug, log_cmd, log_ok, log_err

# Per-tier timeout: if a method doesn't succeed in 10s, it won't at all
_TIER_TIMEOUT = 10
# Total cascade timeout across all tiers
_TOTAL_TIMEOUT = 30


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
            timeout: Total seconds before giving up (max 30s).
        """
        log_cmd(cmd)
        return await _sudo_exec(cmd, self._password, min(timeout, _TOTAL_TIMEOUT))

    async def sudo_write_file(self, content: str, dest: str, *, timeout: int = 30) -> SudoResult:
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

    Each tier gets up to 10s. Total cascade capped at ``timeout`` seconds.
    Fast-fails on password-related errors without waiting for full timeout.
    """
    full_cmd = ["sudo"] + [str(c) for c in cmd]
    pw_len = len(password) if password else 0
    log_debug("INFO", f"sudo_exec: '{' '.join(full_cmd)}' (timeout={timeout}s, password={'***' if pw_len else 'none'}({pw_len}chars))")

    deadline = time.monotonic() + timeout

    # --- Attempt 1: Plain sudo (no password) ---
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        log_err(cmd, -1, "sudo cascade: no time remaining")
        return SudoResult(returncode=-1, output="Command timed out (cascade timeout)", password_sent=False)

    tier_timeout = min(_TIER_TIMEOUT, remaining)
    log_debug("INFO", f"sudo tier 1: plain sudo (timeout={tier_timeout:.0f}s)")
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=tier_timeout
        )
        if proc.returncode == 0:
            log_ok(cmd, 0, stdout.decode(errors="replace").strip()[:200])
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
            log_err(cmd, proc.returncode or 1, combined[:200])
            return SudoResult(
                returncode=proc.returncode or 1,
                output=combined,
                password_sent=False,
            )
        # Fast-fail: needs password, skip to next tier immediately
        log_debug("INFO", "sudo tier 1: needs password (fast-fail), moving to tier 2")
    except asyncio.TimeoutError:
        log_debug("INFO", f"sudo tier 1: timed out after {tier_timeout:.0f}s")
        try:
            proc.kill()
        except Exception:
            pass
    except Exception as exc:
        log_debug("INFO", f"sudo tier 1 exception: {exc}")

    # No password available and sudo needs one
    if not password:
        log_err(cmd, 1, "sudo requires a password but none was provided")
        return SudoResult(
            returncode=1,
            output="sudo requires a password but none was provided",
            password_sent=False,
        )

    # --- Attempt 2: sudo -S (stdin pipe) ---
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        log_err(cmd, -1, "sudo cascade: no time remaining after tier 1")
        return SudoResult(returncode=-1, output="Command timed out (cascade timeout)", password_sent=False)

    tier_timeout = min(_TIER_TIMEOUT, remaining)
    log_debug("INFO", f"sudo tier 2: sudo -S with stdin pipe (timeout={tier_timeout:.0f}s)")
    try:
        result = await _sudo_with_stdin(full_cmd, password, tier_timeout)
        if result.ok:
            log_ok(cmd, result.returncode, result.output[:200])
            return result
        # Fast-fail: if -S failed with "a terminal is required", skip to PTY immediately
        if _needs_password(result.output):
            log_debug("INFO", "sudo tier 2: needs terminal/PTY (fast-fail), moving to tier 3")
        else:
            log_err(cmd, result.returncode, result.output[:200])
            return result
    except asyncio.TimeoutError:
        log_debug("INFO", f"sudo tier 2: timed out after {tier_timeout:.0f}s")
    except Exception as exc:
        log_debug("INFO", f"sudo tier 2 exception: {exc}")

    # --- Attempt 3: PTY via os.forkpty() ---
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        log_err(cmd, -1, "sudo cascade: no time remaining after tier 2")
        return SudoResult(returncode=-1, output="Command timed out (cascade timeout)", password_sent=False)

    tier_timeout = min(_TIER_TIMEOUT, remaining)
    log_debug("INFO", f"sudo tier 3: PTY via os.forkpty() (timeout={tier_timeout:.0f}s)")
    try:
        result = await _sudo_with_pty(full_cmd, password, tier_timeout)
        if result.ok:
            log_ok(cmd, result.returncode, result.output[:200])
        else:
            log_err(cmd, result.returncode, result.output[:200])
        return result
    except Exception as exc:
        log_err(cmd, -1, f"All sudo methods failed: {exc}")
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

        pid_result = 0
        status = 0

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
        timeout: Kill after this many seconds (max 30s).
    """
    ctx = SudoContext(password)
    return await ctx.sudo_run(cmd, timeout=timeout)


async def sudo_write_file(
    content: str, dest: str, *, password: str = "", timeout: int = 30
) -> SudoResult:
    """One-shot sudo file write.

    Args:
        content: File content to write.
        dest: Destination path.
        password: sudo password.
        timeout: Kill after this many seconds (max 30s).
    """
    ctx = SudoContext(password)
    return await ctx.sudo_write_file(content, dest, timeout=timeout)
