"""Deterministic W28 parameter composition and red-eye ownership tests."""

from __future__ import annotations

import math
import random

from app.avatar import (
    AvatarParameterFrame,
    AvatarParameterController,
    RedEyeCommand,
    RedEyeOwner,
    RedEyeOwnership,
)
from app.clients.vts import VTSParameterCapability
from app.config.settings import AvatarConfig
from app.emotion import EmotionLabel


def _capabilities() -> dict[str, VTSParameterCapability]:
    return {
        "MouthOpen": VTSParameterCapability(0.0, 1.0, 0.0),
        "MouthSmile": VTSParameterCapability(-1.0, 1.0, 0.0),
        "Brows": VTSParameterCapability(-1.0, 1.0, 0.0),
        "EyeOpenLeft": VTSParameterCapability(0.0, 1.0, 1.0),
        "EyeOpenRight": VTSParameterCapability(0.0, 1.0, 1.0),
        "EyeLeftX": VTSParameterCapability(-1.0, 1.0, 0.0),
        "EyeLeftY": VTSParameterCapability(-1.0, 1.0, 0.0),
        "EyeRightX": VTSParameterCapability(-1.0, 1.0, 0.0),
        "EyeRightY": VTSParameterCapability(-1.0, 1.0, 0.0),
        "FaceAngleX": VTSParameterCapability(-30.0, 30.0, 0.0),
        "FaceAngleY": VTSParameterCapability(-30.0, 30.0, 0.0),
        "FaceAngleZ": VTSParameterCapability(-30.0, 30.0, 0.0),
        "FacePositionY": VTSParameterCapability(-1.0, 1.0, 0.0),
    }


def _parameter_values(frame: AvatarParameterFrame | None) -> dict[str, float]:
    assert frame is not None
    return dict(frame.parameters)


def _red_eye_owner(ownership: RedEyeOwnership) -> RedEyeOwner:
    return ownership.owner


def test_controller_is_seeded_bounded_and_audio_changes_only_mouth() -> None:
    config = AvatarConfig()
    first = AvatarParameterController(config, _capabilities(), rng=random.Random(17))
    second = AvatarParameterController(config, _capabilities(), rng=random.Random(17))
    for controller in (first, second):
        controller.set_emotion(EmotionLabel.happy, now=0.0)
        controller.set_speaking(True)
        controller.set_mouth(0.75)

    first_frames = [
        first.compose(now=index / 25, vts_generation=1, model_generation=1) for index in range(250)
    ]
    second_frames = [
        second.compose(now=index / 25, vts_generation=1, model_generation=1) for index in range(250)
    ]

    assert first_frames == second_frames
    assert all(frame is not None for frame in first_frames)
    assert any(
        dict(frame.parameters)["EyeOpenLeft"] < 0.2 for frame in first_frames if frame is not None
    )
    for frame in first_frames:
        assert frame is not None
        for parameter_id, value in frame.parameters:
            capability = _capabilities()[parameter_id]
            assert math.isfinite(value)
            assert capability.minimum <= value <= capability.maximum

    baseline = AvatarParameterController(config, _capabilities(), rng=random.Random(7))
    with_audio = AvatarParameterController(config, _capabilities(), rng=random.Random(7))
    for controller in (baseline, with_audio):
        controller.set_emotion(EmotionLabel.focused, now=0.0)
        controller.set_speaking(True)
    baseline.set_mouth(0.0)
    with_audio.set_mouth(0.8)
    base_values = _parameter_values(baseline.compose(now=0.4, vts_generation=1, model_generation=1))
    audio_values = _parameter_values(
        with_audio.compose(now=0.4, vts_generation=1, model_generation=1)
    )
    changed = {key for key in base_values if abs(base_values[key] - audio_values[key]) > 1e-12}
    assert changed == {"MouthOpen"}


def test_controller_smooths_emotion_and_speaking_gaze_stays_tighter() -> None:
    config = AvatarConfig(gaze_amplitude=0.3)
    controller = AvatarParameterController(config, _capabilities(), rng=random.Random(3))
    controller.set_emotion(EmotionLabel.neutral, now=0.0)
    before = _parameter_values(controller.compose(now=0.2, vts_generation=1, model_generation=1))
    controller.set_emotion(EmotionLabel.explosion_mode, now=0.2)
    immediate = _parameter_values(controller.compose(now=0.2, vts_generation=1, model_generation=1))
    later = _parameter_values(controller.compose(now=1.0, vts_generation=1, model_generation=1))
    assert immediate["MouthSmile"] == before["MouthSmile"]
    assert later["MouthSmile"] > immediate["MouthSmile"]

    idle = AvatarParameterController(config, _capabilities(), rng=random.Random(11))
    speaking = AvatarParameterController(config, _capabilities(), rng=random.Random(11))
    speaking.set_speaking(True)
    idle_peak = speaking_peak = 0.0
    for index in range(400):
        now = index / 25
        idle_values = _parameter_values(idle.compose(now=now, vts_generation=1, model_generation=1))
        speaking_values = _parameter_values(
            speaking.compose(now=now, vts_generation=1, model_generation=1)
        )
        idle_peak = max(idle_peak, abs(idle_values["EyeLeftX"]))
        speaking_peak = max(speaking_peak, abs(speaking_values["EyeLeftX"]))
    assert speaking_peak < idle_peak


def test_controller_is_deterministic_and_bounded_for_twenty_thousand_frames() -> None:
    config = AvatarConfig()
    first = AvatarParameterController(config, _capabilities(), rng=random.Random(991))
    second = AvatarParameterController(config, _capabilities(), rng=random.Random(991))
    emotions = (
        EmotionLabel.neutral,
        EmotionLabel.focused,
        EmotionLabel.happy,
        EmotionLabel.worried,
        EmotionLabel.explosion_mode,
        EmotionLabel.sleepy,
    )

    blink_frames = 0
    for index in range(20_000):
        now = index / config.tick_hz
        if index % 2_000 == 0:
            emotion = emotions[(index // 2_000) % len(emotions)]
            first.set_emotion(emotion, now=now)
            second.set_emotion(emotion, now=now)
        if index % 333 == 0:
            mouth = ((index // 333) % 11) / 10.0
            speaking = (index // 333) % 2 == 0
            for controller in (first, second):
                controller.set_mouth(mouth)
                controller.set_speaking(speaking)

        first_frame = first.compose(
            now=now,
            vts_generation=7,
            model_generation=11,
        )
        second_frame = second.compose(
            now=now,
            vts_generation=7,
            model_generation=11,
        )
        assert first_frame == second_frame
        assert first_frame is not None
        assert first_frame.sequence == index + 1
        values = dict(first_frame.parameters)
        blink_frames += values["EyeOpenLeft"] < 0.2
        for parameter_id, value in first_frame.parameters:
            capability = _capabilities()[parameter_id]
            assert math.isfinite(value)
            assert capability.minimum <= value <= capability.maximum

    assert blink_frames > 0


def test_red_eye_system_manual_retrigger_and_stale_deadline() -> None:
    ownership = RedEyeOwnership(duration_seconds=2.0)

    assert ownership.trigger_system(now=1.0, vts_generation=2, model_generation=3) is (
        RedEyeCommand.activate
    )
    assert _red_eye_owner(ownership) is RedEyeOwner.system
    assert ownership.deadline == 3.0
    assert ownership.trigger_system(now=2.5, vts_generation=2, model_generation=3) is None
    assert ownership.deadline == 4.5
    assert ownership.due(now=3.1, vts_generation=2, model_generation=3) is None
    assert ownership.due(now=4.5, vts_generation=9, model_generation=3) is None
    assert _red_eye_owner(ownership) is RedEyeOwner.off

    assert ownership.trigger_system(now=5.0, vts_generation=2, model_generation=3) is (
        RedEyeCommand.activate
    )
    ownership.reconcile_manual(active=True)
    assert _red_eye_owner(ownership) is RedEyeOwner.manual
    assert ownership.trigger_system(now=8.0, vts_generation=2, model_generation=3) is None
    assert ownership.due(now=20.0, vts_generation=2, model_generation=3) is None
    ownership.reconcile_manual(active=False)
    assert _red_eye_owner(ownership) is RedEyeOwner.off

    assert ownership.reset() is RedEyeCommand.deactivate
    assert _red_eye_owner(ownership) is RedEyeOwner.off
