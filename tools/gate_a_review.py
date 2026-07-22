"""Manual Day 5-7 Gate A review without real text, credentials, or voice assets."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.llm import MockLLMProvider  # noqa: E402
from app.clients.tts import MockTTSProvider  # noqa: E402
from app.core import CancellationToken, TurnService  # noqa: E402
from app.media import MediaWorkerAudioPlayer  # noqa: E402
from app.pipelines import DialoguePipeline, DialogueSegmenter  # noqa: E402
from app.pipelines.audio_player import AudioPlayer, SilentAudioPlayer  # noqa: E402
from app.schemas import (  # noqa: E402
    AudioResult,
    InputMode,
    PipelineEvent,
    TTSJob,
    TurnState,
    UserMessage,
)

AudioPlayerFactory = Callable[[], AudioPlayer]
AudibleConfirmationWaiter = Callable[[], Awaitable[None]]
_AUDIO_REVIEW_EVENTS = frozenset(
    {
        "audio.ready",
        "audio.degraded",
        "playback.started",
        "playback.finished",
        "playback.skipped",
    }
)
_TURN_REVIEW_EVENTS = frozenset(
    {
        "turn.accepted",
        "turn.cancelled",
        "turn.failed",
        "assistant.completed",
    }
)


class SwitchingToneTTS:
    """Give the replacement turn a clearly higher pitch during the interruption review."""

    def __init__(self, provider: MockTTSProvider) -> None:
        self._provider = provider

    async def synthesize(
        self,
        job: TTSJob,
        *,
        segment_index: int,
        token: CancellationToken,
    ) -> AudioResult:
        audible_index = segment_index + (4 if "新轮次" in job.text else 0)
        return await self._provider.synthesize(
            job,
            segment_index=audible_index,
            token=token,
        )

    async def discard(self, result: AudioResult) -> None:
        await self._provider.discard(result)

    async def close(self) -> None:
        await self._provider.close()


def _review_event_line(event_type: str, payload: dict[str, object]) -> str:
    """Render no-content playback evidence so a skipped device is not hidden."""

    detail = payload.get("error_code") or payload.get("reason")
    suffix = f" code={detail}" if isinstance(detail, str) and detail else ""
    return f"{event_type:18} index={payload.get('index')}{suffix}"


def _turn_review_event_line(event: PipelineEvent) -> str:
    """Render terminal turn evidence without relying on logger formatting."""

    detail = event.payload.get("error_code")
    suffix = f" code={detail}" if isinstance(detail, str) and detail else ""
    return f"{event.type:20} turn={event.turn_id} index={event.payload.get('index')}{suffix}"


async def _wait_for_audible_low_tone() -> None:
    """Require a real listener to confirm output before the Day 7 interruption."""

    if not sys.stdin.isatty():
        raise RuntimeError(
            "Gate A 打断听感验收需要交互式终端；请在实际听到低音后按 Enter，"
            "或使用 --dry-run 运行自动顺序检查。"
        )
    await asyncio.to_thread(input, "听到仍在播放的低音后，请立即按 Enter 提交新轮次：")


def review_segments() -> None:
    samples = {
        "中文短句合并": ["你好。", "今天一起努力吧！"],
        "英文缩写与小数": ["Dr. Smith paid 3.14 dollars. ", "Really?"],
        "日文标点": ["これはテストです。次も大丈夫！"],
        "短反应词": ["嗯？", "我听到了。"],
        "省略号": ["吾之爆裂魔法……", "准备完毕！"],
    }
    print("\n=== Day 5 分句人工阅读 ===")
    for label, chunks in samples.items():
        segmenter = DialogueSegmenter("turn_review")
        output: list[str] = []
        for chunk in chunks:
            output.extend(segment.text for segment in segmenter.feed(chunk))
        output.extend(segment.text for segment in segmenter.flush())
        print(f"\n[{label}]")
        for index, text in enumerate(output):
            print(f"  {index}: {text}")


async def review_order(
    volume: float,
    *,
    cache: Path,
    player_factory: AudioPlayerFactory,
) -> None:
    print("\n=== Day 6 音频顺序人工验收 ===")
    print("即将播放 3 个由低到高的纯合成提示音；合成完成顺序会被故意打乱。")
    player = player_factory()
    pipeline = DialoguePipeline(
        MockLLMProvider(
            deltas=["第一句话已经准备好了。", "第二句话也准备好了。", "第三句话完成！"],
            token_delay_seconds=0.03,
        ),
        MockTTSProvider(
            cache,
            duration_ms=650,
            volume=volume,
            delay_by_index={0: 0.30, 1: 0.03, 2: 0.01},
        ),
        player,
        tts_worker_count=3,
        tts_connect_timeout_ms=500,
        tts_first_byte_timeout_ms=500,
        tts_total_timeout_ms=5_000,
        tts_cancellation_timeout_ms=500,
    )
    message = UserMessage(text="Gate A 音频顺序检查")
    state = TurnState(
        session_id=message.session_id,
        source_message_id=message.message_id,
        input_mode=message.input_mode,
    )
    token = CancellationToken(state.turn_id)

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        if event_type in _AUDIO_REVIEW_EVENTS:
            print(_review_event_line(event_type, payload))

    try:
        metrics = await pipeline.run(message, state, token, emit)
        print("延迟指标：", metrics.model_dump(mode="json"))
        if metrics.metrics.playback_count != 3:
            raise RuntimeError(
                "Gate A 音频顺序验收失败：3 个合成提示音没有全部完成播放；"
                "请查看 playback.skipped 的 code。"
            )
    finally:
        await pipeline.close()


async def review_interruption(
    volume: float,
    *,
    cache: Path,
    player_factory: AudioPlayerFactory,
    wait_for_audible_confirmation: bool = False,
    confirmation_waiter: AudibleConfirmationWaiter = _wait_for_audible_low_tone,
) -> None:
    print("\n=== Day 7 快速输入/打断人工验收 ===")
    if wait_for_audible_confirmation:
        print("先播放较低音的旧轮次；实际听到低音后按 Enter 自动提交新轮次。")
    else:
        print("dry-run：旧轮次调度后约 0.65 秒自动提交新轮次。")
    print("随后只应听到较高音的新轮次，旧轮次低音不得恢复或与其重叠。")
    tts = SwitchingToneTTS(
        MockTTSProvider(cache, duration_ms=1000, volume=volume, synthesis_delay_seconds=0)
    )
    responses = iter(
        (
            "旧轮次正在持续播放中。旧轮次后续绝不能播放。",
            "新轮次已经接管播放。新轮次播放完成。",
        )
    )
    pipeline = DialoguePipeline(
        MockLLMProvider(
            # MockLLMProvider receives ChatRequest, not UserMessage. The turn
            # order is deterministic in this isolated reviewer, so do not read
            # an input-mode attribute that the request contract never has.
            response_factory=lambda _request: next(responses, "新轮次播放完成。"),
            chunk_size=50,
            token_delay_seconds=0,
        ),
        tts,
        player_factory(),
        tts_worker_count=2,
        tts_connect_timeout_ms=500,
        tts_first_byte_timeout_ms=500,
        tts_total_timeout_ms=5_000,
        tts_cancellation_timeout_ms=500,
    )
    logger = logging.getLogger("gate_a_review")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    service = TurnService(logger, pipeline)
    session_id = "gate_a_review"
    events = await service.subscribe(session_id)
    try:
        first = await service.accept(
            UserMessage(text="旧轮次", input_mode=InputMode.text, session_id=session_id)
        )
        print(f"旧 turn: {first.turn_id}")

        while True:
            event = await asyncio.wait_for(events.get(), timeout=10)
            events.task_done()
            if not isinstance(event, PipelineEvent):
                continue
            if event.type in _TURN_REVIEW_EVENTS:
                print(_turn_review_event_line(event))
            elif event.type in _AUDIO_REVIEW_EVENTS:
                print(f"{_review_event_line(event.type, event.payload)} turn={event.turn_id}")
            if event.type == "turn.failed" and event.turn_id == first.turn_id:
                raise RuntimeError(
                    "Gate A 打断验收失败：旧轮次在替换输入前进入 turn.failed；"
                    "请记录上方稳定 error code。"
                )
            if event.type == "playback.started" and event.turn_id == first.turn_id:
                break

        if wait_for_audible_confirmation:
            print("已收到 playback.started：它仅表示播放请求已调度，不代表声音已经到达扬声器。")
            await confirmation_waiter()
        else:
            await asyncio.sleep(0.65)
        second = await service.accept(
            UserMessage(text="新轮次", input_mode=InputMode.voice, session_id=session_id)
        )
        print(f"新 turn: {second.turn_id}")

        first_cancelled = False
        first_failed = False
        first_completed = False
        replacement_finished: set[int] = set()
        replacement_skipped: list[int] = []
        while True:
            event = await asyncio.wait_for(events.get(), timeout=10)
            events.task_done()
            if not isinstance(event, PipelineEvent):
                continue
            if event.type in _TURN_REVIEW_EVENTS:
                print(_turn_review_event_line(event))
            elif event.type in _AUDIO_REVIEW_EVENTS:
                print(f"{_review_event_line(event.type, event.payload)} turn={event.turn_id}")
            if event.turn_id == first.turn_id:
                first_cancelled = first_cancelled or event.type == "turn.cancelled"
                first_failed = first_failed or event.type == "turn.failed"
                first_completed = first_completed or event.type == "assistant.completed"
            if event.turn_id == second.turn_id:
                if event.type == "playback.finished":
                    index = event.payload.get("index")
                    if isinstance(index, int):
                        replacement_finished.add(index)
                elif event.type == "playback.skipped":
                    index = event.payload.get("index")
                    if isinstance(index, int):
                        replacement_skipped.append(index)
            if event.type == "assistant.completed" and event.turn_id == second.turn_id:
                break

        if first_failed or not first_cancelled:
            if first_completed:
                raise RuntimeError(
                    "Gate A 打断验收无效：旧轮次已在确认前完成；"
                    "请重新运行，并在低音仍在播放时立即按 Enter。"
                )
            raise RuntimeError("Gate A 打断验收失败：旧轮次没有在新输入后以 turn.cancelled 收束。")
        if replacement_skipped or replacement_finished != {0, 1}:
            raise RuntimeError(
                "Gate A 打断验收失败：新轮次音频未完整播放；请查看 playback.skipped 的 code。"
            )
        print("Day 7 打断人工验收：事件顺序通过，等待实际听感确认。")
    finally:
        service.unsubscribe(events)
        print("Gate A 清理 MediaWorker…")
        await service.shutdown()
        print("Gate A 清理完成。")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("segments", "audio", "interrupt", "all"),
        default="segments",
        help="默认只打印分句；audio/interrupt/all 会实际通过 MediaWorker 打开系统音频设备。",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=0.08,
        help="合成提示音振幅，范围 0.0-1.0；建议保持默认低音量。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="运行 audio/interrupt 流程但不启动 MediaWorker 或打开真实设备。",
    )
    args = parser.parse_args(argv)
    if not 0.0 <= args.volume <= 1.0:
        parser.error("--volume 必须位于 0.0 到 1.0")
    return args


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    with TemporaryDirectory(prefix="megumin-gate-a-") as temporary_directory:
        cache = Path(temporary_directory)
        player_factory: AudioPlayerFactory
        if args.dry_run:
            print("dry-run：使用时间模拟，不会启动 MediaWorker 或打开真实音频设备。")

            # Keeping timing makes the interruption path exercise cancellation
            # rather than merely executing two completed silent turns.
            def create_dry_run_player() -> AudioPlayer:
                return SilentAudioPlayer(realtime=True)

            player_factory = create_dry_run_player
        else:

            def create_media_worker_player() -> AudioPlayer:
                return MediaWorkerAudioPlayer.for_review(cache)

            player_factory = create_media_worker_player
        if args.mode in {"segments", "all"}:
            review_segments()
        if args.mode in {"audio", "all"}:
            await review_order(args.volume, cache=cache, player_factory=player_factory)
        if args.mode in {"interrupt", "all"}:
            await review_interruption(
                args.volume,
                cache=cache,
                player_factory=player_factory,
                wait_for_audible_confirmation=not args.dry_run,
            )


if __name__ == "__main__":
    asyncio.run(main())
