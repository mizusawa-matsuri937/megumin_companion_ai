"""Secret paths reject directory aliases and non-file targets."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from app.paths import AppPaths
from app.secret_store import EncryptedSecretFile, SecretStoreError, SecretStoreErrorCode
from app.windows_security import PortableDirectorySecurity


class _IdentityProtector:
    algorithm = "test-only"
    scope = "current_user"

    def protect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        del purpose, key_id
        return value[::-1]

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        del purpose, key_id
        return value[::-1]


def _file(paths: AppPaths) -> EncryptedSecretFile:
    return EncryptedSecretFile(
        paths.secrets / "test-secret.json",
        app_root=paths.root,
        key_id="test-secret",
        purpose="test.secret",
        protector=_IdentityProtector(),
        directory_security=PortableDirectorySecurity(),
    )


def _create_directory_reparse(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlink unavailable: {symlink_error}")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("directory junction unavailable")


def test_secret_target_must_be_a_file_below_secrets(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "private")

    with pytest.raises(SecretStoreError) as captured:
        EncryptedSecretFile(
            paths.secrets,
            app_root=paths.root,
            key_id="test-secret",
            purpose="test.secret",
            protector=_IdentityProtector(),
            directory_security=PortableDirectorySecurity(),
        )

    assert captured.value.code is SecretStoreErrorCode.invalid_contract


def test_secret_write_rejects_reparse_parent_without_touching_outside(
    tmp_path: Path,
) -> None:
    paths = AppPaths(root=tmp_path / "private")
    paths.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    _create_directory_reparse(paths.secrets, outside)

    with pytest.raises(SecretStoreError) as captured:
        _file(paths).write_text("must-not-escape")

    assert captured.value.code is SecretStoreErrorCode.io_failed
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (outside / "test-secret.json").exists()
    paths.secrets.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse attribute evidence")
def test_windows_directory_symlink_is_not_treated_as_a_normal_secret_dir(
    tmp_path: Path,
) -> None:
    paths = AppPaths(root=tmp_path / "private")
    paths.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _create_directory_reparse(paths.secrets, outside)

    with pytest.raises(SecretStoreError) as captured:
        _ = _file(paths).exists
    assert captured.value.code is SecretStoreErrorCode.io_failed
    paths.secrets.rmdir()
