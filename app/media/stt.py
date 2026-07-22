"""Helper-private whisper.cpp preflight, execution, and result parsing.

This module is imported only by :mod:`app.media.worker`, inside a supervised
``MediaWorker``.  It deliberately accepts a worker-private WAV path and emits
only bounded transcript metadata; PCM, WAV bytes, JSON bodies, command-line
details, and stderr never leave the helper process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import stat
import struct
import subprocess
import wave
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.windows_security import ReparsePointError, assert_no_reparse_points

MAX_TRANSCRIPT_CHARS = 4_096
MAX_LANGUAGE_CHARS = 64
_FINGERPRINT_SAMPLE_BYTES = 64 * 1024
_VERSION_TIMEOUT_SECONDS = 5.0
_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


class WhisperRuntimeError(RuntimeError):
    """A stable, content-free local STT failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WhisperCppConfig:
    """Static worker-side configuration for one explicitly configured runtime."""

    executable: Path
    model_path: Path
    executable_prefix_args: tuple[str, ...] = ()
    threads: int | None = None
    terminate_grace_seconds: float = 0.5
    max_audio_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.threads is not None and self.threads < 1:
            raise ValueError("threads 必须大于 0")
        if self.terminate_grace_seconds <= 0:
            raise ValueError("terminate_grace_seconds 必须大于 0")
        if self.max_audio_bytes < 44 or self.max_output_bytes < 1:
            raise ValueError("音频和输出大小上限无效")
        if any(not argument or "\x00" in argument for argument in self.executable_prefix_args):
            raise ValueError("whisper 前缀参数无效")


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Bounded text returned across the trusted helper pipe."""

    text: str
    language: str | None = None
    segment_count: int = 0

    def __post_init__(self) -> None:
        normalized = self.text.strip()
        if not normalized:
            raise ValueError("转写文本不能为空")
        if "\x00" in normalized or len(normalized) > MAX_TRANSCRIPT_CHARS:
            raise ValueError("转写文本超出 helper 边界")
        if self.language is not None and (
            not self.language or "\x00" in self.language or len(self.language) > MAX_LANGUAGE_CHARS
        ):
            raise ValueError("转写语言无效")
        if self.segment_count < 0:
            raise ValueError("segment_count 不能为负数")
        object.__setattr__(self, "text", normalized)


@dataclass(frozen=True, slots=True)
class WhisperRuntimeFingerprint:
    """Private preflight evidence retained in the helper, never logged or returned."""

    executable_size: int
    executable_mtime_ns: int
    executable_sha256: str
    model_size: int
    model_mtime_ns: int
    model_fingerprint: str
    architecture: str


class WhisperCppRunner:
    """Run one whisper process under the surrounding MediaWorker Job Object.

    The parent ``WorkerSupervisor`` owns the Job Object containing this helper,
    so every process started here is in that tree.  This runner still performs
    a bounded terminate-then-kill sequence for normal cancellation and timeout;
    a non-settling native process is then handled by the supervisor's Job kill.
    """

    def __init__(self, config: WhisperCppConfig) -> None:
        self._config = config
        self._fingerprint: WhisperRuntimeFingerprint | None = None
        self._fingerprint_signature: tuple[int, int, int, int] | None = None
        self._runtime_paths: tuple[Path, Path] | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._lock = asyncio.Lock()
        self._preflight_lock = asyncio.Lock()
        self._closed = False

    async def preflight(self) -> None:
        """Validate paths, architecture, fingerprints, and ``--version`` lazily."""

        async with self._preflight_lock:
            async with self._lock:
                if self._closed:
                    raise WhisperRuntimeError("stt_worker_closed")
            signature, fingerprint, runtime_paths = await asyncio.to_thread(self._inspect_runtime)
            async with self._lock:
                if self._closed:
                    raise WhisperRuntimeError("stt_worker_closed")
                if signature == self._fingerprint_signature and self._fingerprint is not None:
                    self._runtime_paths = runtime_paths
                    return
            await self._probe_version(runtime_paths[0])
            async with self._lock:
                if self._closed:
                    raise WhisperRuntimeError("stt_worker_closed")
                self._fingerprint_signature = signature
                self._fingerprint = fingerprint
                self._runtime_paths = runtime_paths

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str,
        timeout_seconds: float,
        cancelled: asyncio.Event,
    ) -> TranscriptionResult:
        if timeout_seconds <= 0:
            raise WhisperRuntimeError("stt_timeout_invalid")
        await self.preflight()
        async with self._lock:
            if self._closed or self._runtime_paths is None:
                raise WhisperRuntimeError("stt_worker_closed")
            executable, model_path = self._runtime_paths
        validated_audio = await asyncio.to_thread(self._validate_audio, audio_path)
        output_base = validated_audio.with_name("transcript")
        process = await self._start_process(
            self._build_command(
                validated_audio,
                output_base,
                language,
                executable=executable,
                model_path=model_path,
            )
        )
        try:
            await self._wait_for_process(process, timeout_seconds, cancelled)
            if process.returncode != 0:
                raise WhisperRuntimeError("stt_process_failed")
            return await asyncio.to_thread(self._read_result, output_base.with_suffix(".json"))
        finally:
            await self._terminate(process)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            processes = tuple(self._processes)
        await asyncio.gather(
            *(self._terminate(process) for process in processes),
            return_exceptions=True,
        )

    def _inspect_runtime(
        self,
    ) -> tuple[tuple[int, int, int, int], WhisperRuntimeFingerprint, tuple[Path, Path]]:
        executable = _validated_regular_file(self._config.executable, "stt_executable_missing")
        model = _validated_regular_file(self._config.model_path, "stt_model_missing")
        executable_stat = executable.stat()
        model_stat = model.stat()
        architecture = _executable_architecture(executable)
        host_architecture = _host_architecture()
        if architecture is not None and architecture != host_architecture:
            raise WhisperRuntimeError("stt_architecture_incompatible")
        if struct.calcsize("P") != 8:
            raise WhisperRuntimeError("stt_architecture_incompatible")
        return (
            (
                executable_stat.st_size,
                executable_stat.st_mtime_ns,
                model_stat.st_size,
                model_stat.st_mtime_ns,
            ),
            WhisperRuntimeFingerprint(
                executable_size=executable_stat.st_size,
                executable_mtime_ns=executable_stat.st_mtime_ns,
                executable_sha256=_sha256_file(executable),
                model_size=model_stat.st_size,
                model_mtime_ns=model_stat.st_mtime_ns,
                model_fingerprint=_sampled_fingerprint(model),
                architecture=architecture or host_architecture,
            ),
            (executable, model),
        )

    async def _probe_version(self, executable: Path) -> None:
        """Confirm that the configured executable can execute a version probe.

        Output is intentionally discarded: it may contain a local build path and
        is not needed after an exit-status preflight.
        """

        process = await self._start_process(
            (
                str(executable),
                *self._config.executable_prefix_args,
                "--version",
            )
        )
        try:
            try:
                async with asyncio.timeout(_VERSION_TIMEOUT_SECONDS):
                    await process.wait()
            except TimeoutError as exc:
                raise WhisperRuntimeError("stt_version_probe_timeout") from exc
            if process.returncode != 0:
                raise WhisperRuntimeError("stt_version_probe_failed")
        finally:
            await self._terminate(process)

    def _build_command(
        self,
        audio_path: Path,
        output_base: Path,
        language: str,
        *,
        executable: Path,
        model_path: Path,
    ) -> tuple[str, ...]:
        command = [
            str(executable),
            *self._config.executable_prefix_args,
            "--model",
            str(model_path),
            "--file",
            str(audio_path),
            "--language",
            language,
            "--output-json",
            "--output-file",
            str(output_base),
            "--no-prints",
        ]
        if self._config.threads is not None:
            command.extend(("--threads", str(self._config.threads)))
        return tuple(command)

    async def _start_process(self, command: Sequence[str]) -> asyncio.subprocess.Process:
        async with self._lock:
            if self._closed:
                raise WhisperRuntimeError("stt_worker_closed")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except OSError as exc:
                raise WhisperRuntimeError("stt_process_start_failed") from exc
            self._processes.add(process)
            return process

    async def _wait_for_process(
        self,
        process: asyncio.subprocess.Process,
        timeout_seconds: float,
        cancelled: asyncio.Event,
    ) -> None:
        wait_task = asyncio.create_task(process.wait(), name=f"whisper-wait-{process.pid}")
        cancellation = asyncio.create_task(cancelled.wait(), name=f"whisper-cancel-{process.pid}")
        try:
            try:
                async with asyncio.timeout(timeout_seconds):
                    done, _pending = await asyncio.wait(
                        (wait_task, cancellation),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
            except TimeoutError as exc:
                raise WhisperRuntimeError("stt_timeout") from exc
            if cancellation in done:
                raise WhisperRuntimeError("stt_cancelled")
            await wait_task
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
            if not wait_task.done():
                wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        async with self._lock:
            self._processes.discard(process)
        if process.returncode is not None:
            await process.wait()
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            async with asyncio.timeout(self._config.terminate_grace_seconds):
                await process.wait()
                return
        except TimeoutError:
            pass
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()

    def _validate_audio(self, audio_path: Path) -> Path:
        try:
            validated_audio = _validated_regular_file(audio_path, "stt_invalid_audio")
            stat_result = validated_audio.stat()
            if (
                not stat.S_ISREG(stat_result.st_mode)
                or stat_result.st_size > self._config.max_audio_bytes
            ):
                raise WhisperRuntimeError("stt_invalid_audio")
            with wave.open(str(validated_audio), "rb") as recording:
                valid = (
                    recording.getnchannels() == 1
                    and recording.getsampwidth() == 2
                    and recording.getframerate() == 16_000
                    and recording.getnframes() > 0
                    and recording.getcomptype() == "NONE"
                )
        except WhisperRuntimeError:
            raise
        except (OSError, EOFError, wave.Error) as exc:
            raise WhisperRuntimeError("stt_invalid_audio") from exc
        if not valid:
            raise WhisperRuntimeError("stt_invalid_audio")
        return validated_audio

    def _read_result(self, output_path: Path) -> TranscriptionResult:
        try:
            validated_output = _validated_regular_file(output_path, "stt_invalid_response")
            stat_result = validated_output.stat()
            if (
                not stat.S_ISREG(stat_result.st_mode)
                or stat_result.st_size > self._config.max_output_bytes
            ):
                raise WhisperRuntimeError("stt_output_too_large")
            payload: Any = json.loads(validated_output.read_text(encoding="utf-8"))
        except WhisperRuntimeError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WhisperRuntimeError("stt_invalid_response") from exc
        if not isinstance(payload, dict):
            raise WhisperRuntimeError("stt_invalid_response")
        raw_segments = payload.get("transcription")
        if not isinstance(raw_segments, list):
            raise WhisperRuntimeError("stt_invalid_response")
        texts: list[str] = []
        for segment in raw_segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                raise WhisperRuntimeError("stt_invalid_response")
            if normalized := segment["text"].strip():
                texts.append(normalized)
        if not texts:
            raise WhisperRuntimeError("stt_empty_transcript")
        text = " ".join(texts)
        if "\x00" in text or len(text) > MAX_TRANSCRIPT_CHARS:
            raise WhisperRuntimeError("stt_transcript_too_large")
        raw_result = payload.get("result")
        language = raw_result.get("language") if isinstance(raw_result, dict) else None
        if not isinstance(language, str) or not language or "\x00" in language:
            language = None
        elif len(language) > MAX_LANGUAGE_CHARS:
            raise WhisperRuntimeError("stt_invalid_response")
        return TranscriptionResult(text=text, language=language, segment_count=len(raw_segments))


def _validated_regular_file(path: Path, missing_code: str) -> Path:
    try:
        candidate = path.absolute()
        anchor = Path(candidate.anchor)
        assert_no_reparse_points(anchor, candidate)
        resolved = candidate.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise OSError("not a regular file")
        return resolved
    except (OSError, ReparsePointError) as exc:
        raise WhisperRuntimeError(missing_code) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sampled_fingerprint(path: Path) -> str:
    stat_result = path.stat()
    digest = hashlib.sha256()
    digest.update(b"whisper-model-fingerprint-v1\0")
    digest.update(str(stat_result.st_size).encode("ascii"))
    digest.update(b"\0")
    with path.open("rb") as source:
        digest.update(source.read(_FINGERPRINT_SAMPLE_BYTES))
        if stat_result.st_size > _FINGERPRINT_SAMPLE_BYTES:
            source.seek(max(0, stat_result.st_size - _FINGERPRINT_SAMPLE_BYTES))
            digest.update(source.read(_FINGERPRINT_SAMPLE_BYTES))
    return digest.hexdigest()


def _host_architecture() -> str:
    value = platform.machine().casefold()
    if value in {"amd64", "x86_64", "x64"}:
        return "x64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value or "unknown"


def _executable_architecture(path: Path) -> str | None:
    """Read enough common executable headers for a fail-closed mismatch check.

    An unrecognised format is accepted only after the no-shell ``--version``
    probe succeeds, which is itself evidence that the current OS can execute it.
    """

    try:
        with path.open("rb") as source:
            header = source.read(4096)
            if header.startswith(b"MZ") and len(header) >= 64:
                offset = struct.unpack_from("<I", header, 60)[0]
                if offset + 6 > len(header):
                    source.seek(offset)
                    signature_and_machine = source.read(6)
                else:
                    signature_and_machine = header[offset : offset + 6]
                if signature_and_machine[:4] == b"PE\0\0":
                    machine = struct.unpack_from("<H", signature_and_machine, 4)[0]
                    return {0x8664: "x64", 0xAA64: "arm64", 0x14C: "x86"}.get(machine)
            if header.startswith(b"\x7fELF") and len(header) >= 20:
                machine = struct.unpack_from("<H", header, 18)[0]
                return {0x3E: "x64", 0xB7: "arm64", 0x03: "x86"}.get(machine)
    except OSError:
        return None
    return None
