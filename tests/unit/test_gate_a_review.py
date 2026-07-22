"""No-device regression coverage for the Gate A reviewer utility."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from app.core import CancellationToken
from app.pipelines.audio_player import AudioPlaybackResult, SilentAudioPlayer
from app.schemas import AudioResult
from tools import gate_a_review


class _InterruptThenSkipPlayer:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def play(
        self,
        _result: AudioResult,
        token: CancellationToken,
    ) -> AudioPlaybackResult:
        self.calls += 1
        if self.calls == 1:
            await token.wait()
            token.raise_if_cancelled()
        return AudioPlaybackResult(played=False, error_code="audio_worker_failed")

    async def stop(self, *, immediate: bool = False) -> None:
        del immediate

    async def close(self) -> None:
        self.closed = True


def test_gate_a_review_renders_playback_failure_codes() -> None:
    assert (
        gate_a_review._review_event_line(
            "playback.skipped", {"index": 1, "error_code": "audio_device_unavailable"}
        )
        == "playback.skipped   index=1 code=audio_device_unavailable"
    )
    assert gate_a_review._review_event_line("playback.finished", {"index": 2}) == (
        "playback.finished  index=2"
    )


def test_gate_a_review_all_modes_dry_run_exercises_interrupt_path() -> None:
    root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "tools/gate_a_review.py", "--mode", "all", "--dry-run"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "dry-run" in completed.stdout
    assert "Day 6 音频顺序人工验收" in completed.stdout
    assert "Day 7 快速输入/打断人工验收" in completed.stdout
    assert "新 turn:" in completed.stdout
    assert "Day 7 打断人工验收：事件顺序通过" in completed.stdout
    assert "Gate A 清理完成。" in completed.stdout


def test_gate_a_review_rejects_a_replacement_turn_with_skipped_audio(tmp_path: Path) -> None:
    player = _InterruptThenSkipPlayer()

    with pytest.raises(RuntimeError, match="新轮次音频未完整播放"):
        asyncio.run(
            gate_a_review.review_interruption(
                0,
                cache=tmp_path,
                player_factory=lambda: player,
            )
        )

    assert player.closed


def test_gate_a_review_waits_for_listener_confirmation_before_interrupting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    confirmations: list[str] = []

    async def confirm_low_tone() -> None:
        # Yield once so the realtime fake has begun its first playback before
        # the simulated listener acknowledges the low tone.
        await asyncio.sleep(0)
        confirmations.append("confirmed")

    asyncio.run(
        gate_a_review.review_interruption(
            0,
            cache=tmp_path,
            player_factory=lambda: SilentAudioPlayer(realtime=True),
            wait_for_audible_confirmation=True,
            confirmation_waiter=confirm_low_tone,
        )
    )

    output = capsys.readouterr().out
    assert confirmations == ["confirmed"]
    assert "仅表示播放请求已调度" in output
    assert "Day 7 打断人工验收：事件顺序通过" in output


def test_real_interrupt_mode_requests_audible_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    confirmations: list[bool] = []

    async def fake_review_interruption(
        _volume: float,
        *,
        cache: Path,
        player_factory: gate_a_review.AudioPlayerFactory,
        wait_for_audible_confirmation: bool = False,
        confirmation_waiter: gate_a_review.AudibleConfirmationWaiter = (
            gate_a_review._wait_for_audible_low_tone
        ),
    ) -> None:
        del cache, player_factory, confirmation_waiter
        confirmations.append(wait_for_audible_confirmation)

    monkeypatch.setattr(gate_a_review, "review_interruption", fake_review_interruption)

    asyncio.run(gate_a_review.main(["--mode", "interrupt", "--volume", "0"]))

    assert confirmations == [True]


def test_audible_confirmation_rejects_noninteractive_input(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NonInteractiveInput:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", _NonInteractiveInput())

    with pytest.raises(RuntimeError, match="需要交互式终端"):
        asyncio.run(gate_a_review._wait_for_audible_low_tone())
