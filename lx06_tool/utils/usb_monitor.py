"""
lx06_tool/utils/usb_monitor.py
--------------------------------
USB connection monitoring and keep-alive for LX06 devices.

Prevents speaker disconnection/reboots during long operations
by monitoring USB state and sending keep-alive commands.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from lx06_tool.utils.amlogic import AmlogicTool

logger = logging.getLogger(__name__)


class USBMonitor:
    """Monitors USB connection and sends keep-alive commands."""

    def __init__(
        self,
        tool: AmlogicTool,
        *,
        keep_alive_interval: float = 30.0,  # seconds
        on_disconnect: Optional[Callable] = None,
    ):
        """Initialize USB monitor.

        Args:
            tool: Connected AmlogicTool instance
            keep_alive_interval: Seconds between keep-alive commands
            on_disconnect: Callback when disconnection detected
        """
        self._tool = tool
        self._keep_alive_interval = keep_alive_interval
        self._on_disconnect = on_disconnect
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_check_time = 0

    async def start(self) -> None:
        """Start USB monitoring in background."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("USB monitor started (keep-alive every %ds)", self._keep_alive_interval)

    async def stop(self) -> None:
        """Stop USB monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("USB monitor stopped")

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self._keep_alive_interval)
                await self._send_keep_alive()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("USB monitor error: %s", exc)
                # Don't stop monitoring on errors, just continue

    async def _send_keep_alive(self) -> None:
        """Send keep-alive command to device.

        Uses harmless bulkcmd that queries device state without
        causing any operations that might trigger reboot.
        """
        try:
            # Query partition table - harmless command that keeps connection alive
            result = await self._tool.bulkcmd("printenv partitions", timeout=10)

            if result.returncode != 0:
                logger.warning("Keep-alive command failed (RC=%d)", result.returncode)

                # Check if device disconnected
                if self._on_disconnect:
                    error_output = (result.stderr or "").lower()
                    if "not found" in error_output or "no device" in error_output:
                        logger.error("Device disconnected detected!")
                        self._on_disconnect()

        except Exception as exc:
            logger.warning("Keep-alive failed: %s", exc)

    async def check_connection(self) -> bool:
        """Check if device is still connected.

        Returns:
            True if device is connected, False otherwise
        """
        try:
            result = await self._tool.bulkcmd("echo ping", timeout=5)
            return result.returncode == 0
        except Exception:
            return False


class USBSafetyGuard:
    """Context manager for USB-critical operations.

    Ensures USB connection is monitored during critical operations
    and handles disconnections gracefully.
    """

    def __init__(
        self,
        tool: AmlogicTool,
        *,
        keep_alive_interval: float = 30.0,
        on_disconnect: Optional[Callable] = None,
    ):
        """Initialize safety guard.

        Args:
            tool: Connected AmlogicTool instance
            keep_alive_interval: Seconds between keep-alive commands
            on_disconnect: Callback when disconnection detected
        """
        self._monitor = USBMonitor(
            tool,
            keep_alive_interval=keep_alive_interval,
            on_disconnect=on_disconnect,
        )

    async def __aenter__(self):
        """Start monitoring when entering context."""
        await self._monitor.start()
        return self._monitor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop monitoring when exiting context."""
        await self._monitor.stop()
