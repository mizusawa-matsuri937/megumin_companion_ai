"""Stable, side-effect-free packaging entry point for the Windows desktop shell."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from app import __version__
from app.cli import check_configuration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megumin-companion-desktop",
        description=(
            "Windows desktop shell. Qt owns the main thread and the backend owns an "
            "independent asyncio thread."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help="Validate an explicit YAML configuration.")
    parser.add_argument("--env-file", type=Path, help="Use an explicit development .env file.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting Qt, a database, device, or network.",
    )
    parser.add_argument("--headless-smoke", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_config:
        return check_configuration(args.config, args.env_file)
    if args.headless_smoke:
        return start_headless_smoke()
    return start_desktop()


def start_desktop() -> int:
    from desktop_client.ui.application import run_desktop

    return run_desktop()


def start_headless_smoke() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from desktop_client.ui.application import run_headless_smoke

    return run_headless_smoke()
