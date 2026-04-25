"""
lx06_tool/utils/runner.py
--------------------------
Async subprocess execution engine.

All external tool calls (update.exe, pacman, docker, unsquashfs, …) go
through this module so that:

  • The Textual event loop is never blocked.
  • Output is captured and can be streamed to the UI in real time.
  • Timeouts are enforced without risking zombie processes.
  • The caller always gets a structured RunResult, not a bare string.
"""
from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Optional, Union

from lx06_tool.utils.debug_log import log_cmd, log_ok, log_err

# ─── Result Type ──────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    """Completed subprocess result."""
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def raise_on_error(self, context: str = "") -> None:
        """Raise RuntimeError with a helpful message if the command failed."""
        if not self.ok:
            prefix = f"[{context}] " if context else ""
            detail = self.stderr.strip() or self.stdout.strip()
            reason = "timed out" if self.timed_out else f"exit {self.returncode}"
            raise RuntimeError(
                f"{prefix}Command failed ({reason}): {' '.join(self.cmd)}\n{detail}"
            )


# ─── Core Runner ──────────────────────────────────────────────────────────────

async def run(
    cmd: list[str | Path],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 60,
    stdin_data: Optional[str] = None,
    capture: bool = True,
) -> RunResult:
    """
    Run a command asynchronously and return a RunResult.

    Parameters
    ----------
    cmd         : Command + arguments. Path objects are converted to str.
    cwd         : Working directory (default: current directory).
    env         : Override environment variables (merged with os.environ).
    timeout     : Kill the process after this many seconds.
    stdin_data  : Optional string to pipe to the process's stdin.
    capture     : If True, capture stdout/stderr. If False, inherit them
                  (useful for interactive sudo password prompts).
    """
    str_cmd = [str(c) for c in cmd]
    log_cmd(str_cmd)

    stdout_mode = asyncio.subprocess.PIPE if capture else None
    stderr_mode = asyncio.subprocess.PIPE if capture else None
    stdin_mode  = asyncio.subprocess.PIPE if stdin_data is not None else None

    proc = await asyncio.create_subprocess_exec(
        *str_cmd,
        stdout=stdout_mode,
        stderr=stderr_mode,
        stdin=stdin_mode,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

    timed_out = False
    try:
        stdin_bytes = stdin_data.encode() if stdin_data else None
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=float(timeout),
        )
    except asyncio.TimeoutError:
        timed_out = True
        _terminate(proc)
        stdout_bytes, stderr_bytes = b"", b""

    stdout_str = stdout_bytes.decode(errors="replace").strip() if stdout_bytes else ""
    stderr_str = stderr_bytes.decode(errors="replace").strip() if stderr_bytes else ""
    rc = proc.returncode if proc.returncode is not None else -1

    if rc == 0 and not timed_out:
        log_ok(str_cmd, rc, stdout_str[:200])
    else:
        combined = (stdout_str + "\n" + stderr_str).strip()
        log_err(str_cmd, rc, combined[:200])

    return RunResult(
        cmd=str_cmd,
        returncode=rc,
        stdout=stdout_str,
        stderr=stderr_str,
        timed_out=timed_out,
    )


async def run_streaming(
    cmd: list[str | Path],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 300,
    on_stdout: Optional[Callable[[str], None]] = None,
    on_stderr: Optional[Callable[[str], None]] = None,
) -> RunResult:
    """
    Run a command and stream output line-by-line to callbacks.

    Useful for long-running operations (squashfs, flashing) where the UI
    should display live progress rather than waiting for completion.

    Parameters
    ----------
    on_stdout : Called with each stdout line (stripped, no trailing newline).
    on_stderr : Called with each stderr line.
    """
    str_cmd = [str(c) for c in cmd]
    log_cmd(str_cmd)


    proc = await asyncio.create_subprocess_exec(
        *str_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def _read(stream: asyncio.StreamReader, lines: list[str],
                    callback: Optional[Callable[[str], None]]) -> None:
        async for raw_line in stream:
            line = raw_line.decode(errors="replace").rstrip("\n")
            lines.append(line)
            if callback:
                callback(line)

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _read(proc.stdout, stdout_lines, on_stdout),   # type: ignore[arg-type]
                _read(proc.stderr, stderr_lines, on_stderr),   # type: ignore[arg-type]
                proc.wait(),
            ),
            timeout=float(timeout),
        )
    except asyncio.TimeoutError:
        timed_out = True
        _terminate(proc)

    rc = proc.returncode if proc.returncode is not None else -1

    if rc == 0 and not timed_out:
        log_ok(str_cmd, rc)
    else:
        combined = ("\n".join(stdout_lines) + "\n" + "\n".join(stderr_lines)).strip()
        log_err(str_cmd, rc, combined[:200])

    return RunResult(
        cmd=str_cmd,
        returncode=rc,
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
        timed_out=timed_out,
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Gracefully terminate, then SIGKILL if still alive."""
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    # Give 2 seconds before SIGKILL
    async def _kill() -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    asyncio.create_task(_kill())


async def which_async(binary: str) -> Optional[str]:
    """Async shutil.which — returns path string or None."""
    result = await run(["which", binary], timeout=5)
    return result.stdout.strip() or None
