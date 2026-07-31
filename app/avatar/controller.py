"""Deterministic programmatic blink, gaze, breathing, head and mouth control."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Protocol

from app.avatar.models import AvatarParameterFrame
from app.clients.vts import VTSParameterCapability
from app.config.settings import AvatarConfig
from app.emotion import EmotionLabel

_MOUTH_OPEN = "MouthOpen"
_MOUTH_SMILE = "MouthSmile"
_BROWS = "Brows"
_EYE_OPEN_LEFT = "EyeOpenLeft"
_EYE_OPEN_RIGHT = "EyeOpenRight"
_EYE_LEFT_X = "EyeLeftX"
_EYE_LEFT_Y = "EyeLeftY"
_EYE_RIGHT_X = "EyeRightX"
_EYE_RIGHT_Y = "EyeRightY"
_FACE_ANGLE_X = "FaceAngleX"
_FACE_ANGLE_Y = "FaceAngleY"
_FACE_ANGLE_Z = "FaceAngleZ"
_FACE_POSITION_Y = "FacePositionY"
_BLINK_INTERVAL_SECONDS = 4.0
_BLINK_DURATION_SECONDS = 0.18


class RandomSource(Protocol):
    def random(self) -> float: ...

    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True, slots=True)
class _EmotionProfile:
    activity: float
    brows: float
    smile: float
    eye_open: float
    transition_seconds: float


_PROFILES: dict[EmotionLabel, _EmotionProfile] = {
    EmotionLabel.neutral: _EmotionProfile(0.80, 0.00, 0.00, 1.00, 0.65),
    EmotionLabel.happy: _EmotionProfile(1.05, 0.08, 0.16, 1.00, 0.50),
    EmotionLabel.shy: _EmotionProfile(0.72, 0.05, 0.05, 0.90, 0.28),
    EmotionLabel.proud: _EmotionProfile(0.88, 0.08, 0.10, 1.00, 0.45),
    EmotionLabel.angry_cute: _EmotionProfile(0.95, -0.10, -0.08, 0.90, 0.25),
    EmotionLabel.worried: _EmotionProfile(0.62, -0.12, -0.05, 0.86, 0.80),
    EmotionLabel.bored: _EmotionProfile(0.48, -0.05, -0.08, 0.82, 0.85),
    EmotionLabel.excited: _EmotionProfile(1.25, 0.14, 0.22, 1.00, 0.24),
    EmotionLabel.explosion_mode: _EmotionProfile(1.45, 0.18, 0.26, 1.00, 0.18),
    EmotionLabel.sleepy: _EmotionProfile(0.35, -0.10, -0.08, 0.62, 0.95),
    EmotionLabel.focused: _EmotionProfile(0.52, 0.02, 0.00, 0.94, 0.55),
}


class AvatarParameterController:
    """Compose all continuous VTS input parameters in one finite frame."""

    def __init__(
        self,
        config: AvatarConfig,
        capabilities: dict[str, VTSParameterCapability],
        *,
        rng: RandomSource | None = None,
    ) -> None:
        self._config = config
        self._capabilities = dict(capabilities)
        self._rng = rng or random.Random()
        self._emotion = EmotionLabel.neutral
        self._profile_from = _PROFILES[self._emotion]
        self._profile_to = self._profile_from
        self._transition_started = 0.0
        self._speaking = False
        self._mouth = 0.0
        self._last_now: float | None = None
        self._next_blink_at: float | None = None
        self._blink_started: float | None = None
        self._gaze_x = 0.0
        self._gaze_y = 0.0
        self._gaze_target_x = 0.0
        self._gaze_target_y = 0.0
        self._next_gaze_at: float | None = None
        self._frame_sequence = 0

    @property
    def lip_sync_available(self) -> bool:
        return self._config.lip_sync_enabled and _MOUTH_OPEN in self._capabilities

    def set_emotion(self, emotion: EmotionLabel, *, now: float) -> None:
        if (
            not isinstance(emotion, EmotionLabel)
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
        ):
            raise ValueError("Avatar emotion transition is invalid")
        current = self._interpolated_profile(float(now))
        self._emotion = emotion
        self._profile_from = current
        self._profile_to = _PROFILES[emotion]
        self._transition_started = float(now)

    def set_speaking(self, speaking: bool) -> None:
        if not isinstance(speaking, bool):
            raise ValueError("Avatar speaking state is invalid")
        self._speaking = speaking

    def set_mouth(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Avatar mouth value is invalid")
        self._mouth = min(1.0, max(0.0, float(value)))

    def compose(
        self,
        *,
        now: float,
        vts_generation: int,
        model_generation: int,
    ) -> AvatarParameterFrame | None:
        if (
            not self._config.parameter_control_enabled
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or isinstance(vts_generation, bool)
            or not isinstance(vts_generation, int)
            or vts_generation < 1
            or isinstance(model_generation, bool)
            or not isinstance(model_generation, int)
            or model_generation < 1
        ):
            return None
        selected_now = float(now)
        delta = (
            1.0 / self._config.tick_hz
            if self._last_now is None
            else max(0.0, min(1.0, selected_now - self._last_now))
        )
        self._last_now = selected_now
        profile = self._interpolated_profile(selected_now)
        values: dict[str, float] = {}
        if self._config.micro_motion_enabled:
            blink = self._blink_openness(selected_now)
            self._update_gaze(selected_now, delta, profile.activity)
            values.update(
                {
                    _EYE_OPEN_LEFT: profile.eye_open * blink,
                    _EYE_OPEN_RIGHT: profile.eye_open * blink,
                    _EYE_LEFT_X: self._gaze_x,
                    _EYE_LEFT_Y: self._gaze_y,
                    _EYE_RIGHT_X: self._gaze_x,
                    _EYE_RIGHT_Y: self._gaze_y,
                    _FACE_ANGLE_X: math.sin(selected_now * 0.63)
                    * self._config.head_amplitude_degrees
                    * profile.activity,
                    _FACE_ANGLE_Y: math.sin(selected_now * 0.47 + 1.1)
                    * self._config.head_amplitude_degrees
                    * 0.55
                    * profile.activity,
                    _FACE_ANGLE_Z: math.sin(selected_now * 0.31 + 2.0)
                    * self._config.head_amplitude_degrees
                    * 0.35
                    * profile.activity,
                    _FACE_POSITION_Y: math.sin(selected_now * 1.35)
                    * self._config.breathing_amplitude
                    * (0.75 + profile.activity * 0.25),
                    _BROWS: profile.brows,
                    _MOUTH_SMILE: profile.smile,
                }
            )
        if self._config.lip_sync_enabled:
            values[_MOUTH_OPEN] = self._mouth
        bounded = tuple(
            (parameter_id, self._clamp(parameter_id, value))
            for parameter_id, value in values.items()
            if parameter_id in self._capabilities
        )
        if not bounded:
            return None
        self._frame_sequence += 1
        return AvatarParameterFrame(
            vts_generation=vts_generation,
            model_generation=model_generation,
            sequence=self._frame_sequence,
            parameters=bounded,
        )

    def zero_mouth_parameters(self) -> dict[str, float]:
        return {_MOUTH_OPEN: 0.0} if _MOUTH_OPEN in self._capabilities else {}

    def _interpolated_profile(self, now: float) -> _EmotionProfile:
        duration = self._profile_to.transition_seconds
        progress = min(1.0, max(0.0, (now - self._transition_started) / duration))
        eased = progress * progress * (3.0 - 2.0 * progress)
        return _EmotionProfile(
            activity=self._mix(self._profile_from.activity, self._profile_to.activity, eased),
            brows=self._mix(self._profile_from.brows, self._profile_to.brows, eased),
            smile=self._mix(self._profile_from.smile, self._profile_to.smile, eased),
            eye_open=self._mix(self._profile_from.eye_open, self._profile_to.eye_open, eased),
            transition_seconds=duration,
        )

    def _blink_openness(self, now: float) -> float:
        if self._next_blink_at is None:
            self._next_blink_at = now + _BLINK_INTERVAL_SECONDS
        if (
            self._blink_started is None
            and self._next_blink_at is not None
            and now >= self._next_blink_at
        ):
            overdue_intervals = math.floor((now - self._next_blink_at) / _BLINK_INTERVAL_SECONDS)
            self._next_blink_at += overdue_intervals * _BLINK_INTERVAL_SECONDS
            self._blink_started = self._next_blink_at
            self._next_blink_at += _BLINK_INTERVAL_SECONDS
        if self._blink_started is None:
            return 1.0
        elapsed = now - self._blink_started
        if elapsed <= 0.0:
            return 1.0
        if elapsed > _BLINK_DURATION_SECONDS:
            self._blink_started = None
            return 1.0
        phase = elapsed / _BLINK_DURATION_SECONDS
        return 1.0 - math.sin(math.pi * phase) ** 2

    def _update_gaze(self, now: float, delta: float, activity: float) -> None:
        if self._next_gaze_at is None or now >= self._next_gaze_at:
            scale = self._config.gaze_amplitude * (0.35 if self._speaking else 1.0)
            scale *= min(1.25, max(0.45, activity))
            self._gaze_target_x = self._rng.uniform(-scale, scale)
            self._gaze_target_y = self._rng.uniform(-scale * 0.55, scale * 0.55)
            interval = (
                self._rng.uniform(2.5, 5.5) if self._speaking else self._rng.uniform(1.4, 3.8)
            )
            self._next_gaze_at = now + interval
        alpha = 1.0 - math.exp(-delta / 0.32) if delta > 0.0 else 0.0
        self._gaze_x += alpha * (self._gaze_target_x - self._gaze_x)
        self._gaze_y += alpha * (self._gaze_target_y - self._gaze_y)

    def _clamp(self, parameter_id: str, value: float) -> float:
        capability = self._capabilities[parameter_id]
        return min(capability.maximum, max(capability.minimum, float(value)))

    @staticmethod
    def _mix(start: float, end: float, amount: float) -> float:
        return start + (end - start) * amount
