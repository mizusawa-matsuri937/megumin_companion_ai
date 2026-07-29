"""Pure red-eye source ownership and monotonic deadline state machine."""

from __future__ import annotations

import math
from enum import StrEnum


class RedEyeOwner(StrEnum):
    off = "off"
    manual = "manual"
    system = "system"


class RedEyeCommand(StrEnum):
    activate = "activate"
    deactivate = "deactivate"


class RedEyeOwnership:
    """Separate manual toggle ownership from idempotent system activation."""

    def __init__(self, *, duration_seconds: float) -> None:
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds <= 0.0
        ):
            raise ValueError("red-eye duration is invalid")
        self._duration = float(duration_seconds)
        self._owner = RedEyeOwner.off
        self._deadline: float | None = None
        self._vts_generation = 0
        self._model_generation = 0

    @property
    def owner(self) -> RedEyeOwner:
        return self._owner

    @property
    def deadline(self) -> float | None:
        return self._deadline

    def trigger_system(
        self,
        *,
        now: float,
        vts_generation: int,
        model_generation: int,
    ) -> RedEyeCommand | None:
        self._validate_time_and_generations(now, vts_generation, model_generation)
        if self._owner is RedEyeOwner.manual:
            return None
        self._deadline = float(now) + self._duration
        self._vts_generation = vts_generation
        self._model_generation = model_generation
        if self._owner is RedEyeOwner.system:
            return None
        self._owner = RedEyeOwner.system
        return RedEyeCommand.activate

    def reconcile_manual(self, *, active: bool) -> None:
        if not isinstance(active, bool):
            raise ValueError("red-eye state is invalid")
        self._deadline = None
        self._vts_generation = 0
        self._model_generation = 0
        self._owner = RedEyeOwner.manual if active else RedEyeOwner.off

    def due(
        self,
        *,
        now: float,
        vts_generation: int,
        model_generation: int,
    ) -> RedEyeCommand | None:
        self._validate_time_and_generations(now, vts_generation, model_generation)
        if self._owner is not RedEyeOwner.system or self._deadline is None:
            return None
        if vts_generation != self._vts_generation or model_generation != self._model_generation:
            self._clear()
            return None
        if now < self._deadline:
            return None
        self._clear()
        return RedEyeCommand.deactivate

    def reset(self) -> RedEyeCommand:
        self._clear()
        return RedEyeCommand.deactivate

    def _clear(self) -> None:
        self._owner = RedEyeOwner.off
        self._deadline = None
        self._vts_generation = 0
        self._model_generation = 0

    @staticmethod
    def _validate_time_and_generations(
        now: float,
        vts_generation: int,
        model_generation: int,
    ) -> None:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or isinstance(vts_generation, bool)
            or not isinstance(vts_generation, int)
            or vts_generation < 1
            or isinstance(model_generation, bool)
            or not isinstance(model_generation, int)
            or model_generation < 1
        ):
            raise ValueError("red-eye generation is invalid")
