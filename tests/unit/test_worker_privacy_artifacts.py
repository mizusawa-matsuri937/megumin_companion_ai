"""Cross-artifact sentinel scan for W12 worker diagnostics."""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import asdict
from pathlib import Path

from app.config.logging import close_logging, configure_logging, log_event
from app.config.settings import LoggingConfig, Settings
from app.diagnostics import DiagnosticExporter
from app.paths import AppPaths
from app.temp_assets import TempAssetRegistry
from app.workers.supervisor import SupervisorEvent, WorkerActualState


def test_worker_sentinel_never_reaches_event_log_temp_or_diagnostic_bundle(
    tmp_path: Path,
) -> None:
    body = "W12_TRANSCRIPT_OCR_PCM_BODY_SENTINEL"
    secret = "sk-W12SyntheticSecret123456789"
    user_path = "C:\\Users\\private-user\\recording.wav"
    paths = AppPaths(root=tmp_path / "LocalAppData" / "MeguminCompanion")
    paths.logs.mkdir(parents=True)
    paths.temp.mkdir(parents=True)
    settings = Settings(
        logging=LoggingConfig(
            console_enabled=False,
            file_enabled=True,
            file_path=Path("app.jsonl"),
        )
    )
    settings._paths = paths
    logger = configure_logging(settings, additional_secrets=(secret,))
    log_event(
        logger,
        logging.ERROR,
        "worker.hard_fault",
        error_code="worker_job_deadline",
        stderr=body,
        transcript=body,
        ocr_text=body,
        pcm=body,
        path=user_path,
        secret=secret,
    )
    close_logging(logger)

    event = SupervisorEvent(
        code="worker.hard_fault",
        state=WorkerActualState.failed,
        monotonic_seconds=1.0,
        count=1,
    )
    event_bytes = json.dumps(asdict(event), default=str).encode("utf-8")
    assert all(value.encode() not in event_bytes for value in (body, secret, user_path))

    registry = TempAssetRegistry(paths, minimum_scavenge_age_seconds=0)
    registry.scavenge()
    managed_bytes = b"".join(
        candidate.read_bytes() for candidate in paths.root.rglob("*") if candidate.is_file()
    )
    for value in (body, secret, user_path):
        assert value.encode() not in managed_bytes

    destination = tmp_path / "diagnostics.zip"
    DiagnosticExporter(
        paths,
        known_secrets=(secret,),
        forbidden_values=(body, user_path),
    ).export(destination)
    with zipfile.ZipFile(destination) as archive:
        bundle = b"".join(archive.read(name) for name in archive.namelist())
    for value in (body, secret, user_path):
        assert value.encode() not in bundle
