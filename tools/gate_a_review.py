"""Manual Day 5-7 Gate A review without real text, credentials, or voice assets."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.llm import MockLLMProvider  # noqa: E402
from app.clients.tts import MockTTSProvider  # noqa: E402
from app.core import CancellationToken, TurnService  # noqa: E402
from app.pipelines import DialoguePipeline, DialogueSegmenter  # noqa: E402
from app.pipelines.audio_player import SystemAudioPlayer  # noqa: E402
from app.schemas import AudioResult, InputMode, TTSJob, TurnState, UserMessage  # noqa: E402

REVIEW_CACHE = Path("data/cache/audio/gate-a-review")


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
        output = []
        for chunk in chunks:
            output.extend(segment.text for segment in segmenter.feed(chunk))
        output.extend(segment.text for segment in segmenter.flush())
        print(f"\n[{label}]")
        for index, text in enumerate(output):
            print(f"  {index}: {text}")


async def review_order(volume: float) -> None:
    print("\n=== Day 6 音频顺序人工验收 ===")
    print("即将播放 3 个由低到高的纯合成提示音；合成完成顺序会被故意打乱。")
    player = SystemAudioPlayer()
    pipeline = DialoguePipeline(
        MockLLMProvider(
            deltas=["第一句话已经准备好了。", "第二句话也准备好了。", "第三句话完成！"],
            token_delay_seconds=0.03,
        ),
        MockTTSProvider(
            REVIEW_CACHE,
            duration_ms=650,
            volume=volume,
            delay_by_index={0: 0.30, 1: 0.03, 2: 0.01},
        ),
        player,
        tts_worker_count=3,
    )
    message = UserMessage(text="Gate A 音频顺序检查")
    state = TurnState(
        session_id=message.session_id,
        source_message_id=message.message_id,
        input_mode=message.input_mode,
    )
    token = CancellationToken(state.turn_id)

    async def emit(event_type: str, payload: dict[str, object]) -> None:
        if event_type in {"audio.ready", "playback.started", "playback.finished"}:
            print(f"{event_type:18} index={payload.get('index')}")

    try:
        metrics = await pipeline.run(message, state, token, emit)
        print("延迟指标：", metrics.model_dump(mode="json"))
    finally:
        await pipeline.close()


async def review_interruption(volume: float) -> None:
    print("\n=== Day 7 快速输入/打断人工验收 ===")
    print("先播放较低音的旧轮次；确认开始播放后约 0.65 秒自动提交新轮次。")
    print("随后只应听到较高音的新轮次，旧轮次低音不得恢复或与其重叠。")
    tts = SwitchingToneTTS(
        MockTTSProvider(REVIEW_CACHE, duration_ms=1000, volume=volume, synthesis_delay_seconds=0)
    )
    pipeline = DialoguePipeline(
        MockLLMProvider(
            response_factory=lambda message: (
                "旧轮次正在持续播放中。旧轮次后续绝不能播放。"
                if message.input_mode is InputMode.text
                else "新轮次已经接管播放。新轮次播放完成。"
            ),
            chunk_size=50,
            token_delay_seconds=0,
        ),
        tts,
        SystemAudioPlayer(),
        tts_worker_count=2,
    )
    logger = logging.getLogger("gate_a_review")
    logger.addHandler(logging.StreamHandler())
    service = TurnService(logger, pipeline)
    events = service.subscribe("*")
    first = await service.accept(UserMessage(text="旧轮次", input_mode=InputMode.text))
    print(f"旧 turn: {first.turn_id}")

    while True:
        event = await asyncio.wait_for(events.get(), timeout=10)
        events.task_done()
        if event.type in {"turn.accepted", "playback.started"}:
            print(f"{event.type:20} turn={event.turn_id} index={event.payload.get('index')}")
        if event.type == "playback.started" and event.turn_id == first.turn_id:
            break
    await asyncio.sleep(0.65)
    second = await service.accept(UserMessage(text="新轮次", input_mode=InputMode.voice))
    print(f"新 turn: {second.turn_id}")

    try:
        while True:
            event = await asyncio.wait_for(events.get(), timeout=10)
            events.task_done()
            if event.type in {
                "turn.accepted",
                "turn.cancelled",
                "playback.started",
                "playback.finished",
                "assistant.completed",
            }:
                print(f"{event.type:20} turn={event.turn_id} index={event.payload.get('index')}")
            if event.type == "assistant.completed" and event.turn_id == second.turn_id:
                break
    finally:
        service.unsubscribe("*", events)
        await service.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("segments", "audio", "interrupt", "all"),
        default="segments",
        help="默认只打印分句；audio/interrupt/all 会实际打开系统音频设备。",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=0.08,
        help="合成提示音振幅，范围 0.0-1.0；建议保持默认低音量。",
    )
    args = parser.parse_args()
    if not 0.0 <= args.volume <= 1.0:
        parser.error("--volume 必须位于 0.0 到 1.0")
    return args


async def main() -> None:
    args = parse_args()
    if args.mode in {"segments", "all"}:
        review_segments()
    if args.mode in {"audio", "all"}:
        await review_order(args.volume)
    if args.mode in {"interrupt", "all"}:
        await review_interruption(args.volume)


if __name__ == "__main__":
    asyncio.run(main())
