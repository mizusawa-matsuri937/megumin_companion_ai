from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.tts_gateway import manifest as manifest_module
from app.tts_gateway.manifest import (
    GATEWAY_SOURCE_COMMIT,
    GatewayManifestError,
    GatewayManifestErrorCode,
    _private_acl_sddl_matches,
    load_gateway_manifest,
    source_tree_sha256,
    tree_sha256,
)
from app.windows_security import WindowsSecurityError

_SLOTS = (
    "neutral",
    "gentle",
    "tsundere",
    "focused",
    "excited_explosion",
)

_CURRENT_USER_SID = "S-1-5-21-1000"


@pytest.mark.parametrize(
    "sddl",
    (
        (f"O:{_CURRENT_USER_SID}D:PAI(A;OICI;FA;;;{_CURRENT_USER_SID})(A;OICI;FA;;;SY)"),
        (f"O:{_CURRENT_USER_SID}D:P(A;OICI;FA;;;{_CURRENT_USER_SID})(A;OICI;FA;;;SY)"),
        (f"O:{_CURRENT_USER_SID}D:AI(A;ID;FA;;;{_CURRENT_USER_SID})(A;ID;FA;;;SY)"),
    ),
)
def test_private_acl_accepts_equivalent_protected_and_inherited_shapes(
    sddl: str,
) -> None:
    assert _private_acl_sddl_matches(sddl, _CURRENT_USER_SID)


@pytest.mark.parametrize(
    "sddl",
    (
        f"O:SYD:AI(A;ID;FA;;;{_CURRENT_USER_SID})(A;ID;FA;;;SY)",
        f"O:{_CURRENT_USER_SID}D:AI(A;;FA;;;{_CURRENT_USER_SID})(A;ID;FA;;;SY)",
        f"O:{_CURRENT_USER_SID}D:PAI(A;ID;FA;;;{_CURRENT_USER_SID})(A;ID;FA;;;SY)",
        f"O:{_CURRENT_USER_SID}D:AI(A;ID;FR;;;{_CURRENT_USER_SID})(A;ID;FA;;;SY)",
        f"O:{_CURRENT_USER_SID}D:AI(A;ID;FA;;;{_CURRENT_USER_SID})(A;ID;FA;;;BA)",
        (f"O:{_CURRENT_USER_SID}D:AI(A;ID;FA;;;{_CURRENT_USER_SID})(A;ID;FA;;;SY)(A;ID;FA;;;BA)"),
    ),
)
def test_private_acl_rejects_owner_inheritance_rights_and_trustee_drift(
    sddl: str,
) -> None:
    assert not _private_acl_sddl_matches(sddl, _CURRENT_USER_SID)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "private"
    source = root / "source"
    (source / ".git").mkdir(parents=True)
    (source / "official.py").write_text("PINNED = True\n", encoding="utf-8")
    bert = root / "common" / "bert"
    hubert = root / "common" / "hubert"
    ffmpeg_bin = root / "common" / "ffmpeg"
    g2pw = source / "GPT_SoVITS" / "text" / "G2PWModel"
    language_detection = source / "GPT_SoVITS" / "pretrained_models" / "fast_langdetect"
    open_jtalk = root / "common" / "open-jtalk"
    bert.mkdir(parents=True)
    hubert.mkdir(parents=True)
    ffmpeg_bin.mkdir(parents=True)
    g2pw.mkdir(parents=True)
    language_detection.mkdir(parents=True)
    open_jtalk.mkdir(parents=True)
    (bert / "config.json").write_text("{}", encoding="utf-8")
    (hubert / "model.bin").write_bytes(b"hubert")
    (ffmpeg_bin / "ffmpeg.exe").write_bytes(b"ffmpeg")
    (g2pw / "g2pW.onnx").write_bytes(b"g2pw")
    (language_detection / "lid.176.bin").write_bytes(b"language-model")
    (open_jtalk / "sys.dic").write_bytes(b"open-jtalk")
    sv = root / "common" / "sv.ckpt"
    sv.write_bytes(b"speaker-verification")
    slots: dict[str, Any] = {}
    for name in _SLOTS:
        directory = root / "slots" / name
        directory.mkdir(parents=True)
        gpt = directory / "gpt.ckpt"
        sovits = directory / "sovits.pth"
        reference = directory / "reference.wav"
        gpt.write_bytes(f"{name}-gpt".encode())
        sovits.write_bytes(f"{name}-sovits".encode())
        reference.write_bytes(f"{name}-wav".encode())
        slots[name] = {
            "gpt_weight": {"path": gpt.relative_to(root).as_posix(), "sha256": _digest(gpt)},
            "sovits_weight": {
                "path": sovits.relative_to(root).as_posix(),
                "sha256": _digest(sovits),
            },
            "reference_audio": {
                "path": reference.relative_to(root).as_posix(),
                "sha256": _digest(reference),
            },
            "prompt_text": "信頼済みの合成参照文",
            "prompt_lang": "ja",
            "text_lang": "zh",
            "archive_sha256": "a" * 64,
        }
    payload: dict[str, Any] = {
        "format_version": 1,
        "source_commit": GATEWAY_SOURCE_COMMIT,
        "source_root": "source",
        "source_tree_sha256": source_tree_sha256(source),
        "common": {
            "bert": {"path": "common/bert", "sha256": tree_sha256(bert)},
            "cnhubert": {"path": "common/hubert", "sha256": tree_sha256(hubert)},
            "ffmpeg_bin": {
                "path": "common/ffmpeg",
                "sha256": tree_sha256(ffmpeg_bin),
            },
            "g2pw": {
                "path": "source/GPT_SoVITS/text/G2PWModel",
                "sha256": tree_sha256(g2pw),
            },
            "language_detection": {
                "path": "source/GPT_SoVITS/pretrained_models/fast_langdetect",
                "sha256": tree_sha256(language_detection),
            },
            "open_jtalk": {
                "path": "common/open-jtalk",
                "sha256": tree_sha256(open_jtalk),
            },
            "speaker_verification": {
                "path": "common/sv.ckpt",
                "sha256": _digest(sv),
            },
        },
        "batch_size": 5,
        "slots": slots,
        "device": "cuda",
        "is_half": True,
    }
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return manifest, payload


def test_manifest_verifies_hashes_source_and_every_private_acl(tmp_path: Path) -> None:
    manifest_path, _payload = _fixture(tmp_path)
    audited: set[Path] = set()

    def audit(path: Path) -> bool:
        audited.add(path)
        return True

    manifest, root = load_gateway_manifest(
        manifest_path,
        acl_auditor=audit,
        source_auditor=lambda _path, commit: commit == GATEWAY_SOURCE_COMMIT,
    )

    assert manifest.batch_size == 5
    assert set(manifest.slots) == set(_SLOTS)
    assert root == manifest_path.parent.absolute()
    expected_files = {path.absolute() for path in root.rglob("*") if path.is_file()}
    assert expected_files <= audited


def test_manifest_rejects_one_permissive_weight_acl(tmp_path: Path) -> None:
    manifest_path, payload = _fixture(tmp_path)
    rejected = manifest_path.parent / payload["slots"]["gentle"]["gpt_weight"]["path"]

    with pytest.raises(GatewayManifestError) as caught:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda path: path != rejected,
            source_auditor=lambda _path, _commit: True,
        )

    assert caught.value.code is GatewayManifestErrorCode.acl
    assert str(rejected) not in str(caught.value)


def test_manifest_rejects_g2pw_tamper(tmp_path: Path) -> None:
    manifest_path, payload = _fixture(tmp_path)
    g2pw = manifest_path.parent / payload["common"]["g2pw"]["path"] / "g2pW.onnx"
    g2pw.write_bytes(b"tampered")

    with pytest.raises(GatewayManifestError) as caught:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )

    assert caught.value.code is GatewayManifestErrorCode.hash
    assert str(g2pw) not in str(caught.value)


def test_manifest_rejects_open_jtalk_tamper(tmp_path: Path) -> None:
    manifest_path, payload = _fixture(tmp_path)
    dictionary = manifest_path.parent / payload["common"]["open_jtalk"]["path"] / "sys.dic"
    dictionary.write_bytes(b"tampered")

    with pytest.raises(GatewayManifestError) as caught:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )

    assert caught.value.code is GatewayManifestErrorCode.hash
    assert str(dictionary) not in str(caught.value)


def test_manifest_rejects_tamper_unknown_commit_and_duplicate_keys(tmp_path: Path) -> None:
    manifest_path, payload = _fixture(tmp_path)
    weight = manifest_path.parent / payload["slots"]["neutral"]["gpt_weight"]["path"]
    weight.write_bytes(b"tampered")
    with pytest.raises(GatewayManifestError) as tampered:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )
    assert tampered.value.code is GatewayManifestErrorCode.hash

    manifest_path, payload = _fixture(tmp_path / "commit")
    payload["source_commit"] = "0" * 40
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(GatewayManifestError) as commit:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )
    assert commit.value.code is GatewayManifestErrorCode.invalid

    manifest_path, _payload = _fixture(tmp_path / "duplicate")
    manifest_path.write_text('{"format_version":1,"format_version":1}', encoding="utf-8")
    with pytest.raises(GatewayManifestError) as duplicate:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )
    assert duplicate.value.code is GatewayManifestErrorCode.invalid


@pytest.mark.parametrize(
    "mutation",
    (
        "file_sha",
        "archive_sha",
        "source_sha",
        "missing_slot",
        "traversal",
        "non_normalized",
    ),
)
def test_manifest_schema_rejects_invalid_hash_slot_and_path_shapes(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path, payload = _fixture(tmp_path)
    if mutation == "file_sha":
        payload["slots"]["neutral"]["gpt_weight"]["sha256"] = "invalid"
    elif mutation == "archive_sha":
        payload["slots"]["neutral"]["archive_sha256"] = "invalid"
    elif mutation == "source_sha":
        payload["source_tree_sha256"] = "invalid"
    elif mutation == "missing_slot":
        payload["slots"].pop("focused")
    elif mutation == "traversal":
        payload["source_root"] = "../source"
    else:
        payload["source_root"] = "source//nested/.."
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GatewayManifestError) as caught:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )

    assert caught.value.code is GatewayManifestErrorCode.invalid


def test_manifest_maps_file_size_acl_and_source_auditor_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(GatewayManifestError) as missing:
        load_gateway_manifest(tmp_path / "missing.json")
    assert missing.value.code is GatewayManifestErrorCode.invalid

    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(GatewayManifestError) as not_file:
        load_gateway_manifest(directory)
    assert not_file.value.code is GatewayManifestErrorCode.invalid

    manifest_path, _payload = _fixture(tmp_path / "oversize")
    monkeypatch.setattr(manifest_module, "_MAX_MANIFEST_BYTES", 1)
    with pytest.raises(GatewayManifestError) as oversized:
        load_gateway_manifest(manifest_path)
    assert oversized.value.code is GatewayManifestErrorCode.invalid
    monkeypatch.setattr(manifest_module, "_MAX_MANIFEST_BYTES", 256 * 1024)

    manifest_path, _payload = _fixture(tmp_path / "acl")

    def broken_acl(_path: Path) -> bool:
        raise RuntimeError("synthetic ACL failure")

    with pytest.raises(GatewayManifestError) as acl:
        load_gateway_manifest(manifest_path, acl_auditor=broken_acl)
    assert acl.value.code is GatewayManifestErrorCode.acl

    with pytest.raises(GatewayManifestError) as rejected_source:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: False,
        )
    assert rejected_source.value.code is GatewayManifestErrorCode.source

    def broken_source(_path: Path, _commit: str) -> bool:
        raise RuntimeError("synthetic source failure")

    with pytest.raises(GatewayManifestError) as source_error:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=broken_source,
        )
    assert source_error.value.code is GatewayManifestErrorCode.source


def test_manifest_rejects_duplicate_asset_and_wrong_suffix(tmp_path: Path) -> None:
    manifest_path, payload = _fixture(tmp_path / "duplicate")
    payload["slots"]["gentle"]["gpt_weight"] = dict(payload["slots"]["neutral"]["gpt_weight"])
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(GatewayManifestError) as duplicate:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )
    assert duplicate.value.code is GatewayManifestErrorCode.path

    manifest_path, payload = _fixture(tmp_path / "suffix")
    payload["slots"]["neutral"]["reference_audio"] = dict(
        payload["slots"]["neutral"]["sovits_weight"]
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(GatewayManifestError) as suffix:
        load_gateway_manifest(
            manifest_path,
            acl_auditor=lambda _path: True,
            source_auditor=lambda _path, _commit: True,
        )
    assert suffix.value.code is GatewayManifestErrorCode.path


def test_tree_hash_and_source_checkout_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(OSError):
        tree_sha256(empty)

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "asset.bin").write_bytes(b"asset")
    monkeypatch.setattr(manifest_module, "_MAX_TREE_FILES", 0)
    with pytest.raises(OSError):
        tree_sha256(tree)
    monkeypatch.setattr(manifest_module, "_MAX_TREE_FILES", 10_000)
    monkeypatch.setattr(manifest_module, "is_reparse_point", lambda _path: True)
    with pytest.raises(OSError):
        tree_sha256(tree)
    monkeypatch.undo()

    source = tmp_path / "source"
    source.mkdir()
    assert not manifest_module._audit_source_checkout(source, GATEWAY_SOURCE_COMMIT)
    (source / ".git").mkdir()

    def unavailable_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("synthetic git failure")

    monkeypatch.setattr(subprocess, "run", unavailable_git)
    assert not manifest_module._audit_source_checkout(source, GATEWAY_SOURCE_COMMIT)

    results = iter(
        (
            subprocess.CompletedProcess([], 0, stdout=f"{GATEWAY_SOURCE_COMMIT}\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
        )
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: next(results))
    assert manifest_module._audit_source_checkout(source, GATEWAY_SOURCE_COMMIT)


def test_acl_helpers_fail_closed_without_exposing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_acl(_path: Path) -> bool:
        raise RuntimeError("synthetic ACL failure")

    with pytest.raises(GatewayManifestError) as acl:
        manifest_module._require_private_acl((tmp_path,), broken_acl)
    assert acl.value.code is GatewayManifestErrorCode.acl

    class _Security:
        current_user_sid = _CURRENT_USER_SID

        def audit_sddl(self, _path: Path) -> str:
            return f"O:{_CURRENT_USER_SID}D:PAI(A;OICI;FA;;;{_CURRENT_USER_SID})(A;OICI;FA;;;SY)"

    monkeypatch.setattr(manifest_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(manifest_module, "WindowsDirectorySecurity", _Security)
    assert manifest_module._windows_private_acl(tmp_path)

    class _BrokenSecurity(_Security):
        def audit_sddl(self, _path: Path) -> str:
            raise WindowsSecurityError("synthetic security failure")

    monkeypatch.setattr(manifest_module, "WindowsDirectorySecurity", _BrokenSecurity)
    assert not manifest_module._windows_private_acl(tmp_path)
