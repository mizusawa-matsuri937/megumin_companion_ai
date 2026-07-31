from __future__ import annotations

import hashlib
import stat
import struct
import unicodedata
import zipfile
from pathlib import Path

import pytest
from app.tts_gateway.package_import import (
    VoicePackageError,
    VoicePackageErrorCode,
    recover_prompt_text,
    safe_import_voice_package,
)


def _valid_zip(path: Path, *, wav_name: str = "参照音声.wav") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("voice.ckpt", b"gpt-weight")
        archive.writestr("voice.pth", b"sovits-weight")
        archive.writestr(wav_name, b"RIFF-synthetic-wave")
        archive.writestr("__MACOSX/._voice.pth", b"ignored")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_safe_import_recovers_nfc_prompt_and_preserves_archive(tmp_path: Path) -> None:
    archive = tmp_path / "voice.zip"
    prompt = unicodedata.normalize("NFD", "これは参照音声です")
    _valid_zip(archive, wav_name=f"{prompt}.wav_0000000000_0000123456.wav")
    before = _sha256(archive)

    imported = safe_import_voice_package(archive, tmp_path / "slot")

    assert imported.prompt_text == "これは参照音声です"
    assert imported.archive_sha256 == before == _sha256(archive)
    assert imported.gpt_weight.read_bytes() == b"gpt-weight"
    assert imported.sovits_weight.read_bytes() == b"sovits-weight"
    assert imported.reference_audio.read_bytes() == b"RIFF-synthetic-wave"
    assert {path.name for path in (tmp_path / "slot").iterdir()} == {
        "gpt.ckpt",
        "sovits.pth",
        "reference.wav",
    }


def test_prompt_export_suffixes_are_removed_without_rewriting_japanese() -> None:
    assert recover_prompt_text("爆裂魔法です.wav_0000000000_0000123456.wav") == "爆裂魔法です"
    assert recover_prompt_text("優しく話します_音量提高版2.wav") == "優しく話します"


@pytest.mark.parametrize(
    ("entry_name", "expected"),
    [
        ("../escape.ckpt", VoicePackageErrorCode.unsafe_entry),
        ("extra.txt", VoicePackageErrorCode.unexpected_file),
    ],
)
def test_unsafe_or_unexpected_entries_are_rejected(
    tmp_path: Path,
    entry_name: str,
    expected: VoicePackageErrorCode,
) -> None:
    archive = tmp_path / "voice.zip"
    _valid_zip(archive)
    with zipfile.ZipFile(archive, "a") as target:
        target.writestr(entry_name, b"bad")

    with pytest.raises(VoicePackageError) as caught:
        safe_import_voice_package(archive, tmp_path / "slot")

    assert caught.value.code is expected
    assert not (tmp_path / "slot").exists()


def test_link_and_casefold_duplicate_entries_are_rejected(tmp_path: Path) -> None:
    link_archive = tmp_path / "link.zip"
    with zipfile.ZipFile(link_archive, "w") as target:
        target.writestr("voice.ckpt", b"gpt")
        target.writestr("voice.pth", b"sovits")
        link = zipfile.ZipInfo("reference.wav")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(link, b"target")
    with pytest.raises(VoicePackageError) as caught:
        safe_import_voice_package(link_archive, tmp_path / "link-slot")
    assert caught.value.code is VoicePackageErrorCode.unsafe_entry

    duplicate_archive = tmp_path / "duplicate.zip"
    _valid_zip(duplicate_archive)
    with zipfile.ZipFile(duplicate_archive, "a") as target:
        target.writestr("VOICE.PTH", b"duplicate")
    with pytest.raises(VoicePackageError) as duplicate:
        safe_import_voice_package(duplicate_archive, tmp_path / "duplicate-slot")
    assert duplicate.value.code is VoicePackageErrorCode.duplicate


def test_unflagged_utf8_filename_is_losslessly_recovered(tmp_path: Path) -> None:
    archive = tmp_path / "legacy.zip"
    placeholder = "x" * len("日本語です.wav".encode())
    _valid_zip(archive, wav_name=placeholder)
    raw = bytearray(archive.read_bytes())
    encoded = "日本語です.wav".encode()
    placeholder_bytes = placeholder.encode("ascii")
    assert raw.count(placeholder_bytes) == 2
    start = 0
    while True:
        index = raw.find(placeholder_bytes, start)
        if index < 0:
            break
        raw[index : index + len(encoded)] = encoded
        if raw[index - 30 : index - 26] == b"PK\x03\x04":
            flag_offset = index - 24
        elif raw[index - 46 : index - 42] == b"PK\x01\x02":
            flag_offset = index - 38
        else:
            raise AssertionError("unexpected ZIP header")
        flags = struct.unpack_from("<H", raw, flag_offset)[0] & ~0x800
        struct.pack_into("<H", raw, flag_offset, flags)
        start = index + len(encoded)
    archive.write_bytes(raw)

    imported = safe_import_voice_package(archive, tmp_path / "slot")

    assert imported.prompt_text == "日本語です"


def test_macos_traversal_is_not_hidden_by_ignore_rule(tmp_path: Path) -> None:
    archive = tmp_path / "voice.zip"
    _valid_zip(archive)
    with zipfile.ZipFile(archive, "a") as target:
        target.writestr("__MACOSX/../../escape.ckpt", b"bad")

    with pytest.raises(VoicePackageError) as caught:
        safe_import_voice_package(archive, tmp_path / "slot")

    assert caught.value.code is VoicePackageErrorCode.unsafe_entry
