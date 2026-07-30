"""Portable contract tests for the purpose-bound encrypted secret envelope."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from app.paths import AppPaths
from app.secret_store import (
    DEEPSEEK_API_KEY_ID,
    LLM_API_KEY_ID,
    EncryptedSecretFile,
    SecretStoreError,
    SecretStoreErrorCode,
    deepseek_api_key_file,
    llm_api_key_file,
)
from app.windows_security import PortableDirectorySecurity, WindowsSecurityError


class _TestProtector:
    """Deterministic authenticated transform used only for portable tests."""

    @property
    def algorithm(self) -> str:
        return "test-protector"

    @property
    def scope(self) -> str:
        return "current_user"

    def protect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        context = f"{purpose}\0{key_id}".encode()
        mask = hashlib.sha256(context).digest()
        ciphertext = bytes(byte ^ mask[index % len(mask)] for index, byte in enumerate(value))
        tag = hmac.digest(mask, ciphertext, "sha256")
        return tag + ciphertext

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        if len(value) <= 32:
            raise WindowsSecurityError("test integrity failure")
        context = f"{purpose}\0{key_id}".encode()
        mask = hashlib.sha256(context).digest()
        tag, ciphertext = value[:32], value[32:]
        if not hmac.compare_digest(tag, hmac.digest(mask, ciphertext, "sha256")):
            raise WindowsSecurityError("test integrity failure")
        return bytes(byte ^ mask[index % len(mask)] for index, byte in enumerate(ciphertext))


class _FailOnceVerifierProtector(_TestProtector):
    """Simulate a DPAPI verification failure after the new envelope replaces the old one."""

    def __init__(self) -> None:
        self.fail_next_unprotect = False

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        if self.fail_next_unprotect:
            self.fail_next_unprotect = False
            raise WindowsSecurityError("synthetic post-replace verification failure")
        return super().unprotect(value, purpose=purpose, key_id=key_id)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(root=tmp_path / "MeguminCompanion")


def _secret_file(paths: AppPaths) -> EncryptedSecretFile:
    return llm_api_key_file(
        paths,
        protector=_TestProtector(),
        directory_security=PortableDirectorySecurity(),
    )


def test_secret_round_trip_replace_revoke_and_reset(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    secret_file = _secret_file(paths)

    metadata = secret_file.write_text("first-never-log-secret")

    assert metadata.key_id == LLM_API_KEY_ID
    assert metadata.scope == "current_user"
    assert secret_file.read_text() == "first-never-log-secret"
    serialized = (paths.secrets / "llm-api-key.json").read_text(encoding="ascii")
    assert "first-never-log-secret" not in serialized
    assert list(paths.secrets.glob("*.part")) == []

    secret_file.write_text("replacement-never-log-secret")
    assert secret_file.read_text() == "replacement-never-log-secret"
    assert "first-never-log-secret" not in (paths.secrets / "llm-api-key.json").read_text(
        encoding="ascii"
    )
    assert secret_file.revoke()
    assert not secret_file.revoke()
    assert not secret_file.reset()
    assert secret_file.read_text() is None


def test_deepseek_secret_has_an_independent_purpose_bound_slot(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    generic = _secret_file(paths)
    deepseek = deepseek_api_key_file(
        paths,
        protector=_TestProtector(),
        directory_security=PortableDirectorySecurity(),
    )

    generic.write_text("generic-never-log-secret")
    metadata = deepseek.write_text("deepseek-never-log-secret")

    assert metadata.key_id == DEEPSEEK_API_KEY_ID
    assert metadata.purpose == "deepseek.api-key"
    assert deepseek.read_text() == "deepseek-never-log-secret"
    assert generic.read_text() == "generic-never-log-secret"
    assert (paths.secrets / "deepseek-api-key.json").exists()
    assert (paths.secrets / "llm-api-key.json").exists()
    assert "deepseek-never-log-secret" not in (paths.secrets / "deepseek-api-key.json").read_text(
        encoding="ascii"
    )
    assert deepseek.revoke()
    assert deepseek.read_text() is None
    assert generic.read_text() == "generic-never-log-secret"


def test_post_replace_verification_failure_restores_the_previous_encrypted_envelope(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    protector = _FailOnceVerifierProtector()
    secret_file = deepseek_api_key_file(
        paths,
        protector=protector,
        directory_security=PortableDirectorySecurity(),
    )
    previous_value = "previous-deepseek-key-must-survive"
    rejected_value = "replacement-deepseek-key-must-not-survive"
    secret_file.write_text(previous_value)
    path = paths.secrets / "deepseek-api-key.json"
    previous_envelope = path.read_bytes()
    protector.fail_next_unprotect = True

    with pytest.raises(SecretStoreError) as captured:
        secret_file.write_text(rejected_value)

    assert captured.value.code is SecretStoreErrorCode.decrypt_failed
    assert path.read_bytes() == previous_envelope
    assert secret_file.read_text() == previous_value
    assert rejected_value not in str(captured.value)
    assert previous_value not in str(captured.value)
    assert list(paths.secrets.glob("*.part")) == []
    assert list(paths.secrets.glob("*.restore")) == []


def test_post_replace_verification_failure_removes_a_new_envelope_without_a_prior_key(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    protector = _FailOnceVerifierProtector()
    secret_file = deepseek_api_key_file(
        paths,
        protector=protector,
        directory_security=PortableDirectorySecurity(),
    )
    path = paths.secrets / "deepseek-api-key.json"
    protector.fail_next_unprotect = True

    with pytest.raises(SecretStoreError) as captured:
        secret_file.write_text("new-deepseek-key-must-not-survive")

    assert captured.value.code is SecretStoreErrorCode.decrypt_failed
    assert not path.exists()
    assert secret_file.read_text() is None
    assert list(paths.secrets.glob("*.part")) == []
    assert list(paths.secrets.glob("*.restore")) == []


def test_ciphertext_tamper_is_reported_without_secret_or_path(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    secret_file = _secret_file(paths)
    value = "tamper-sentinel-secret"
    secret_file.write_text(value)
    path = paths.secrets / "llm-api-key.json"
    envelope = json.loads(path.read_text(encoding="ascii"))
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"], validate=True))
    ciphertext[-1] ^= 0x01
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    path.write_text(json.dumps(envelope), encoding="ascii")

    with pytest.raises(SecretStoreError) as captured:
        secret_file.read_text()

    assert captured.value.code is SecretStoreErrorCode.decrypt_failed
    assert value not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)
    assert path.exists(), "损坏 secret 必须保留到用户显式 reset，不能静默删除"
    assert secret_file.reset()
    assert not path.exists()


def test_wrong_purpose_and_future_format_fail_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    original = _secret_file(paths)
    original.write_text("purpose-bound-secret")
    wrong_purpose = EncryptedSecretFile(
        paths.secrets / "llm-api-key.json",
        app_root=paths.root,
        key_id=LLM_API_KEY_ID,
        purpose="llm.other-purpose",
        protector=_TestProtector(),
        directory_security=PortableDirectorySecurity(),
    )

    with pytest.raises(SecretStoreError) as captured:
        wrong_purpose.read_text()
    assert captured.value.code is SecretStoreErrorCode.purpose_mismatch

    path = paths.secrets / "llm-api-key.json"
    envelope = json.loads(path.read_text(encoding="ascii"))
    envelope["format_version"] = 999
    path.write_text(json.dumps(envelope), encoding="ascii")
    with pytest.raises(SecretStoreError) as captured:
        original.read_text()
    assert captured.value.code is SecretStoreErrorCode.corrupt


def test_secret_path_must_stay_inside_private_secrets_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    with pytest.raises(SecretStoreError) as captured:
        EncryptedSecretFile(
            tmp_path / "outside.json",
            app_root=paths.root,
            key_id=LLM_API_KEY_ID,
            purpose="llm.api-key",
            protector=_TestProtector(),
            directory_security=PortableDirectorySecurity(),
        )

    assert captured.value.code is SecretStoreErrorCode.invalid_contract


@pytest.mark.parametrize(
    "value",
    ["", "   ", "x" * (64 * 1024 + 1)],
    ids=["empty", "whitespace", "too-large"],
)
def test_empty_or_unbounded_secret_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(SecretStoreError) as captured:
        _secret_file(_paths(tmp_path)).write_text(value)

    assert captured.value.code is SecretStoreErrorCode.invalid_contract
