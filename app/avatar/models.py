"""Content-free contracts for the single-owner Avatar Runtime."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from app.emotion import EmotionLabel

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_SEMANTIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AvatarRuntimeState(StrEnum):
    stopped = "stopped"
    connecting = "connecting"
    authorizing = "authorizing"
    preparing = "preparing"
    ready = "ready"
    backoff = "backoff"
    disabled = "disabled"


class AvatarTransitionClass(StrEnum):
    gentle = "gentle"
    normal = "normal"
    sudden = "sudden"


@dataclass(frozen=True, slots=True)
class AvatarTurnPlan:
    """One frozen, bounded visual plan for a complete assistant turn."""

    turn_id: str
    emotion: EmotionLabel
    voice_slot: str
    body_motion_key: str | None
    transition: AvatarTransitionClass

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.turn_id):
            raise ValueError("Avatar turn id is invalid")
        if (
            not isinstance(self.emotion, EmotionLabel)
            or not _SAFE_SEMANTIC.fullmatch(self.voice_slot)
            or (
                self.body_motion_key is not None
                and not _SAFE_SEMANTIC.fullmatch(self.body_motion_key)
            )
            or not isinstance(self.transition, AvatarTransitionClass)
        ):
            raise ValueError("Avatar turn plan is invalid")


@dataclass(frozen=True, slots=True)
class AvatarParameterFrame:
    """One finite, generation-scoped latest-wins VTS parameter frame."""

    vts_generation: int
    model_generation: int
    sequence: int
    parameters: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.vts_generation, bool)
            or not isinstance(self.vts_generation, int)
            or self.vts_generation < 1
            or isinstance(self.model_generation, bool)
            or not isinstance(self.model_generation, int)
            or self.model_generation < 1
            or isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
            or not 1 <= len(self.parameters) <= 32
        ):
            raise ValueError("Avatar parameter frame is invalid")
        names: set[str] = set()
        for parameter_id, value in self.parameters:
            if (
                not isinstance(parameter_id, str)
                or not parameter_id
                or len(parameter_id) > 256
                or "\x00" in parameter_id
                or parameter_id in names
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Avatar parameter frame is invalid")
            names.add(parameter_id)


@dataclass(frozen=True, slots=True)
class AvatarHealthSnapshot:
    """Private-identifier-free runtime status safe for UI and diagnostics."""

    state: AvatarRuntimeState
    parameter_control_available: bool
    lip_sync_available: bool
    body_motion_available: bool
    automatic_red_eye_available: bool
    sent_frames: int
    coalesced_frames: int
    dropped_actions: int
    reconnect_count: int
    error_code: str | None = None
    parameter_error_code: str | None = None
    lip_sync_error_code: str | None = None
    body_motion_error_code: str | None = None
    red_eye_error_code: str | None = None

    def __post_init__(self) -> None:
        flags = (
            self.parameter_control_available,
            self.lip_sync_available,
            self.body_motion_available,
            self.automatic_red_eye_available,
        )
        counters = (
            self.sent_frames,
            self.coalesced_frames,
            self.dropped_actions,
            self.reconnect_count,
        )
        if (
            not isinstance(self.state, AvatarRuntimeState)
            or any(not isinstance(flag, bool) for flag in flags)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counters
            )
            or any(
                value is not None and not _SAFE_CODE.fullmatch(value)
                for value in (
                    self.error_code,
                    self.parameter_error_code,
                    self.lip_sync_error_code,
                    self.body_motion_error_code,
                    self.red_eye_error_code,
                )
            )
        ):
            raise ValueError("Avatar health snapshot is invalid")
