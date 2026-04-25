"""
State machine for LX06 Flash Tool.

Defines all application states, valid transitions, guard functions,
and recovery point mapping. Every state change is validated against
the transition table to prevent invalid flows.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Callable

from lx06_tool.exceptions import LX06Error


# ── State Definition ────────────────────────────────────────────────────────


class AppState(Enum):
    """All possible states in the LX06 Flash Tool workflow.

    The states follow the natural progression:
        Init → USB → Backup → Customize → Build → Flash → Complete

    Any state can transition to ERROR. From ERROR, the machine
    recovers to the last safe checkpoint.
    """

    # Initialization
    WELCOME = auto()
    CHECK_ENV = auto()
    INSTALL_DEPS = auto()
    SETUP_UDEV = auto()
    DOWNLOAD_TOOLS = auto()

    # USB Connection
    WAIT_USB = auto()
    DEVICE_IDENTIFIED = auto()

    # Backup & Safety
    UNLOCK_BOOTLOADER = auto()
    DUMP_PARTITIONS = auto()
    VERIFY_BACKUP = auto()

    # Firmware Customization
    EXTRACT_FIRMWARE = auto()
    CUSTOMIZE_MENU = auto()
    APPLY_CUSTOMIZATIONS = auto()
    BUILD_FIRMWARE = auto()

    # Flashing
    DETECT_AB_PARTITION = auto()
    FLASH_BOOTLOADER = auto()
    FLASH_SYSTEM = auto()

    # Completion
    VERIFY_FLASH = auto()
    COMPLETE = auto()
    ERROR = auto()


# ── Transition Table ────────────────────────────────────────────────────────
# Maps (from_state, to_state) → guard_function.
# Guard returns True to allow transition, False to deny.
# None means no guard (always allowed).

TransitionKey = tuple[AppState, AppState]
GuardFn = Callable[[], bool] | None


def _always_true() -> bool:
    return True


# Will be populated by StateMachine.__init__
TRANSITION_TABLE: dict[TransitionKey, GuardFn] = {}


# ── Recovery Points ─────────────────────────────────────────────────────────
# When an error occurs, the state machine rolls back to the recovery point
# associated with the current state. This ensures the user can retry safely.

RECOVERY_POINTS: dict[AppState, AppState] = {
    # Initialization phase — restart from welcome
    AppState.WELCOME: AppState.WELCOME,
    AppState.CHECK_ENV: AppState.WELCOME,
    AppState.INSTALL_DEPS: AppState.CHECK_ENV,
    AppState.SETUP_UDEV: AppState.CHECK_ENV,
    AppState.DOWNLOAD_TOOLS: AppState.CHECK_ENV,
    # USB phase — re-plug device
    AppState.WAIT_USB: AppState.WAIT_USB,
    AppState.DEVICE_IDENTIFIED: AppState.WAIT_USB,
    # Backup phase — retry from identified
    AppState.UNLOCK_BOOTLOADER: AppState.DEVICE_IDENTIFIED,
    AppState.DUMP_PARTITIONS: AppState.DEVICE_IDENTIFIED,
    AppState.VERIFY_BACKUP: AppState.DEVICE_IDENTIFIED,
    # Customization phase — re-extract
    AppState.EXTRACT_FIRMWARE: AppState.EXTRACT_FIRMWARE,
    AppState.CUSTOMIZE_MENU: AppState.EXTRACT_FIRMWARE,
    AppState.APPLY_CUSTOMIZATIONS: AppState.EXTRACT_FIRMWARE,
    AppState.BUILD_FIRMWARE: AppState.EXTRACT_FIRMWARE,
    # Flashing phase — re-detect partitions
    AppState.DETECT_AB_PARTITION: AppState.DETECT_AB_PARTITION,
    AppState.FLASH_BOOTLOADER: AppState.DETECT_AB_PARTITION,
    AppState.FLASH_SYSTEM: AppState.DETECT_AB_PARTITION,
    # Verification
    AppState.VERIFY_FLASH: AppState.DETECT_AB_PARTITION,
    # Terminal
    AppState.COMPLETE: AppState.COMPLETE,
    AppState.ERROR: AppState.WELCOME,
}


# ── State Machine ───────────────────────────────────────────────────────────


class StateMachine:
    """Manages application state transitions with guard validation.

    Usage:
        sm = StateMachine(guards={"deps_installed": check_deps, ...})
        sm.transition(AppState.CHECK_ENV)   # WELCOME → CHECK_ENV
        sm.transition(AppState.INSTALL_DEPS) # CHECK_ENV → INSTALL_DEPS

    Guards are callables provided at init time, keyed by name.
    The transition table maps (from, to) pairs to guard names.
    """

    def __init__(
        self,
        initial_state: AppState = AppState.WELCOME,
        guards: dict[str, GuardFn] | None = None,
    ):
        self._state = initial_state
        self._guards = guards or {}
        self._history: list[AppState] = [initial_state]
        self._build_transition_table()

    @property
    def state(self) -> AppState:
        """Current application state."""
        return self._state

    @property
    def history(self) -> list[AppState]:
        """List of all states visited, oldest first."""
        return list(self._history)

    @property
    def recovery_state(self) -> AppState:
        """The state to recover to on error."""
        return RECOVERY_POINTS.get(self._state, AppState.WELCOME)

    def can_transition(self, target: AppState) -> bool:
        """Check if a transition from current state to target is valid."""
        key = (self._state, target)
        if key not in TRANSITION_TABLE:
            return False
        guard = TRANSITION_TABLE[key]
        if guard is None:
            return True
        try:
            return guard()
        except Exception:
            return False

    def transition(self, target: AppState) -> AppState:
        """Transition to the target state if the guard passes.

        Returns:
            The new state (same as target on success).

        Raises:
            InvalidTransitionError: If the transition is not in the table.
            GuardFailedError: If the guard function returns False.
        """
        key = (self._state, target)

        if key not in TRANSITION_TABLE:
            raise InvalidTransitionError(
                f"Invalid transition: {self._state.name} → {target.name}. "
                f"No such transition exists in the state table."
            )

        guard = TRANSITION_TABLE[key]
        if guard is not None:
            try:
                result = guard()
            except Exception as exc:
                raise GuardFailedError(
                    f"Guard for {self._state.name} → {target.name} raised: {exc}"
                ) from exc
            if not result:
                raise GuardFailedError(
                    f"Guard denied transition: {self._state.name} → {target.name}. "
                    f"Preconditions not met."
                )

        self._state = target
        self._history.append(target)
        return self._state

    def recover(self) -> AppState:
        """Transition to the recovery point for the current state.

        Called after an error to return to a safe retry point.
        """
        recovery = RECOVERY_POINTS.get(self._state, AppState.WELCOME)
        self._state = recovery
        self._history.append(recovery)
        return self._state

    def force_state(self, state: AppState) -> None:
        """Force-set the state without transition validation.

        Used only for testing and error recovery. Do NOT use in
        normal operation — prefer transition() or recover().
        """
        self._state = state
        self._history.append(state)

    def _build_transition_table(self) -> None:
        """Build the transition table with guard references.

        Defines all valid state transitions and maps them to
        guard functions from the provided guards dict.
        """
        g = self._guards

        # fmt: off
        transitions: list[tuple[AppState, AppState, str | None]] = [
            # ── Initialization ──
            (AppState.WELCOME,           AppState.CHECK_ENV,          None),
            (AppState.CHECK_ENV,         AppState.INSTALL_DEPS,       g.get("deps_need_install")),
            (AppState.CHECK_ENV,         AppState.SETUP_UDEV,         g.get("deps_installed")),
            (AppState.INSTALL_DEPS,      AppState.SETUP_UDEV,         g.get("deps_installed")),
            (AppState.SETUP_UDEV,        AppState.DOWNLOAD_TOOLS,     g.get("udev_ready")),
            (AppState.DOWNLOAD_TOOLS,     AppState.WAIT_USB,          g.get("tools_downloaded")),
            # Skip paths (all deps already satisfied)
            (AppState.CHECK_ENV,         AppState.WAIT_USB,           g.get("env_fully_ready")),

            # ── USB Connection ──
            (AppState.WAIT_USB,          AppState.DEVICE_IDENTIFIED,  g.get("device_identified")),

            # ── Backup & Safety ──
            (AppState.DEVICE_IDENTIFIED, AppState.UNLOCK_BOOTLOADER,  None),
            (AppState.UNLOCK_BOOTLOADER, AppState.DUMP_PARTITIONS,    g.get("bootloader_unlocked")),
            (AppState.DUMP_PARTITIONS,   AppState.VERIFY_BACKUP,      g.get("partitions_dumped")),
            (AppState.VERIFY_BACKUP,     AppState.EXTRACT_FIRMWARE,   g.get("backup_verified")),

            # ── Firmware Customization ──
            (AppState.EXTRACT_FIRMWARE,    AppState.CUSTOMIZE_MENU,        g.get("firmware_extracted")),
            (AppState.CUSTOMIZE_MENU,      AppState.APPLY_CUSTOMIZATIONS,  g.get("choices_made")),
            (AppState.APPLY_CUSTOMIZATIONS, AppState.BUILD_FIRMWARE,       g.get("customizations_applied")),
            (AppState.BUILD_FIRMWARE,      AppState.DETECT_AB_PARTITION,   g.get("firmware_built")),

            # ── Flashing ──
            (AppState.DETECT_AB_PARTITION, AppState.FLASH_BOOTLOADER,  g.get("boot_partition_detected")),
            (AppState.DETECT_AB_PARTITION, AppState.FLASH_SYSTEM,      g.get("system_partition_detected")),
            (AppState.FLASH_BOOTLOADER,   AppState.FLASH_SYSTEM,      None),
            (AppState.FLASH_SYSTEM,       AppState.VERIFY_FLASH,      g.get("flash_complete")),

            # ── Completion ──
            (AppState.VERIFY_FLASH,       AppState.COMPLETE,          g.get("flash_verified")),
        ]
        # fmt: on

        TRANSITION_TABLE.clear()
        for from_state, to_state, guard_fn in transitions:
            TRANSITION_TABLE[(from_state, to_state)] = guard_fn

    def __repr__(self) -> str:
        return f"StateMachine(state={self._state.name}, history={len(self._history)} states)"


# ── Transition Errors ───────────────────────────────────────────────────────


class InvalidTransitionError(LX06Error):
    """An invalid state transition was attempted."""


class GuardFailedError(LX06Error):
    """A state transition guard returned False or raised an exception."""
