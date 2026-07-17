"""Windows-native private-directory and current-user DPAPI primitives.

The portable fallback exists only so source quality gates can exercise higher
level storage logic.  It is never presented as Windows DACL evidence.
"""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from ctypes import POINTER, Structure, byref, c_int, c_ubyte, c_uint32, c_void_p
from pathlib import Path
from typing import Any, Protocol

_CRYPTPROTECT_UI_FORBIDDEN = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_OWNER_SECURITY_INFORMATION = 0x00000001
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_ERROR_ALREADY_EXISTS = 183
_ERROR_INSUFFICIENT_BUFFER = 122
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class WindowsSecurityError(RuntimeError):
    """A stable, non-secret Windows security boundary failure."""


class ReparsePointError(WindowsSecurityError):
    """A managed path contains a symlink, junction, or other reparse point."""


class DataProtector(Protocol):
    """Protect bytes for one stable purpose and key identifier."""

    @property
    def algorithm(self) -> str: ...

    @property
    def scope(self) -> str: ...

    def protect(self, value: bytes, *, purpose: str, key_id: str) -> bytes: ...

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes: ...


class DirectorySecurity(Protocol):
    """Create and validate one private application directory tree."""

    def ensure_private_tree(self, root: Path, children: Iterable[Path] = ()) -> None: ...

    def audit_sddl(self, path: Path) -> str: ...


class _DataBlob(Structure):
    _fields_ = [("cbData", c_uint32), ("pbData", POINTER(c_ubyte))]


class _SecurityAttributes(Structure):
    _fields_ = [
        ("nLength", c_uint32),
        ("lpSecurityDescriptor", c_void_p),
        ("bInheritHandle", c_int),
    ]


class _SidAndAttributes(Structure):
    _fields_ = [("Sid", c_void_p), ("Attributes", c_uint32)]


class _TokenUser(Structure):
    _fields_ = [("User", _SidAndAttributes)]


def is_reparse_point(path: Path) -> bool:
    """Return whether an existing path is a symlink or Windows reparse point."""

    path_stat = path.lstat()
    attributes = int(getattr(path_stat, "st_file_attributes", 0))
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def assert_no_reparse_points(root: Path, path: Path) -> None:
    """Reject existing managed components without following them."""

    root_absolute = root.absolute()
    path_absolute = path.absolute()
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ReparsePointError("managed path escapes the private application root") from exc

    candidates = [root_absolute]
    current = root_absolute
    for part in relative.parts:
        current /= part
        candidates.append(current)
    for candidate in candidates:
        try:
            if is_reparse_point(candidate):
                raise ReparsePointError("managed path contains a reparse point")
        except FileNotFoundError:
            continue


def _require_windows() -> None:  # pragma: no cover - native Windows scenario gate
    if os.name != "nt":
        raise WindowsSecurityError("Windows security API is unavailable on this platform")


def _load_dll(name: str) -> Any:  # pragma: no cover - native Windows scenario gate
    _require_windows()
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise WindowsSecurityError("Windows DLL loader is unavailable")
    return loader(name, use_last_error=True)


def _last_error(operation: str) -> WindowsSecurityError:  # pragma: no cover
    return WindowsSecurityError(f"{operation} failed (winerror={ctypes.get_last_error()})")


def _extended_windows_path(path: Path) -> str:  # pragma: no cover
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


@contextmanager
def _converted_security_descriptor(  # pragma: no cover - native Windows scenario gate
    sddl: str,
) -> Iterator[c_void_p]:
    advapi32 = _load_dll("advapi32")
    kernel32 = _load_dll("kernel32")
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, c_uint32, POINTER(c_void_p), POINTER(c_uint32)]
    convert.restype = c_int
    descriptor = c_void_p()
    descriptor_size = c_uint32()
    if not convert(sddl, _SDDL_REVISION_1, byref(descriptor), byref(descriptor_size)):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        yield descriptor
    finally:
        kernel32.LocalFree(descriptor)


class WindowsDirectorySecurity:  # pragma: no cover - exercised by required Windows tests/VM
    """Create the app root with an explicit protected current-user DACL."""

    def __init__(self) -> None:
        _require_windows()
        self._kernel32 = _load_dll("kernel32")
        self._advapi32 = _load_dll("advapi32")
        self._user_sid = self._current_user_sid()

    @property
    def current_user_sid(self) -> str:
        return self._user_sid

    @property
    def expected_sddl(self) -> str:
        return f"D:P(A;OICI;FA;;;{self._user_sid})(A;OICI;FA;;;SY)"

    def ensure_private_tree(self, root: Path, children: Iterable[Path] = ()) -> None:
        if not root.is_absolute():
            raise WindowsSecurityError("private application root must be absolute")
        managed_children = tuple(children)
        root.parent.mkdir(parents=True, exist_ok=True)
        with _converted_security_descriptor(self.expected_sddl) as descriptor:
            self._create_or_secure_root(root, descriptor)
            # Protect the root before creating children so a permissive or
            # changed LocalAppData parent can never supply their initial ACL.
            self._apply_protected_dacl(root, descriptor)
            for child in managed_children:
                self._create_inherited_child(root, child)
                # Existing category directories may predate this policy.  Give
                # each one the same explicit protected DACL; its inheritable
                # ACEs then cover newly created files and nested directories.
                self._apply_protected_dacl(child, descriptor)
        assert_no_reparse_points(root, root)
        for child in managed_children:
            assert_no_reparse_points(root, child)

    def _create_or_secure_root(self, root: Path, descriptor: c_void_p) -> None:
        try:
            root_stat = root.lstat()
        except FileNotFoundError:
            root_stat = None
        if root_stat is not None:
            if is_reparse_point(root) or not stat.S_ISDIR(root_stat.st_mode):
                raise ReparsePointError("private application root is not a normal directory")
            return

        create_directory = self._kernel32.CreateDirectoryW
        create_directory.argtypes = [ctypes.c_wchar_p, POINTER(_SecurityAttributes)]
        create_directory.restype = c_int
        attributes = _SecurityAttributes(
            nLength=ctypes.sizeof(_SecurityAttributes),
            lpSecurityDescriptor=descriptor,
            bInheritHandle=0,
        )
        if create_directory(_extended_windows_path(root), byref(attributes)):
            return
        if ctypes.get_last_error() != _ERROR_ALREADY_EXISTS:
            raise _last_error("CreateDirectoryW")
        if is_reparse_point(root) or not root.is_dir():
            raise ReparsePointError("private application root changed during creation")

    def _create_inherited_child(self, root: Path, child: Path) -> None:
        root_absolute = root.absolute()
        child_absolute = child.absolute()
        try:
            relative = child_absolute.relative_to(root_absolute)
        except ValueError as exc:
            raise WindowsSecurityError("private child escapes the application root") from exc
        current = root_absolute
        for part in relative.parts:
            current /= part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                continue
            if is_reparse_point(current) or not stat.S_ISDIR(current_stat.st_mode):
                raise ReparsePointError("private child contains a reparse point")

    def _apply_protected_dacl(self, root: Path, descriptor: c_void_p) -> None:
        get_dacl = self._advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [c_void_p, POINTER(c_int), POINTER(c_void_p), POINTER(c_int)]
        get_dacl.restype = c_int
        present = c_int()
        defaulted = c_int()
        dacl = c_void_p()
        if not get_dacl(descriptor, byref(present), byref(dacl), byref(defaulted)):
            raise _last_error("GetSecurityDescriptorDacl")
        if not present.value or not dacl.value:
            raise WindowsSecurityError("private DACL is unexpectedly absent")

        set_security = self._advapi32.SetNamedSecurityInfoW
        set_security.argtypes = [
            ctypes.c_wchar_p,
            c_uint32,
            c_uint32,
            c_void_p,
            c_void_p,
            c_void_p,
            c_void_p,
        ]
        set_security.restype = c_uint32
        result = set_security(
            _extended_windows_path(root),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise WindowsSecurityError(f"SetNamedSecurityInfoW failed (winerror={result})")

    def audit_sddl(self, path: Path) -> str:
        """Return owner and protected DACL text for human/effective-access audit."""

        descriptor = c_void_p()
        get_security = self._advapi32.GetNamedSecurityInfoW
        get_security.argtypes = [
            ctypes.c_wchar_p,
            c_uint32,
            c_uint32,
            POINTER(c_void_p),
            c_void_p,
            c_void_p,
            c_void_p,
            POINTER(c_void_p),
        ]
        get_security.restype = c_uint32
        owner = c_void_p()
        result = get_security(
            _extended_windows_path(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            byref(owner),
            None,
            None,
            None,
            byref(descriptor),
        )
        if result != 0:
            raise WindowsSecurityError(f"GetNamedSecurityInfoW failed (winerror={result})")
        try:
            convert = self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
            convert.argtypes = [
                c_void_p,
                c_uint32,
                c_uint32,
                POINTER(c_void_p),
                POINTER(c_uint32),
            ]
            convert.restype = c_int
            text_pointer = c_void_p()
            text_length = c_uint32()
            if not convert(
                descriptor,
                _SDDL_REVISION_1,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                byref(text_pointer),
                byref(text_length),
            ):
                raise _last_error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
            try:
                if text_pointer.value is None:
                    raise WindowsSecurityError("SDDL conversion returned an empty pointer")
                return ctypes.wstring_at(text_pointer.value)
            finally:
                self._kernel32.LocalFree(text_pointer)
        finally:
            self._kernel32.LocalFree(descriptor)

    def _current_user_sid(self) -> str:
        get_process = self._kernel32.GetCurrentProcess
        get_process.argtypes = []
        get_process.restype = c_void_p
        open_token = self._advapi32.OpenProcessToken
        open_token.argtypes = [c_void_p, c_uint32, POINTER(c_void_p)]
        open_token.restype = c_int
        token = c_void_p()
        if not open_token(get_process(), _TOKEN_QUERY, byref(token)):
            raise _last_error("OpenProcessToken")
        try:
            get_information = self._advapi32.GetTokenInformation
            get_information.argtypes = [
                c_void_p,
                c_uint32,
                c_void_p,
                c_uint32,
                POINTER(c_uint32),
            ]
            get_information.restype = c_int
            size = c_uint32()
            get_information(token, _TOKEN_USER_CLASS, None, 0, byref(size))
            if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or size.value == 0:
                raise _last_error("GetTokenInformation(size)")
            buffer = ctypes.create_string_buffer(size.value)
            if not get_information(
                token,
                _TOKEN_USER_CLASS,
                ctypes.cast(buffer, c_void_p),
                size,
                byref(size),
            ):
                raise _last_error("GetTokenInformation")
            token_user = ctypes.cast(buffer, POINTER(_TokenUser)).contents
            convert_sid = self._advapi32.ConvertSidToStringSidW
            convert_sid.argtypes = [c_void_p, POINTER(c_void_p)]
            convert_sid.restype = c_int
            sid_text = c_void_p()
            if not convert_sid(token_user.User.Sid, byref(sid_text)):
                raise _last_error("ConvertSidToStringSidW")
            try:
                if sid_text.value is None:
                    raise WindowsSecurityError("SID conversion returned an empty pointer")
                return ctypes.wstring_at(sid_text.value)
            finally:
                self._kernel32.LocalFree(sid_text)
        finally:
            self._kernel32.CloseHandle(token)


class PortableDirectorySecurity:
    """Non-Windows test fallback; never valid as Windows DACL evidence."""

    def ensure_private_tree(self, root: Path, children: Iterable[Path] = ()) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_reparse_point(root):
            raise ReparsePointError("private application root is a reparse point")
        with suppress(OSError):
            os.chmod(root, 0o700)
        for child in children:
            try:
                child.absolute().relative_to(root.absolute())
            except ValueError as exc:
                raise WindowsSecurityError("private child escapes the application root") from exc
            child.mkdir(parents=True, exist_ok=True, mode=0o700)
            assert_no_reparse_points(root, child)
            with suppress(OSError):
                os.chmod(child, 0o700)

    def audit_sddl(self, path: Path) -> str:
        del path
        raise WindowsSecurityError("SDDL evidence is available only on Windows")


def directory_security_for_current_platform() -> DirectorySecurity:  # pragma: no cover
    if os.name == "nt":
        return WindowsDirectorySecurity()
    return PortableDirectorySecurity()


class WindowsDataProtector:  # pragma: no cover - exercised by required Windows tests/VM
    """Protect data with DPAPI current-user scope and purpose-bound entropy."""

    @property
    def algorithm(self) -> str:
        return "windows-dpapi"

    @property
    def scope(self) -> str:
        return "current_user"

    def protect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        return self._transform(value, purpose=purpose, key_id=key_id, protect=True)

    def unprotect(self, value: bytes, *, purpose: str, key_id: str) -> bytes:
        return self._transform(value, purpose=purpose, key_id=key_id, protect=False)

    def _transform(self, value: bytes, *, purpose: str, key_id: str, protect: bool) -> bytes:
        _require_windows()
        if not value:
            raise WindowsSecurityError(f"empty DPAPI input for secret_id={key_id}")
        crypt32 = _load_dll("crypt32")
        kernel32 = _load_dll("kernel32")
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        function.argtypes = [
            POINTER(_DataBlob),
            ctypes.c_wchar_p if protect else c_void_p,
            POINTER(_DataBlob),
            c_void_p,
            c_void_p,
            c_uint32,
            POINTER(_DataBlob),
        ]
        function.restype = c_int

        entropy = _purpose_entropy(purpose, key_id)
        input_blob, input_buffer = _blob(value)
        entropy_blob, entropy_buffer = _blob(entropy)
        output_blob = _DataBlob()
        description = f"MeguminCompanion:{key_id}:{purpose}:v1"
        description_pointer = c_void_p()
        try:
            description_argument: Any = description if protect else byref(description_pointer)
            succeeded = function(
                byref(input_blob),
                description_argument,
                byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                byref(output_blob),
            )
            if not succeeded:
                operation = "CryptProtectData" if protect else "CryptUnprotectData"
                raise WindowsSecurityError(
                    f"{operation} failed for secret_id={key_id} "
                    f"(winerror={ctypes.get_last_error()})"
                )
            if not output_blob.pbData or output_blob.cbData == 0:
                raise WindowsSecurityError(f"DPAPI returned empty output for secret_id={key_id}")
            result = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            if not protect:
                actual_description = (
                    ctypes.wstring_at(description_pointer.value)
                    if description_pointer.value is not None
                    else ""
                )
                if actual_description != description:
                    raise WindowsSecurityError(f"DPAPI description mismatch for secret_id={key_id}")
            return result
        finally:
            ctypes.memset(input_buffer, 0, len(value))
            ctypes.memset(entropy_buffer, 0, len(entropy))
            if output_blob.pbData:
                if not protect:
                    ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
                kernel32.LocalFree(ctypes.cast(output_blob.pbData, c_void_p))
            if description_pointer.value:
                kernel32.LocalFree(description_pointer)


def _purpose_entropy(purpose: str, key_id: str) -> bytes:  # pragma: no cover
    import hashlib

    return hashlib.sha256(
        b"MeguminCompanion/DPAPI/v1\0" + key_id.encode("utf-8") + b"\0" + purpose.encode("utf-8")
    ).digest()


def _blob(value: bytes) -> tuple[_DataBlob, Any]:  # pragma: no cover
    buffer = (c_ubyte * len(value)).from_buffer_copy(value)
    return _DataBlob(len(value), ctypes.cast(buffer, POINTER(c_ubyte))), buffer
