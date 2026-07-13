"""In-memory frame change detection without retaining screenshots."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

from app.perception.models import FrameChangeAssessment, ImageFrame, WindowInfo


@dataclass(frozen=True, slots=True)
class _Signature:
    perceptual_hash: int | None
    digest: bytes
    sensitive: bool


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

    def compare(self, window: WindowInfo, frame: ImageFrame) -> FrameChangeAssessment:
        """Compare without mutating committed privacy state."""

        digest = hashlib.blake2b(frame.data, digest_size=16).digest()
        window_key = hashlib.blake2b(window.window_id.encode("utf-8"), digest_size=16).digest()
        previous = self._signatures.get(window_key)
        window_changed = self._last_window_key != window_key
        if previous is None or window_changed:
            changed = True
        elif previous.perceptual_hash is not None and frame.perceptual_hash is not None:
            differing_bits = (previous.perceptual_hash ^ frame.perceptual_hash).bit_count()
            bit_count = max(
                previous.perceptual_hash.bit_length(), frame.perceptual_hash.bit_length(), 64
            )
            changed = differing_bits / bit_count >= self._minimum_hash_distance
        else:
            changed = previous.digest != digest
        return FrameChangeAssessment(
            window_key=window_key,
            digest=digest,
            perceptual_hash=frame.perceptual_hash,
            changed=changed,
            previous_sensitive=previous.sensitive if previous is not None else False,
        )

    def commit(self, assessment: FrameChangeAssessment, *, sensitive: bool) -> None:
        """Commit only a frame whose local privacy outcome is known."""

        self._last_window_key = assessment.window_key
        self._signatures[assessment.window_key] = _Signature(
            perceptual_hash=assessment.perceptual_hash,
            digest=assessment.digest,
            sensitive=sensitive,
        )
        self._signatures.move_to_end(assessment.window_key)
        while len(self._signatures) > self._max_windows:
            self._signatures.popitem(last=False)

    def reset(self) -> None:
        self._last_window_key = None
        self._signatures.clear()
