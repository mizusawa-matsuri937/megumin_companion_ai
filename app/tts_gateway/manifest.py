"""Strict private manifest loading with hash, path, source, and ACL gates."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.tts_gateway.contracts import VoiceSlot
from app.windows_security import (
    WindowsDirectorySecurity,
    WindowsSecurityError,
    assert_no_reparse_points,
    is_reparse_point,
)

GATEWAY_SOURCE_COMMIT = "d523079fc05d9a8028d6085bffe4a2757c32abb6"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_TREE_FILES = 10_000
_MAX_TREE_BYTES = 20 * 1024 * 1024 * 1024
_SLOTS = frozenset(
    {
        "neutral",
        "gentle",
        "tsundere",
        "focused",
        "excited_explosion",
    }
)
ACLAuditor = Callable[[Path], bool]
SourceAuditor = Callable[[Path, str], bool]


class GatewayManifestErrorCode(StrEnum):
    invalid = "tts_manifest_invalid"
    path = "tts_manifest_path_invalid"
    hash = "tts_manifest_hash_failed"
    acl = "tts_manifest_acl_failed"
    source = "tts_source_untrusted"


class GatewayManifestError(RuntimeError):
    """Stable manifest failure without private paths or prompt content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FileDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=512)
    sha256: str

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _normalized_relative(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid sha256")
        return value


class TreeDigest(FileDigest):
    pass


class GatewayCommonAssets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bert: TreeDigest
    cnhubert: TreeDigest
    ffmpeg_bin: TreeDigest
    g2pw: TreeDigest
    language_detection: TreeDigest
    open_jtalk: TreeDigest
    speaker_verification: FileDigest


class GatewayVoiceSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gpt_weight: FileDigest
    sovits_weight: FileDigest
    reference_audio: FileDigest
    prompt_text: str = Field(min_length=1, max_length=20_000)
    prompt_lang: Literal["ja"]
    text_lang: Literal["zh"]
    archive_sha256: str

    @field_validator("archive_sha256")
    @classmethod
    def validate_archive_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid archive sha256")
        return value


class GatewayManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1]
    source_commit: str
    source_root: str = Field(min_length=1, max_length=512)
    source_tree_sha256: str
    common: GatewayCommonAssets
    batch_size: Literal[1, 5, 10, 20]
    slots: dict[VoiceSlot, GatewayVoiceSlot]
    device: Literal["cuda"] = "cuda"
    is_half: Literal[True] = True

    @field_validator("source_root")
    @classmethod
    def validate_source_root(cls, value: str) -> str:
        return _normalized_relative(value)

    @field_validator("source_commit")
    @classmethod
    def validate_source_commit(cls, value: str) -> str:
        if not hmac.compare_digest(value, GATEWAY_SOURCE_COMMIT):
            raise ValueError("untrusted source commit")
        return value

    @field_validator("source_tree_sha256")
    @classmethod
    def validate_source_tree_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("invalid source tree sha256")
        return value

    @field_validator("slots")
    @classmethod
    def validate_slots(
        cls,
        value: dict[VoiceSlot, GatewayVoiceSlot],
    ) -> dict[VoiceSlot, GatewayVoiceSlot]:
        if set(value) != _SLOTS:
            raise ValueError("all voice slots are required")
        return value


def load_gateway_manifest(
    path: Path,
    *,
    acl_auditor: ACLAuditor | None = None,
    source_auditor: SourceAuditor | None = None,
) -> tuple[GatewayManifest, Path]:
    """Load and fully verify one private manifest without returning raw paths in errors."""

    try:
        path = path.absolute()
        root = path.parent
        assert_no_reparse_points(root, path)
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > _MAX_MANIFEST_BYTES:
            raise GatewayManifestError(GatewayManifestErrorCode.invalid)
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
        manifest = GatewayManifest.model_validate(data)
    except GatewayManifestError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        WindowsSecurityError,
        ValueError,
    ) as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.invalid) from exc

    auditor = acl_auditor or _windows_private_acl
    try:
        _require_private_acl((root, path), auditor)
    except GatewayManifestError:
        raise
    except Exception as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.acl) from exc

    source_root = _resolve(root, manifest.source_root, directory=True)
    source_check = source_auditor or _audit_source_checkout
    try:
        if not source_check(source_root, manifest.source_commit):
            raise GatewayManifestError(GatewayManifestErrorCode.source)
        if not hmac.compare_digest(
            _source_tree_sha256(source_root),
            manifest.source_tree_sha256,
        ):
            raise GatewayManifestError(GatewayManifestErrorCode.source)
        _require_private_tree_acl(source_root, auditor)
    except GatewayManifestError:
        raise
    except Exception as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.source) from exc

    bert_root = _verify_tree(root, manifest.common.bert)
    cnhubert_root = _verify_tree(root, manifest.common.cnhubert)
    ffmpeg_bin_root = _verify_tree(root, manifest.common.ffmpeg_bin)
    g2pw_root = _verify_tree(root, manifest.common.g2pw)
    language_detection_root = _verify_tree(
        root,
        manifest.common.language_detection,
    )
    open_jtalk_root = _verify_tree(root, manifest.common.open_jtalk)
    speaker_verification = _verify_file(
        root,
        manifest.common.speaker_verification,
        suffix=".ckpt",
    )
    _require_private_tree_acl(bert_root, auditor)
    _require_private_tree_acl(cnhubert_root, auditor)
    _require_private_tree_acl(ffmpeg_bin_root, auditor)
    _require_private_tree_acl(g2pw_root, auditor)
    _require_private_tree_acl(language_detection_root, auditor)
    _require_private_tree_acl(open_jtalk_root, auditor)
    _require_private_acl((speaker_verification,), auditor)
    seen_paths: set[Path] = set()
    for slot in sorted(manifest.slots):
        selected = manifest.slots[slot]
        files = (
            (selected.gpt_weight, ".ckpt"),
            (selected.sovits_weight, ".pth"),
            (selected.reference_audio, ".wav"),
        )
        for digest, suffix in files:
            resolved = _verify_file(root, digest, suffix=suffix)
            if resolved in seen_paths:
                raise GatewayManifestError(GatewayManifestErrorCode.path)
            seen_paths.add(resolved)
            _require_private_acl((resolved,), auditor)
    return manifest, root


def tree_sha256(path: Path) -> str:
    """Public installer helper for a bounded, link-free private directory."""

    return _tree_sha256(path)


def source_tree_sha256(path: Path) -> str:
    """Public installer helper using the same official-source exclusions as startup."""

    return _source_tree_sha256(path)


def _verify_file(root: Path, digest: FileDigest, *, suffix: str) -> Path:
    resolved = _resolve(root, digest.path, directory=False)
    if resolved.suffix.casefold() != suffix:
        raise GatewayManifestError(GatewayManifestErrorCode.path)
    try:
        actual = _file_sha256(resolved)
    except OSError as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.hash) from exc
    if not hmac.compare_digest(actual, digest.sha256):
        raise GatewayManifestError(GatewayManifestErrorCode.hash)
    return resolved


def _verify_tree(root: Path, digest: TreeDigest) -> Path:
    resolved = _resolve(root, digest.path, directory=True)
    try:
        actual = _tree_sha256(resolved)
    except OSError as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.hash) from exc
    if not hmac.compare_digest(actual, digest.sha256):
        raise GatewayManifestError(GatewayManifestErrorCode.hash)
    return resolved


def _resolve(root: Path, relative: str, *, directory: bool) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts).absolute()
    try:
        candidate.relative_to(root)
        assert_no_reparse_points(root, candidate)
        selected_stat = candidate.lstat()
    except (ValueError, OSError, WindowsSecurityError) as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.path) from exc
    expected = (
        stat.S_ISDIR(selected_stat.st_mode) if directory else stat.S_ISREG(selected_stat.st_mode)
    )
    if not expected or is_reparse_point(candidate):
        raise GatewayManifestError(GatewayManifestErrorCode.path)
    return candidate


def _normalized_relative(value: str) -> str:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or ":" in candidate.parts[0]
    ):
        raise ValueError("path must be a normalized relative path")
    normalized = candidate.as_posix()
    if normalized != value.replace("\\", "/"):
        raise ValueError("path must already be normalized")
    return normalized


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(path: Path, *, exclude: frozenset[str] = frozenset()) -> str:
    digest = hashlib.sha256()
    total_files = 0
    total_bytes = 0
    for candidate in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = candidate.relative_to(path).as_posix()
        if any(relative == item or relative.startswith(f"{item}/") for item in exclude):
            continue
        candidate_stat = candidate.lstat()
        if is_reparse_point(candidate):
            raise OSError("tree contains a reparse point")
        if stat.S_ISDIR(candidate_stat.st_mode):
            continue
        if not stat.S_ISREG(candidate_stat.st_mode):
            raise OSError("tree contains a non-regular entry")
        total_files += 1
        total_bytes += candidate_stat.st_size
        if total_files > _MAX_TREE_FILES or total_bytes > _MAX_TREE_BYTES:
            raise OSError("tree exceeds manifest bounds")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_file_sha256(candidate)))
    if total_files == 0:
        raise OSError("trusted tree is empty")
    return digest.hexdigest()


def _source_tree_sha256(path: Path) -> str:
    return _tree_sha256(
        path,
        exclude=frozenset(
            {
                ".git",
                "GPT_SoVITS/configs/tts_infer.yaml",
                "GPT_SoVITS/pretrained_models",
                "GPT_SoVITS/text/G2PWModel",
                "__pycache__",
            }
        ),
    )


def _audit_source_checkout(path: Path, expected_commit: str) -> bool:
    if not (path / ".git").is_dir():
        return False
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude)GPT_SoVITS/configs/tts_infer.yaml",
                ":(exclude)GPT_SoVITS/pretrained_models",
                ":(exclude)GPT_SoVITS/text/G2PWModel",
                ":(exclude)GPT_SoVITS/text/ja_userdic/user.dict",
                ":(exclude)GPT_SoVITS/text/ja_userdic/userdict.md5",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return hmac.compare_digest(head, expected_commit) and not status.strip()


def _windows_private_acl(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        security = WindowsDirectorySecurity()
        sddl = security.audit_sddl(path)
    except WindowsSecurityError:
        return False
    return _private_acl_sddl_matches(sddl, security.current_user_sid)


def _private_acl_sddl_matches(sddl: str, current_user_sid: str) -> bool:
    owner_prefix = f"O:{current_user_sid}"
    dacl_match = re.search(r"D:([^()]*)", sddl)
    if not sddl.startswith(owner_prefix) or dacl_match is None:
        return False
    dacl_flags = dacl_match.group(1)
    if dacl_flags in {"P", "PAI"}:
        expected_ace_flags = "OICI"
    elif dacl_flags == "AI":
        expected_ace_flags = "ID"
    else:
        return False
    aces = re.findall(r"\(([^()]*)\)", sddl[dacl_match.end() :])
    trustees: set[str] = set()
    for ace in aces:
        fields = ace.split(";")
        if (
            len(fields) != 6
            or fields[0] != "A"
            or fields[1] != expected_ace_flags
            or fields[2] != "FA"
            or fields[3]
            or fields[4]
        ):
            return False
        trustee = fields[5]
        if trustee not in {current_user_sid, "SY"}:
            return False
        trustees.add(trustee)
    return trustees == {current_user_sid, "SY"} and len(aces) == 2


def _require_private_acl(paths: tuple[Path, ...], auditor: ACLAuditor) -> None:
    try:
        if not all(auditor(path) for path in paths):
            raise GatewayManifestError(GatewayManifestErrorCode.acl)
    except GatewayManifestError:
        raise
    except Exception as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.acl) from exc


def _require_private_tree_acl(root: Path, auditor: ACLAuditor) -> None:
    paths = [root]
    try:
        for candidate in root.rglob("*"):
            if is_reparse_point(candidate):
                raise GatewayManifestError(GatewayManifestErrorCode.path)
            paths.append(candidate)
            if len(paths) > _MAX_TREE_FILES + 1:
                raise GatewayManifestError(GatewayManifestErrorCode.acl)
    except GatewayManifestError:
        raise
    except OSError as exc:
        raise GatewayManifestError(GatewayManifestErrorCode.acl) from exc
    _require_private_acl(tuple(paths), auditor)


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
