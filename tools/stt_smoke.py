"""Explicit local whisper.cpp smoke tool; never downloads a model automatically."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from desktop_client.inputs import (  # noqa: E402
    TranscriptionRequest,
    build_stt_provider,
    build_voice_input,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--mode",
        choices=("check", "file", "microphone"),
        default="check",
        help="check 只检查本地文件；file 转写现有 WAV；microphone 需人工按回车录音。",
    )
    parser.add_argument("--audio", type=Path, help="file 模式所需的 16kHz 单声道 PCM WAV。")
    return parser.parse_args()


async def _run() -> None:
    args = _arguments()
    settings = load_settings(args.config, args.env_file)
    if not settings.stt.enabled:
        raise SystemExit("STT 默认关闭；请先在本地配置中显式设置 stt.enabled=true。")

    executable = settings.stt_executable_path()
    model = settings.stt_model_path()
    missing = [str(path) for path in (executable, model) if not path.is_file()]
    if missing:
        raise SystemExit("缺少本地 whisper.cpp 运行文件：" + ", ".join(missing))

    if args.mode == "check":
        provider = build_stt_provider(settings)
        assert provider is not None
        await provider.close()
        print(f"whisper-cli: {executable}")
        print(f"model: {model}")
        print("本地运行文件检查通过；未访问麦克风，未执行转写。")
        return

    if args.mode == "file":
        if args.audio is None:
            raise SystemExit("file 模式必须传入 --audio。")
        provider = build_stt_provider(settings)
        assert provider is not None
        try:
            result = await provider.transcribe(
                TranscriptionRequest(
                    audio_path=args.audio.expanduser().resolve(),
                    language=settings.stt.language,
                    timeout_seconds=settings.stt.transcription_timeout_seconds,
                )
            )
            print(result.text)
        finally:
            await provider.close()
        return

    recorder = build_voice_input(settings)
    assert recorder is not None
    try:
        await asyncio.to_thread(input, "按回车开始录音（可能触发系统麦克风权限提示）...")
        await recorder.start()
        await asyncio.to_thread(input, "正在录音；按回车停止并进行本地转写...")
        message = await recorder.stop()
        print(message.text)
    finally:
        await recorder.close()


if __name__ == "__main__":
    asyncio.run(_run())
