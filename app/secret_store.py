"""Versioned, purpose-bound encrypted secret files for the Windows runtime."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.paths import AppPaths
from app.windows_security import (
    DataProtector,
    DirectorySecurity,
    WindowsDataProtector,
    WindowsSecurityError,
    assert_no_reparse_points,
    directory_security_for_current_platform,
)

SECRET_FORMAT_VERSION = 1
LLM_API_KEY_ID = "llm-api-key"
LLM_API_KEY_PURPOSE = "llm.api-key"
DEEPSEEK_API_KEY_ID = "deepseek-api-key"
DEEPSEEK_API_KEY_PURPOSE = "deepseek.api-key"
VTS_TOKEN_ID = "vts-token"
VTS_TOKEN_PURPOSE = "vts.authentication-token"

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_SECRET_BYTES = 64 * 1024
_MAX_ENVELOPE_BYTES = 512 * 1024
_OUTER_KEYS = {
    "algorithm",
    "ciphertext",
    "format_version",
    "key_id",
    "purpose",
    "scope",
}
_INNER_KEYS = {"format_version", "sha256", "value"}


class SecretStoreErrorCode(StrEnum):
    invalid_contract = "secret_invalid_contract"
    unavailable = "secret_protection_unavailable"
    corrupt = "secret_corrupt"
    decrypt_failed = "secret_decrypt_failed"
    purpose_mismatch = "secret_purpose_mismatch"
    io_failed = "secret_io_failed"


class SecretStoreError(RuntimeError):
    """Secret failure whose public text contains only a stable identifier."""

    def __init__(self, code: SecretStoreErrorCode, key_id: str) -> None:
        self.code = code
        self.key_id = key_id
        super().__init__(f"{code.value}: secret_id={key_id}")


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    key_id: str
    purpose: str
    format_version: int
    algorithm: str
    scope: str


class EncryptedSecretFile:
    """Store one text secret in an atomic DPAPI envelope.

    A protector can be injected for portable unit tests.  Production callers
    use :class:`WindowsDataProtector`, whose scope is always ``current_user``.
    """

    def __init__(
        self,
        path: Path,
        *,
        app_root: Path,
        key_id: str,
        purpose: str,
        protector: DataProtector | None = None,
        directory_security: DirectorySecurity | None = None,
    ) -> None:
        if not _IDENTIFIER.fullmatch(key_id) or not _IDENTIFIER.fullmatch(purpose):
            raise SecretStoreError(SecretStoreErrorCode.invalid_contract, key_id)
        path_absolute = path.absolute()
        app_root_absolute = app_root.absolute()
        try:
            relative = path_absolute.relative_to(app_root_absolute / "secrets")
        except ValueError as exc:
            raise SecretStoreError(SecretStoreErrorCode.invalid_contract, key_id) from exc
        if not relative.parts:
            raise SecretStoreError(SecretStoreErrorCode.invalid_contract, key_id)
        self._path = path_absolute
        self._app_root = app_root_absolute
        self._key_id = key_id
        self._purpose = purpose
        self._protector = protector or WindowsDataProtector()
        self._directory_security = directory_security or directory_security_for_current_platform()

    @property
    def metadata(self) -> SecretMetadata:
        return SecretMetadata(
            key_id=self._key_id,
            purpose=self._purpose,
            format_version=SECRET_FORMAT_VERSION,
            algorithm=self._protector.algorithm,
            scope=self._protector.scope,
        )

    @property
    def exists(self) -> bool:
        try:
            path_stat = self._path.lstat()
            assert_no_reparse_points(self._app_root, self._path)
            return stat.S_ISREG(path_stat.st_mode)
        except FileNotFoundError:
            try:
                assert_no_reparse_points(self._app_root, self._path.parent)
                return False
            except WindowsSecurityError as exc:
                raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        except (OSError, WindowsSecurityError) as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc

    def write_text(self, value: str) -> SecretMetadata:
        encoded = value.encode("utf-8")
        if not value.strip() or len(encoded) > _MAX_SECRET_BYTES:
            raise SecretStoreError(SecretStoreErrorCode.invalid_contract, self._key_id)
        inner = json.dumps(
            {
                "format_version": SECRET_FORMAT_VERSION,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "value": base64.b64encode(encoded).decode("ascii"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        try:
            ciphertext = self._protector.protect(
                inner,
                purpose=self._purpose,
                key_id=self._key_id,
            )
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.unavailable, self._key_id) from exc
        envelope = json.dumps(
            {
                "algorithm": self._protector.algorithm,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "format_version": SECRET_FORMAT_VERSION,
                "key_id": self._key_id,
                "purpose": self._purpose,
                "scope": self._protector.scope,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._prepare_parent()
        self._validate_existing_target()
        previous_envelope = self._read_existing_envelope_bytes()
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.part")
        replaced = False
        try:
            with temporary.open("x", encoding="ascii", newline="\n") as output:
                output.write(envelope)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            replaced = True
            if self.read_text() != value:
                raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id)
        except SecretStoreError:
            if replaced:
                self._restore_previous_envelope(previous_envelope)
            raise
        except OSError as exc:
            if replaced:
                try:
                    self._restore_previous_envelope(previous_envelope)
                except SecretStoreError as rollback_exc:
                    raise rollback_exc from exc
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        return self.metadata

    def read_text(self) -> str | None:
        try:
            try:
                path_stat = self._path.lstat()
            except FileNotFoundError:
                try:
                    assert_no_reparse_points(self._app_root, self._path.parent)
                    return None
                except WindowsSecurityError as exc:
                    raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
            assert_no_reparse_points(self._app_root, self._path)
            if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > _MAX_ENVELOPE_BYTES:
                raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id)
            raw = self._path.read_text(encoding="ascii")
            envelope = json.loads(raw)
        except SecretStoreError:
            raise
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id) from exc
        if not isinstance(envelope, dict) or set(envelope) != _OUTER_KEYS:
            raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id)
        if envelope.get("key_id") != self._key_id or envelope.get("purpose") != self._purpose:
            raise SecretStoreError(SecretStoreErrorCode.purpose_mismatch, self._key_id)
        if (
            envelope.get("format_version") != SECRET_FORMAT_VERSION
            or envelope.get("algorithm") != self._protector.algorithm
            or envelope.get("scope") != self._protector.scope
            or not isinstance(envelope.get("ciphertext"), str)
        ):
            raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id)
        try:
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            plaintext = self._protector.unprotect(
                ciphertext,
                purpose=self._purpose,
                key_id=self._key_id,
            )
        except (binascii.Error, ValueError, TypeError) as exc:
            raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id) from exc
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.decrypt_failed, self._key_id) from exc
        return self._decode_inner(plaintext)

    def revoke(self) -> bool:
        try:
            try:
                path_stat = self._path.lstat()
            except FileNotFoundError:
                try:
                    assert_no_reparse_points(self._app_root, self._path.parent)
                    return False
                except WindowsSecurityError as exc:
                    raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
            assert_no_reparse_points(self._app_root, self._path)
            if not stat.S_ISREG(path_stat.st_mode):
                raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id)
            self._path.unlink()
            return True
        except SecretStoreError:
            raise
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        except OSError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc

    def reset(self) -> bool:
        """Delete an unreadable secret so the user can explicitly re-enter it."""

        return self.revoke()

    def _prepare_parent(self) -> None:
        try:
            self._directory_security.ensure_private_tree(
                self._app_root,
                (self._path.parent,),
            )
            assert_no_reparse_points(self._app_root, self._path.parent)
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc

    def _validate_existing_target(self) -> None:
        try:
            path_stat = self._path.lstat()
        except FileNotFoundError:
            return
        try:
            assert_no_reparse_points(self._app_root, self._path)
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        if not stat.S_ISREG(path_stat.st_mode):
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id)

    def _read_existing_envelope_bytes(self) -> bytes | None:
        """Keep an encrypted pre-write snapshot for post-replace verification rollback."""

        try:
            try:
                path_stat = self._path.lstat()
            except FileNotFoundError:
                return None
            assert_no_reparse_points(self._app_root, self._path)
            if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > _MAX_ENVELOPE_BYTES:
                raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id)
            return self._path.read_bytes()
        except SecretStoreError:
            raise
        except WindowsSecurityError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        except OSError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc

    def _restore_previous_envelope(self, previous_envelope: bytes | None) -> None:
        """Undo only a verified-after-replace failure without retaining plaintext."""

        if previous_envelope is None:
            try:
                self._validate_existing_target()
                self._path.unlink(missing_ok=True)
                return
            except SecretStoreError:
                raise
            except OSError as exc:
                raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc

        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.restore")
        try:
            self._prepare_parent()
            self._validate_existing_target()
            with temporary.open("xb") as output:
                output.write(previous_envelope)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        except SecretStoreError:
            raise
        except OSError as exc:
            raise SecretStoreError(SecretStoreErrorCode.io_failed, self._key_id) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _decode_inner(self, plaintext: bytes) -> str:
        try:
            inner: Any = json.loads(plaintext.decode("ascii"))
            if not isinstance(inner, dict) or set(inner) != _INNER_KEYS:
                raise ValueError("invalid inner shape")
            if inner.get("format_version") != SECRET_FORMAT_VERSION:
                raise ValueError("invalid inner version")
            if not isinstance(inner.get("value"), str) or not isinstance(inner.get("sha256"), str):
                raise ValueError("invalid inner values")
            encoded = base64.b64decode(inner["value"], validate=True)
            if len(encoded) > _MAX_SECRET_BYTES:
                raise ValueError("secret too large")
            if hashlib.sha256(encoded).hexdigest() != inner["sha256"]:
                raise ValueError("secret checksum mismatch")
            value = encoded.decode("utf-8")
            if not value.strip():
                raise ValueError("empty secret")
            return value
        except (UnicodeError, json.JSONDecodeError, binascii.Error, ValueError, TypeError) as exc:
            raise SecretStoreError(SecretStoreErrorCode.corrupt, self._key_id) from exc


def llm_api_key_file(
    paths: AppPaths,
    *,
    protector: DataProtector | None = None,
    directory_security: DirectorySecurity | None = None,
) -> EncryptedSecretFile:
    return EncryptedSecretFile(
        paths.secrets / f"{LLM_API_KEY_ID}.json",
        app_root=paths.root,
        key_id=LLM_API_KEY_ID,
        purpose=LLM_API_KEY_PURPOSE,
        protector=protector,
        directory_security=directory_security,
    )


def deepseek_api_key_file(
    paths: AppPaths,
    *,
    protector: DataProtector | None = None,
    directory_security: DirectorySecurity | None = None,
) -> EncryptedSecretFile:
    """Return the provider-bound DeepSeek credential store.

    Keeping this separate from the generic compatible-provider credential makes
    switching providers incapable of reusing a key against a different host.
    """

    return EncryptedSecretFile(
        paths.secrets / f"{DEEPSEEK_API_KEY_ID}.json",
        app_root=paths.root,
        key_id=DEEPSEEK_API_KEY_ID,
        purpose=DEEPSEEK_API_KEY_PURPOSE,
        protector=protector,
        directory_security=directory_security,
    )


def vts_token_file(
    paths: AppPaths,
    path: Path,
    *,
    protector: DataProtector | None = None,
    directory_security: DirectorySecurity | None = None,
) -> EncryptedSecretFile:
    return EncryptedSecretFile(
        path,
        app_root=paths.root,
        key_id=VTS_TOKEN_ID,
        purpose=VTS_TOKEN_PURPOSE,
        protector=protector,
        directory_security=directory_security,
    )
