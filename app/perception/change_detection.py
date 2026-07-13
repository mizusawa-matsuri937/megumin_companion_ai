"""In-memory frame change detection without retaining screenshots."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

from app.perception.models import ImageFrame, WindowInfo


@dataclass(frozen=True, slots=True)
class _Signature:
    perceptual_hash: int | None
    digest: bytes


class FrameChangeDetector:
    def __init__(self, *, minimum_hash_distance: float = 0.08, max_windows: int = 128) -> None:
        if not 0.0 <= minimum_hash_distance <= 1.0:
            raise ValueError("minimum_hash_distance 必须在 0 到 1 之间")
        if max_windows < 1:
            raise ValueError("max_windows 必须大于 0")
        self._minimum_hash_distance = minimum_hash_distance
        self._max_windows = max_windows
        self._last_window_key: bytes | None = None
        self._signatures: OrderedDict[bytes, _Signature] = OrderedDict()

    def should_analyze(self, window: WindowInfo, frame: ImageFrame) -> bool:
        signature = _Signature(
            perceptual_hash=frame.perceptual_hash,
            digest=hashlib.blake2b(frame.data, digest_size=16).digest(),
        )
        window_key = hashlib.blake2b(window.window_id.encode("utf-8"), digest_size=16).digest()
        previous = self._signatures.get(window_key)
        window_changed = self._last_window_key != window_key
        self._last_window_key = window_key
        self._signatures[window_key] = signature
        self._signatures.move_to_end(window_key)
        while len(self._signatures) > self._max_windows:
            self._signatures.popitem(last=False)
        if previous is None or window_changed:
            return True
        if previous.perceptual_hash is not None and signature.perceptual_hash is not None:
            differing_bits = (previous.perceptual_hash ^ signature.perceptual_hash).bit_count()
            bit_count = max(
                previous.perceptual_hash.bit_length(), signature.perceptual_hash.bit_length(), 64
            )
            return differing_bits / bit_count >= self._minimum_hash_distance
        return previous.digest != signature.digest

    def reset(self) -> None:
        self._last_window_key = None
        self._signatures.clear()
