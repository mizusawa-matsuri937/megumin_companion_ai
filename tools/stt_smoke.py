"""Explicit local MediaWorker STT smoke tool; never downloads a model automatically."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from desktop_client.inputs import build_voice_input  # noqa: E402
from desktop_client.inputs.voice_input import VoiceInputError  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--mode",
        choices=("check", "microphone"),
        default="check",
        help="check 在 MediaWorker 内预检本地运行时；microphone 需人工按回车录音。",
    )
    return parser.parse_args()


async def _run() -> None:
    args = _arguments()
    settings = load_settings(args.config, args.env_file)
    if not settings.stt.enabled:
        raise SystemExit("STT 默认关闭；请先在本地配置中显式设置 stt.enabled=true。")

    recorder = build_voice_input(settings)
    assert recorder is not None
    try:
        if args.mode == "check":
            try:
                await recorder.preflight()
            except VoiceInputError as exc:
                raise SystemExit(f"本地 STT 预检失败：{exc.code}") from exc
            print("本地 STT 预检通过；未访问麦克风，未执行转写。")
            return
        await asyncio.to_thread(input, "按回车开始录音（可能触发系统麦克风权限提示）...")
        await recorder.start()
        await asyncio.to_thread(input, "正在录音；按回车停止并进行本地转写...")
        message = await recorder.stop()
        print(message.text)
    finally:
        await recorder.close()


if __name__ == "__main__":
    asyncio.run(_run())
