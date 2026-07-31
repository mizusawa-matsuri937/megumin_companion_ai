"""Bounded, body-free contracts shared by MediaWorker and its parent."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

MAX_OUTPUT_DEVICES = 16
MAX_DEVICE_LABEL_CHARS = 160
_DEVICE_ID = re.compile(r"^audio_[0-9a-f]{32}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def output_device_id(
    *,
    host_api_name: str,
    device_name: str,
    maximum_output_channels: int,
    default_sample_rate: float,
) -> str:
    """Return a PortAudio-index-independent identifier for one output endpoint.

    PortAudio does not expose a Windows endpoint GUID through its portable API.
    The identifier is therefore stable across normal index reordering, but a
    collision is deliberately treated as non-selectable by the worker.
    """

    fingerprint = "\x1f".join(
        (
            "v1",
            host_api_name,
            device_name,
            str(maximum_output_channels),
            format(default_sample_rate, ".12g"),
        )
    )
    return "audio_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]


def clean_device_label(value: str) -> str:
    """Keep a device label bounded and safe for the local UI/protocol."""

    normalized = " ".join(value.replace("\x00", " ").split())
    normalized = normalized.encode("utf-8", errors="replace").decode("utf-8")
    return (normalized or "Audio output")[:MAX_DEVICE_LABEL_CHARS]


@dataclass(frozen=True, slots=True)
class AudioOutputDevice:
    """A selectable output device with no PortAudio index on the wire."""

    device_id: str
    label: str
    is_default: bool

    def __post_init__(self) -> None:
        if not _DEVICE_ID.fullmatch(self.device_id):
            raise ValueError("audio output device id is invalid")
        if (
            not isinstance(self.label, str)
            or not self.label
            or "\x00" in self.label
            or len(self.label) > MAX_DEVICE_LABEL_CHARS
        ):
            raise ValueError("audio output device label is invalid")
        if not isinstance(self.is_default, bool):
            raise ValueError("audio output default marker is invalid")


@dataclass(frozen=True, slots=True)
class OutputDeviceList:
    """The bounded result of a MediaWorker enumeration request."""

    devices: tuple[AudioOutputDevice, ...]
    truncated: bool
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if len(self.devices) > MAX_OUTPUT_DEVICES:
            raise ValueError("audio output device list exceeds protocol bound")
        if not isinstance(self.truncated, bool):
            raise ValueError("audio output device truncation marker is invalid")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str) or not _REASON_CODE.fullmatch(self.reason_code)
        ):
            raise ValueError("audio output device reason is invalid")


@dataclass(frozen=True, slots=True)
class MouthEnvelopeSample:
    """Content-free playback progress safe to cross the worker boundary."""

    turn_id: str
    playback_job_id: str
    sequence: int
    value: float
    terminal: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.turn_id, str)
            or not self.turn_id
            or len(self.turn_id) > 128
            or "\x00" in self.turn_id
            or not isinstance(self.playback_job_id, str)
            or not self.playback_job_id
            or len(self.playback_job_id) > 96
            or isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
            or isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not math.isfinite(self.value)
            or not 0.0 <= float(self.value) <= 1.0
            or not isinstance(self.terminal, bool)
        ):
            raise ValueError("mouth envelope sample invalid")
