"""Private MediaWorker entry point; it is not a network or CLI surface."""

from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import Sequence
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", action="append", type=_root_argument, required=True)
    parser.add_argument("--output-device-id", default="")
    parser.add_argument("--maximum-wave-bytes", type=int, default=32 * 1024 * 1024)
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
    try:
        policy = ApprovedResourcePolicy(roots=roots)
        handler = MediaWorkerHandler(
            selected_device_id=args.output_device_id or None,
            maximum_wave_bytes=args.maximum_wave_bytes,
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
