"""Safe, deterministic import of one user-confirmed GPT-SoVITS voice ZIP."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from uuid import uuid4

_ALLOWED_SUFFIXES = frozenset({".ckpt", ".pth", ".wav"})
_MAX_FILES = 16
_MAX_FILE_BYTES = 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_EXPORTED_WAV_SUFFIX = re.compile(r"\.wav_[0-9]+_[0-9]+$")
_VOLUME_SUFFIX = re.compile(r"_音量提高版2$")


class VoicePackageErrorCode(StrEnum):
    invalid = "tts_package_invalid"
    unsafe_entry = "tts_package_unsafe_entry"
    unexpected_file = "tts_package_unexpected_file"
    duplicate = "tts_package_duplicate"
    bounds = "tts_package_bounds_exceeded"
    shape = "tts_package_shape_invalid"
    io = "tts_package_io_failed"


class VoicePackageError(RuntimeError):
    """Path-free package failure safe for diagnostics."""

    def __init__(self, code: VoicePackageErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ImportedVoicePackage:
    archive_sha256: str
    gpt_weight: Path
    gpt_sha256: str
    sovits_weight: Path
    sovits_sha256: str
    reference_audio: Path
    reference_sha256: str
    prompt_text: str


@dataclass(frozen=True, slots=True)
class _SelectedEntry:
    info: zipfile.ZipInfo
    recovered_name: str
    suffix: str


def safe_import_voice_package(
    archive: Path,
    destination: Path,
) -> ImportedVoicePackage:
    """Import exactly one ckpt/pth/wav triplet without using ``ZipFile.extract``."""

    archive = archive.absolute()
    destination = destination.absolute()
    if destination.exists():
        raise VoicePackageError(VoicePackageErrorCode.io)
    staging = destination.with_name(f".{destination.name}.{uuid4().hex}.staging")
    if staging.exists():
        raise VoicePackageError(VoicePackageErrorCode.io)
    try:
        archive_sha256 = _file_sha256(archive)
        with zipfile.ZipFile(archive, "r") as source:
            selected = _validate_entries(source.infolist())
            staging.mkdir(parents=True)
            imported = _extract_selected(
                source,
                selected,
                staging,
                archive_sha256=archive_sha256,
            )
        os.replace(staging, destination)
        return ImportedVoicePackage(
            archive_sha256=imported.archive_sha256,
            gpt_weight=destination / imported.gpt_weight.name,
            gpt_sha256=imported.gpt_sha256,
            sovits_weight=destination / imported.sovits_weight.name,
            sovits_sha256=imported.sovits_sha256,
            reference_audio=destination / imported.reference_audio.name,
            reference_sha256=imported.reference_sha256,
            prompt_text=imported.prompt_text,
        )
    except VoicePackageError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise VoicePackageError(VoicePackageErrorCode.io) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def recover_prompt_text(filename: str) -> str:
    """Recover the Japanese transcript from a normalized exported WAV filename."""

    value = unicodedata.normalize("NFC", PurePosixPath(filename).name)
    if value.casefold().endswith(".wav"):
        value = value[:-4]
    value = _EXPORTED_WAV_SUFFIX.sub("", value)
    value = _VOLUME_SUFFIX.sub("", value)
    if value.casefold().endswith(".wav"):
        value = value[:-4]
    value = value.strip()
    if not value or "\x00" in value or len(value) > 20_000:
        raise VoicePackageError(VoicePackageErrorCode.shape)
    return value


def _validate_entries(entries: list[zipfile.ZipInfo]) -> tuple[_SelectedEntry, ...]:
    selected: list[_SelectedEntry] = []
    seen: set[str] = set()
    total_bytes = 0
    for info in entries:
        recovered = _recover_zip_name(info)
        path = PurePosixPath(recovered)
        _validate_entry_path(info, path)
        if _ignored_macos_entry(path):
            continue
        key = unicodedata.normalize("NFC", path.as_posix()).casefold()
        if key in seen:
            raise VoicePackageError(VoicePackageErrorCode.duplicate)
        seen.add(key)
        if info.is_dir():
            continue
        suffix = path.suffix.casefold()
        if suffix not in _ALLOWED_SUFFIXES:
            raise VoicePackageError(VoicePackageErrorCode.unexpected_file)
        if info.file_size < 1 or info.file_size > _MAX_FILE_BYTES or info.compress_size < 0:
            raise VoicePackageError(VoicePackageErrorCode.bounds)
        total_bytes += info.file_size
        if len(selected) >= _MAX_FILES or total_bytes > _MAX_TOTAL_BYTES:
            raise VoicePackageError(VoicePackageErrorCode.bounds)
        selected.append(_SelectedEntry(info, path.as_posix(), suffix))
    counts = {
        suffix: sum(entry.suffix == suffix for entry in selected) for suffix in _ALLOWED_SUFFIXES
    }
    if counts != {".ckpt": 1, ".pth": 1, ".wav": 1}:
        raise VoicePackageError(VoicePackageErrorCode.shape)
    return tuple(selected)


def _validate_entry_path(info: zipfile.ZipInfo, path: PurePosixPath) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if (
        info.flag_bits & 0x1
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
        or "\x00" in path.as_posix()
        or len(path.as_posix()) > 512
        or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
    ):
        raise VoicePackageError(VoicePackageErrorCode.unsafe_entry)


def _recover_zip_name(info: zipfile.ZipInfo) -> str:
    value = info.filename
    if not info.flag_bits & 0x800:
        try:
            value = value.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            raise VoicePackageError(VoicePackageErrorCode.invalid) from exc
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if normalized != value.replace("\\", "/") and "\x00" in normalized:
        raise VoicePackageError(VoicePackageErrorCode.invalid)
    return normalized


def _ignored_macos_entry(path: PurePosixPath) -> bool:
    return bool(path.parts) and (path.parts[0] == "__MACOSX" or path.name.startswith("._"))


def _extract_selected(
    source: zipfile.ZipFile,
    selected: tuple[_SelectedEntry, ...],
    staging: Path,
    *,
    archive_sha256: str,
) -> ImportedVoicePackage:
    output_names = {
        ".ckpt": "gpt.ckpt",
        ".pth": "sovits.pth",
        ".wav": "reference.wav",
    }
    digests: dict[str, str] = {}
    paths: dict[str, Path] = {}
    prompt_text = ""
    for entry in selected:
        target = staging / output_names[entry.suffix]
        digest = hashlib.sha256()
        written = 0
        try:
            with source.open(entry.info, "r") as input_file, target.open("xb") as output_file:
                while chunk := input_file.read(_CHUNK_BYTES):
                    written += len(chunk)
                    if written > entry.info.file_size or written > _MAX_FILE_BYTES:
                        raise VoicePackageError(VoicePackageErrorCode.bounds)
                    digest.update(chunk)
                    output_file.write(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise VoicePackageError(VoicePackageErrorCode.io) from exc
        if written != entry.info.file_size:
            raise VoicePackageError(VoicePackageErrorCode.invalid)
        digests[entry.suffix] = digest.hexdigest()
        paths[entry.suffix] = target
        if entry.suffix == ".wav":
            prompt_text = recover_prompt_text(entry.recovered_name)
    return ImportedVoicePackage(
        archive_sha256=archive_sha256,
        gpt_weight=paths[".ckpt"],
        gpt_sha256=digests[".ckpt"],
        sovits_weight=paths[".pth"],
        sovits_sha256=digests[".pth"],
        reference_audio=paths[".wav"],
        reference_sha256=digests[".wav"],
        prompt_text=prompt_text,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
