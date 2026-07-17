"""Stable, side-effect-free packaging entry point for the future Windows desktop shell."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app import __version__
from app.cli import check_configuration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megumin-companion-desktop",
        description=(
            "Windows desktop packaging preflight. The PySide6 shell is introduced by W13; "
            "this W01 entry point only validates package and configuration availability."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help="Validate an explicit YAML configuration.")
    parser.add_argument("--env-file", type=Path, help="Use an explicit development .env file.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting a database, device, or network.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_config:
        return check_configuration(args.config, args.env_file)
    parser.print_usage(sys.stderr)
    print(
        "desktop_unavailable: the PySide6 desktop shell is intentionally deferred to W13.",
        file=sys.stderr,
    )
    return 3
