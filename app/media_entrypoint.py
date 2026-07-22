"""Private MediaWorker entry point; it is not a network or CLI surface."""

from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import Sequence
from pathlib import Path

from app.media.stt import WhisperCppConfig
from app.media.worker import MediaWorkerHandler
from app.workers import ApprovedResourcePolicy
from app.workers.access import ResourceAccessError
from app.workers.helper import HelperRuntime

_ROOT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_OUTPUT_DEVICE_ID = re.compile(r"^audio_[0-9a-f]{32}$")


def _root_argument(value: str) -> tuple[str, Path]:
    root_id, separator, raw_path = value.partition("=")
    if not separator or not _ROOT_ID.fullmatch(root_id):
        raise argparse.ArgumentTypeError("invalid root")
    path = Path(raw_path)
    if not raw_path or not path.is_absolute():
        raise argparse.ArgumentTypeError("invalid root")
    return root_id, path


def _absolute_path_argument(value: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute():
        raise argparse.ArgumentTypeError("invalid absolute path")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", action="append", type=_root_argument, required=True)
    parser.add_argument("--output-device-id", default="")
    parser.add_argument("--maximum-wave-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--stt-executable", type=_absolute_path_argument)
    parser.add_argument("--stt-model", type=_absolute_path_argument)
    parser.add_argument("--stt-temporary-root", type=_absolute_path_argument)
    parser.add_argument("--stt-executable-prefix", action="append", default=[])
    parser.add_argument("--stt-language", default="auto")
    parser.add_argument("--stt-threads", type=int)
    parser.add_argument("--stt-terminate-grace-seconds", type=float, default=0.5)
    parser.add_argument("--stt-max-audio-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--stt-max-output-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--stt-maximum-recording-seconds", type=float, default=120.0)
    parser.add_argument("--stt-transcription-timeout-seconds", type=float, default=60.0)
    device_group = parser.add_mutually_exclusive_group()
    device_group.add_argument("--input-device-index", type=int)
    device_group.add_argument("--input-device-name")
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots: dict[str, Path] = {}
    for root_id, path in args.root:
        if root_id in roots:
            return 2
        roots[root_id] = path
    if args.output_device_id and not _OUTPUT_DEVICE_ID.fullmatch(args.output_device_id):
        return 2
    stt_values = (args.stt_executable, args.stt_model, args.stt_temporary_root)
    if any(value is not None for value in stt_values) and not all(
        value is not None for value in stt_values
    ):
        return 2
    if args.stt_temporary_root is not None and args.stt_temporary_root.resolve(
        strict=False
    ) not in {root.resolve(strict=False) for root in roots.values()}:
        return 2
    if args.input_device_name is not None and (
        not args.input_device_name
        or "\x00" in args.input_device_name
        or len(args.input_device_name) > 256
    ):
        return 2
    if args.input_device_index is not None and args.input_device_index < 0:
        return 2
    stt_config: WhisperCppConfig | None = None
    if args.stt_executable is not None:
        try:
            stt_config = WhisperCppConfig(
                executable=args.stt_executable,
                model_path=args.stt_model,
                executable_prefix_args=tuple(args.stt_executable_prefix),
                threads=args.stt_threads,
                terminate_grace_seconds=args.stt_terminate_grace_seconds,
                max_audio_bytes=args.stt_max_audio_bytes,
                max_output_bytes=args.stt_max_output_bytes,
            )
        except (TypeError, ValueError):
            return 2
    try:
        policy = ApprovedResourcePolicy(roots=roots)
        handler_options: dict[str, object] = {
            "selected_device_id": args.output_device_id or None,
            "maximum_wave_bytes": args.maximum_wave_bytes,
        }
        if stt_config is not None:
            handler_options.update(
                {
                    "stt_config": stt_config,
                    "recording_root": args.stt_temporary_root,
                    "input_device": (
                        args.input_device_index
                        if args.input_device_index is not None
                        else args.input_device_name
                    ),
                    "maximum_recording_seconds": args.stt_maximum_recording_seconds,
                    "transcription_timeout_seconds": args.stt_transcription_timeout_seconds,
                    "language": args.stt_language,
                }
            )
        handler = MediaWorkerHandler(
            **handler_options,  # type: ignore[arg-type]
        )
    except (ResourceAccessError, ValueError):
        return 2
    return await HelperRuntime(
        role="media",
        handler=handler,
        resource_policy=policy,
        maximum_active_jobs=1,
    ).run()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
