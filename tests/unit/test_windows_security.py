"""Windows-only evidence for current-user DPAPI and explicit private DACLs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.secret_store import SecretStoreError, llm_api_key_file
from app.windows_security import (
    WindowsDataProtector,
    WindowsDirectorySecurity,
    WindowsSecurityError,
    is_reparse_point,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows security APIs")


def test_real_dpapi_round_trip_and_wrong_purpose(tmp_path: Path) -> None:
    protector = WindowsDataProtector()
    plaintext = b"windows-dpapi-test-sentinel"

    ciphertext = protector.protect(
        plaintext,
        purpose="test.round-trip",
        key_id="test-key",
    )

    assert plaintext not in ciphertext
    assert (
        protector.unprotect(
            ciphertext,
            purpose="test.round-trip",
            key_id="test-key",
        )
        == plaintext
    )
    with pytest.raises(WindowsSecurityError):
        protector.unprotect(
            ciphertext,
            purpose="test.wrong-purpose",
            key_id="test-key",
        )


def test_real_dpapi_secret_file_detects_tamper(tmp_path: Path) -> None:
    from app.paths import AppPaths

    paths = AppPaths(root=tmp_path / "Private App")
    secret_file = llm_api_key_file(paths)
    secret_file.write_text("real-dpapi-tamper-sentinel")
    path = paths.secrets / "llm-api-key.json"
    serialized = bytearray(path.read_bytes())
    index = serialized.index(b"ciphertext") + len(b"ciphertext") + 4
    serialized[index] = ord("A") if serialized[index] != ord("A") else ord("B")
    path.write_bytes(serialized)

    with pytest.raises(SecretStoreError):
        secret_file.read_text()

    assert path.exists()


def test_private_root_has_protected_current_user_and_system_dacl(tmp_path: Path) -> None:
    security = WindowsDirectorySecurity()
    root = tmp_path / "Unicode 私有目录"
    children = (root / "secrets", root / "temp" / "audio")

    security.ensure_private_tree(root, children)

    assert all(child.is_dir() for child in children)
    assert not is_reparse_point(root)
    sddl = security.audit_sddl(root)
    assert "D:P" in sddl
    assert security.current_user_sid in sddl
    assert "SY" in sddl or "S-1-5-18" in sddl
    for child in children:
        child_sddl = security.audit_sddl(child)
        assert "D:P" in child_sddl
        assert security.current_user_sid in child_sddl
        assert "WD" not in child_sddl
