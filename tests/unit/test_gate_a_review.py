"""No-device regression coverage for the Gate A reviewer utility."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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
