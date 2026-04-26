"""
lx06_tool/state.py
-------------------
State machine for the LX06 flash pipeline.

States flow linearly from WELCOME → COMPLETE with a branch at FLASH_*.
Any state can transition to ERROR, from which the machine can recover
to the last registered safe state.
"""

from __future__ import annotations

from enum import Enum, auto

from lx06_tool.exceptions import StateTransitionError


class AppState(Enum):
    # ── Initialisation
    WELCOME              = auto()
    CHECK_ENV            = auto()
    INSTALL_DEPS         = auto()
    SETUP_UDEV           = auto()
    DOWNLOAD_TOOLS       = auto()

    # ── USB Connection
    WAIT_USB             = auto()
    DEVICE_IDENTIFIED    = auto()

    # ── Backup & Safety
    UNLOCK_BOOTLOADER    = auto()
    DUMP_PARTITIONS      = auto()
    VERIFY_BACKUP        = auto()

    # ── Firmware Customisation
    EXTRACT_FIRMWARE     = auto()
    CUSTOMIZE_MENU       = auto()
    APPLY_CUSTOMIZATIONS = auto()
    BUILD_FIRMWARE       = auto()

    # ── Flashing
    DETECT_AB_PARTITION  = auto()
    FLASH_BOOTLOADER     = auto()
    FLASH_SYSTEM         = auto()
    VERIFY_FLASH         = auto()

    # ── Terminal
    COMPLETE             = auto()
    ERROR                = auto()


# Valid forward transitions.  ERROR is reachable from any state (added below).
_TRANSITIONS: dict[AppState, list[AppState]] = {
    AppState.WELCOME:              [AppState.CHECK_ENV],
    AppState.CHECK_ENV:            [AppState.INSTALL_DEPS, AppState.SETUP_UDEV],
    AppState.INSTALL_DEPS:         [AppState.SETUP_UDEV],
    AppState.SETUP_UDEV:           [AppState.DOWNLOAD_TOOLS],
    AppState.DOWNLOAD_TOOLS:       [AppState.WAIT_USB],
    AppState.WAIT_USB:             [AppState.DEVICE_IDENTIFIED],
    AppState.DEVICE_IDENTIFIED:    [AppState.UNLOCK_BOOTLOADER],
    AppState.UNLOCK_BOOTLOADER:    [AppState.DUMP_PARTITIONS],
    AppState.DUMP_PARTITIONS:      [AppState.VERIFY_BACKUP],
    AppState.VERIFY_BACKUP:        [AppState.EXTRACT_FIRMWARE],
    AppState.EXTRACT_FIRMWARE:     [AppState.CUSTOMIZE_MENU],
    AppState.CUSTOMIZE_MENU:       [AppState.APPLY_CUSTOMIZATIONS],
    AppState.APPLY_CUSTOMIZATIONS: [AppState.BUILD_FIRMWARE],
    AppState.BUILD_FIRMWARE:       [AppState.DETECT_AB_PARTITION],
    AppState.DETECT_AB_PARTITION:  [AppState.FLASH_BOOTLOADER, AppState.FLASH_SYSTEM],
    AppState.FLASH_BOOTLOADER:     [AppState.FLASH_SYSTEM, AppState.VERIFY_FLASH],
    AppState.FLASH_SYSTEM:         [AppState.VERIFY_FLASH],
    AppState.VERIFY_FLASH:         [AppState.COMPLETE],
    AppState.COMPLETE:             [],
    AppState.ERROR:                [],   # Recovery is handled separately
}
# Every state can go to ERROR
for _state in list(_TRANSITIONS):
    if AppState.ERROR not in _TRANSITIONS[_state]:
        _TRANSITIONS[_state].append(AppState.ERROR)


# Recovery map: from ERROR, which state is safe to return to
_RECOVERY_POINTS: dict[AppState, AppState] = {
    AppState.WAIT_USB:             AppState.WAIT_USB,
    AppState.DEVICE_IDENTIFIED:    AppState.WAIT_USB,
    AppState.UNLOCK_BOOTLOADER:    AppState.DEVICE_IDENTIFIED,
    AppState.DUMP_PARTITIONS:      AppState.DEVICE_IDENTIFIED,
    AppState.VERIFY_BACKUP:        AppState.DUMP_PARTITIONS,
    AppState.EXTRACT_FIRMWARE:     AppState.VERIFY_BACKUP,
    AppState.APPLY_CUSTOMIZATIONS: AppState.EXTRACT_FIRMWARE,
    AppState.BUILD_FIRMWARE:       AppState.EXTRACT_FIRMWARE,
    AppState.DETECT_AB_PARTITION:  AppState.VERIFY_BACKUP,
    AppState.FLASH_BOOTLOADER:     AppState.DETECT_AB_PARTITION,
    AppState.FLASH_SYSTEM:         AppState.DETECT_AB_PARTITION,
    AppState.VERIFY_FLASH:         AppState.DETECT_AB_PARTITION,
}


class StateMachine:
    """
    Manages the current pipeline state and enforces valid transitions.
    """

    def __init__(self) -> None:
        self._state: AppState = AppState.WELCOME
        self._error_origin: AppState | None = None   # State where error occurred
        self._last_safe: AppState = AppState.WELCOME

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def in_error(self) -> bool:
        return self._state == AppState.ERROR

    def transition(self, target: AppState) -> None:
        """
        Attempt a state transition. Raises StateTransitionError if invalid.
        """
        allowed = _TRANSITIONS.get(self._state, [])
        if target not in allowed:
            raise StateTransitionError(
                from_state=self._state.name,
                to_state=target.name,
                reason=f"Allowed targets from {self._state.name}: "
                       f"{[s.name for s in allowed]}",
            )

        if target == AppState.ERROR:
            self._error_origin = self._state
        elif target not in (AppState.ERROR,):
            self._last_safe = self._state

        self._state = target

    def to_error(self, origin: AppState | None = None) -> None:
        """Transition to ERROR, recording where the failure occurred."""
        self._error_origin = origin or self._state
        self._state = AppState.ERROR

    def recover(self) -> AppState:
        """
        Return to the appropriate recovery state after an error.

        Returns the state recovered to.
        """
        if not self.in_error or self._error_origin is None:
            return self._state

        recovery = _RECOVERY_POINTS.get(self._error_origin, AppState.WELCOME)
        self._state = recovery
        self._error_origin = None
        return recovery

    def can_transition(self, target: AppState) -> bool:
        return target in _TRANSITIONS.get(self._state, [])

    @property
    def error_origin(self) -> AppState | None:
        return self._error_origin

    @property
    def recovery_target(self) -> AppState | None:
        if self._error_origin:
            return _RECOVERY_POINTS.get(self._error_origin)
        return None
