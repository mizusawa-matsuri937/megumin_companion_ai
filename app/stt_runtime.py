"""Pinned, explicit installation of the managed local Chinese STT runtime.

The application never calls this module from startup or from the voice capture
path.  A desktop management command or explicit CLI action owns installation;
normal inference remains fully offline after a verified installation.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from app.paths import AppPaths
from app.windows_security import (
    DirectorySecurity,
    ReparsePointError,
    WindowsSecurityError,
    assert_no_reparse_points,
    directory_security_for_current_platform,
    is_reparse_point,
)

if TYPE_CHECKING:
    from app.config.settings import Settings


_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_DOWNLOAD_TOTAL_TIMEOUT_SECONDS = 10 * 60
_DOWNLOAD_MAX_REDIRECTS = 5
_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_MAX_RUNTIME_EXTRACTED_BYTES = 64 * 1024 * 1024
_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

MANAGED_STT_PROFILE = "whispercpp_base_q5_1"
MANAGED_STT_EXECUTABLE_RELATIVE = Path("stt/whispercpp/v1.9.1/bin/whisper-cli.exe")
MANAGED_STT_MODEL_RELATIVE = Path("stt/whispercpp/v1.9.1/model/ggml-base-q5_1.bin")


@dataclass(frozen=True, slots=True)
class SttAsset:
    """One immutable public asset used by the managed profile."""

    url: str
    sha256: str
    maximum_bytes: int

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("managed STT asset must use HTTPS")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("managed STT asset hash must be lowercase SHA-256")
        if self.maximum_bytes < 1:
            raise ValueError("managed STT asset maximum must be positive")


@dataclass(frozen=True, slots=True)
class ManagedSttRuntimeManifest:
    """Version-pinned source and integrity metadata for the supported profile."""

    profile: str
    version: str
    runtime_archive: SttAsset
    executable_sha256: str
    model: SttAsset


MANAGED_CHINESE_STT_MANIFEST = ManagedSttRuntimeManifest(
    profile=MANAGED_STT_PROFILE,
    version="1.9.1",
    runtime_archive=SttAsset(
        url="https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.1/whisper-bin-x64.zip",
        sha256="7d8be46ecd31828e1eb7a2ecdd0d6b314feafd82163038ab6092594b0a063539",
        maximum_bytes=16 * 1024 * 1024,
    ),
    executable_sha256="58245314fb73b30fbd0cf0542c5c172e23f02b6eb7cad7b51e792439cf5e1755",
    model=SttAsset(
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
            "87cd18b47b941d2f65d09981dad23bb7d0481c77/ggml-base-q5_1.bin"
        ),
        sha256="422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
        maximum_bytes=80 * 1024 * 1024,
    ),
)


class SttRuntimeState(StrEnum):
    missing = "missing"
    installing = "installing"
    verified = "verified"
    integrity_failed = "integrity_failed"
    unmanaged = "unmanaged"
    unsupported = "unsupported"


@dataclass(frozen=True, slots=True)
class SttRuntimeStatus:
    """Bounded runtime state safe for settings snapshots and CLI output."""

    state: SttRuntimeState
    profile: str = MANAGED_STT_PROFILE


@dataclass(frozen=True, slots=True)
class SttRuntimeInstallResult:
    changed: bool
    status: SttRuntimeStatus


@dataclass(frozen=True, slots=True)
class ManagedRuntimeHashes:
    executable_sha256: str
    model_sha256: str


class SttRuntimeError(RuntimeError):
    """Stable, content-free managed-runtime failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def managed_stt_settings_patch() -> dict[str, object]:
    """Return the narrow patch applied after a successful explicit installation.

    ``enabled``, device selection, and thread choice deliberately remain owned
    by the existing user configuration and are never changed here.
    """

    return {
        "stt": {
            "provider": "whisper_cpp",
            "executable": MANAGED_STT_EXECUTABLE_RELATIVE.as_posix(),
            "model_path": MANAGED_STT_MODEL_RELATIVE.as_posix(),
            "language": "zh",
        }
    }


def managed_runtime_hashes_for_paths(
    paths: AppPaths,
    executable: Path,
    model_path: Path,
) -> ManagedRuntimeHashes | None:
    """Return expected hashes only for the canonical managed runtime paths."""

    expected_executable, expected_model = _managed_runtime_paths(paths)
    if (
        executable.absolute() != expected_executable.absolute()
        or model_path.absolute() != expected_model.absolute()
    ):
        return None
    return ManagedRuntimeHashes(
        executable_sha256=MANAGED_CHINESE_STT_MANIFEST.executable_sha256,
        model_sha256=MANAGED_CHINESE_STT_MANIFEST.model.sha256,
    )


class ManagedChineseSttRuntime:
    """Install and inspect the one supported x64 Windows Chinese STT profile."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        platform_supported: Callable[[], bool] | None = None,
        version_probe: Callable[[Path], None] | None = None,
        directory_security: DirectorySecurity | None = None,
    ) -> None:
        self._paths = paths
        self._client_factory = client_factory or _new_http_client
        self._platform_supported = platform_supported or _windows_x64_supported
        self._version_probe = version_probe or _probe_version
        self._directory_security = directory_security or directory_security_for_current_platform()
        self._install_lock = asyncio.Lock()
        self._installing = False
        self._verification_cache: tuple[tuple[int, int, int, int], bool] | None = None

    @property
    def installing(self) -> bool:
        return self._installing

    def status_for_settings(self, settings: Settings) -> SttRuntimeStatus:
        """Inspect persisted settings without creating directories or networking."""

        try:
            executable = settings.stt_executable_path()
            model = settings.stt_model_path()
        except Exception:
            return SttRuntimeStatus(SttRuntimeState.integrity_failed)
        return self.status_for_paths(executable, model)

    def status_for_paths(self, executable: Path, model_path: Path) -> SttRuntimeStatus:
        if self._installing:
            return SttRuntimeStatus(SttRuntimeState.installing)
        if not self._platform_supported():
            return SttRuntimeStatus(SttRuntimeState.unsupported)
        if managed_runtime_hashes_for_paths(self._paths, executable, model_path) is None:
            return SttRuntimeStatus(SttRuntimeState.unmanaged)
        state = SttRuntimeState.verified
        if not self._verify_installed():
            state = self._missing_or_invalid_state()
        return SttRuntimeStatus(state)

    async def install(self) -> SttRuntimeInstallResult:
        """Explicitly download, verify, and atomically activate the pinned profile."""

        if not self._platform_supported():
            raise SttRuntimeError("stt_platform_unsupported")
        if self._install_lock.locked():
            raise SttRuntimeError("stt_install_busy")

        async with self._install_lock:
            self._installing = True
            staging: Path | None = None
            try:
                if self._verify_installed():
                    return SttRuntimeInstallResult(
                        changed=False,
                        status=SttRuntimeStatus(SttRuntimeState.verified),
                    )
                staging = await asyncio.to_thread(self._create_staging_directory)
                runtime_archive = staging / "whisper-bin-x64.zip"
                await self._download_asset(
                    MANAGED_CHINESE_STT_MANIFEST.runtime_archive,
                    runtime_archive,
                )
                await asyncio.to_thread(
                    self._extract_runtime_archive,
                    runtime_archive,
                    staging / "bin",
                )
                await asyncio.to_thread(self._remove_staging_file, runtime_archive)
                await asyncio.to_thread(
                    self._version_probe,
                    staging / "bin" / MANAGED_STT_EXECUTABLE_RELATIVE.name,
                )
                model_path = staging / "model" / MANAGED_STT_MODEL_RELATIVE.name
                model_path.parent.mkdir(parents=True, exist_ok=True)
                await self._download_asset(MANAGED_CHINESE_STT_MANIFEST.model, model_path)
                await asyncio.to_thread(self._write_notice, staging)
                await asyncio.to_thread(self._activate_staging_directory, staging)
                staging = None
                self._verification_cache = None
                if not self._verify_installed():
                    raise SttRuntimeError("stt_runtime_integrity_failed")
                return SttRuntimeInstallResult(
                    changed=True,
                    status=SttRuntimeStatus(SttRuntimeState.verified),
                )
            except asyncio.CancelledError:
                raise
            except SttRuntimeError:
                raise
            except (OSError, ReparsePointError, WindowsSecurityError, zipfile.BadZipFile) as exc:
                raise SttRuntimeError("stt_install_failed") from exc
            finally:
                self._installing = False
                if staging is not None:
                    await asyncio.to_thread(self._remove_managed_tree, staging)

    async def _download_asset(self, asset: SttAsset, destination: Path) -> None:
        """Stream one fixed HTTPS asset to a private staging path with a full hash check."""

        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(_DOWNLOAD_TOTAL_TIMEOUT_SECONDS):
                async with self._client_factory() as client:
                    url = httpx.URL(asset.url)
                    for redirect_count in range(_DOWNLOAD_MAX_REDIRECTS + 1):
                        request = client.build_request("GET", url)
                        response = await client.send(
                            request,
                            stream=True,
                            follow_redirects=False,
                        )
                        if response.status_code in {301, 302, 303, 307, 308}:
                            try:
                                location = response.headers.get("location")
                                if location is None or redirect_count >= _DOWNLOAD_MAX_REDIRECTS:
                                    raise SttRuntimeError("stt_download_failed")
                                url = response.url.join(location)
                                # Do not let HTTPX make a downgraded request
                                # merely so it can be rejected after the fact.
                                if url.scheme.casefold() != "https":
                                    raise SttRuntimeError("stt_download_failed")
                            finally:
                                await response.aclose()
                                response = None
                            continue
                        if response.status_code != 200 or response.url.scheme.casefold() != "https":
                            raise SttRuntimeError("stt_download_failed")
                        declared_size = response.headers.get("content-length")
                        if declared_size is not None:
                            try:
                                if int(declared_size) > asset.maximum_bytes:
                                    raise SttRuntimeError("stt_download_too_large")
                            except ValueError as exc:
                                raise SttRuntimeError("stt_download_failed") from exc
                        digest = hashlib.sha256()
                        byte_count = 0
                        with destination.open("xb") as output:
                            async for chunk in response.aiter_bytes(
                                chunk_size=_DOWNLOAD_CHUNK_BYTES
                            ):
                                if not chunk:
                                    continue
                                byte_count += len(chunk)
                                if byte_count > asset.maximum_bytes:
                                    raise SttRuntimeError("stt_download_too_large")
                                digest.update(chunk)
                                output.write(chunk)
                        if byte_count == 0 or digest.hexdigest() != asset.sha256:
                            raise SttRuntimeError("stt_runtime_integrity_failed")
                        return
                    raise SttRuntimeError("stt_download_failed")
        except SttRuntimeError:
            raise
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            raise SttRuntimeError("stt_download_failed") from exc
        finally:
            if response is not None:
                with suppress(httpx.HTTPError):
                    await response.aclose()
            if destination.exists() and not _matches_sha256(destination, asset.sha256):
                with suppress(OSError):
                    destination.unlink()

    def _create_staging_directory(self) -> Path:
        parent = _managed_runtime_root(self._paths).parent
        self._directory_security.ensure_private_tree(self._paths.root, (parent,))
        assert_no_reparse_points(self._paths.root, parent)
        staging = parent / f".install-{uuid4().hex}"
        staging.mkdir(mode=0o700)
        assert_no_reparse_points(self._paths.root, staging)
        return staging

    def _extract_runtime_archive(self, archive_path: Path, destination: Path) -> None:
        """Extract only the unique CLI directory after validating all ZIP members."""

        total_uncompressed = 0
        with zipfile.ZipFile(archive_path) as archive:
            files = tuple(info for info in archive.infolist() if not info.is_dir())
            members = tuple((_safe_zip_path(info), info) for info in files)
            for _path, info in members:
                if info.flag_bits & 0x1 or stat.S_ISLNK(info.external_attr >> 16):
                    raise SttRuntimeError("stt_runtime_archive_invalid")
                total_uncompressed += info.file_size
                if total_uncompressed > _MAX_RUNTIME_EXTRACTED_BYTES:
                    raise SttRuntimeError("stt_runtime_archive_invalid")
            candidates = tuple(
                path for path, _info in members if path.name.casefold() == "whisper-cli.exe"
            )
            if len(candidates) != 1:
                raise SttRuntimeError("stt_runtime_archive_invalid")
            binary_parent = candidates[0].parent
            selected = tuple((path, info) for path, info in members if path.parent == binary_parent)
            if not selected:
                raise SttRuntimeError("stt_runtime_archive_invalid")
            destination.mkdir(mode=0o700)
            names: set[str] = set()
            for path, info in selected:
                key = path.name.casefold()
                if key in names:
                    raise SttRuntimeError("stt_runtime_archive_invalid")
                names.add(key)
                target = destination / path.name
                with archive.open(info, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=_DOWNLOAD_CHUNK_BYTES)
        executable = destination / MANAGED_STT_EXECUTABLE_RELATIVE.name
        _validated_regular_file(executable, "stt_runtime_archive_invalid", self._paths.root)
        if not _matches_sha256(executable, MANAGED_CHINESE_STT_MANIFEST.executable_sha256):
            raise SttRuntimeError("stt_runtime_integrity_failed")

    def _write_notice(self, staging: Path) -> None:
        notice = staging / "NOTICE.txt"
        notice.write_text(
            "Managed Chinese STT runtime\n"
            f"profile: {MANAGED_CHINESE_STT_MANIFEST.profile}\n"
            f"whisper.cpp version: {MANAGED_CHINESE_STT_MANIFEST.version}\n"
            "whisper.cpp license: MIT\n"
            f"runtime source: {MANAGED_CHINESE_STT_MANIFEST.runtime_archive.url}\n"
            f"runtime sha256: {MANAGED_CHINESE_STT_MANIFEST.runtime_archive.sha256}\n"
            f"whisper-cli sha256: {MANAGED_CHINESE_STT_MANIFEST.executable_sha256}\n"
            "model: ggml-base-q5_1.bin\n"
            "model license: MIT\n"
            f"model source: {MANAGED_CHINESE_STT_MANIFEST.model.url}\n"
            f"model sha256: {MANAGED_CHINESE_STT_MANIFEST.model.sha256}\n",
            encoding="utf-8",
        )

    def _remove_staging_file(self, path: Path) -> None:
        _validated_regular_file(path, "stt_install_failed", self._paths.root)
        path.unlink()

    def _activate_staging_directory(self, staging: Path) -> None:
        target = _managed_runtime_root(self._paths)
        parent = target.parent
        assert_no_reparse_points(self._paths.root, parent)
        if _path_exists(target):
            if self._verify_installed():
                self._remove_managed_tree(staging)
                return
            _assert_tree_has_no_reparse_points(self._paths.root, target)
            backup = parent / f".corrupt-{uuid4().hex}"
            self._rename_managed_tree(target, backup)
            try:
                self._rename_managed_tree(staging, target)
            except BaseException:
                with suppress(OSError):
                    self._rename_managed_tree(backup, target)
                raise
            self._remove_managed_tree(backup)
            return
        self._rename_managed_tree(staging, target)

    def _rename_managed_tree(self, source: Path, destination: Path) -> None:
        _assert_managed_child(self._paths, source)
        _assert_managed_child(self._paths, destination)
        assert_no_reparse_points(self._paths.root, source)
        os.replace(source, destination)

    def _remove_managed_tree(self, target: Path) -> None:
        if not _path_exists(target):
            return
        _assert_managed_child(self._paths, target)
        _assert_tree_has_no_reparse_points(self._paths.root, target)
        shutil.rmtree(target)

    def _verify_installed(self) -> bool:
        executable, model = _managed_runtime_paths(self._paths)
        try:
            executable = _validated_regular_file(
                executable,
                "stt_runtime_integrity_failed",
                self._paths.root,
            )
            model = _validated_regular_file(model, "stt_runtime_integrity_failed", self._paths.root)
            signature = _runtime_signature(executable, model)
        except SttRuntimeError:
            return False
        if self._verification_cache is not None and self._verification_cache[0] == signature:
            return self._verification_cache[1]
        valid = _matches_sha256(
            executable,
            MANAGED_CHINESE_STT_MANIFEST.executable_sha256,
        ) and _matches_sha256(model, MANAGED_CHINESE_STT_MANIFEST.model.sha256)
        # The archive is checked before extraction and the installed CLI is
        # pinned independently. MediaWorker repeats its own version probe before
        # any voice capture starts.
        if valid:
            valid = executable.stat().st_size > 0
        self._verification_cache = (signature, valid)
        return valid

    def _missing_or_invalid_state(self) -> SttRuntimeState:
        executable, model = _managed_runtime_paths(self._paths)
        if not _path_exists(executable) or not _path_exists(model):
            return SttRuntimeState.missing
        return SttRuntimeState.integrity_failed


def _new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "MeguminCompanion-STT-Installer/1"},
    )


def _windows_x64_supported() -> bool:
    return os.name == "nt" and platform.machine().casefold() in {"amd64", "x86_64", "x64"}


def _managed_runtime_root(paths: AppPaths) -> Path:
    return paths.models / "stt" / "whispercpp" / f"v{MANAGED_CHINESE_STT_MANIFEST.version}"


def _managed_runtime_paths(paths: AppPaths) -> tuple[Path, Path]:
    return (
        paths.models / MANAGED_STT_EXECUTABLE_RELATIVE,
        paths.models / MANAGED_STT_MODEL_RELATIVE,
    )


def _runtime_signature(executable: Path, model: Path) -> tuple[int, int, int, int]:
    executable_stat = executable.stat()
    model_stat = model.stat()
    return (
        executable_stat.st_size,
        executable_stat.st_mtime_ns,
        model_stat.st_size,
        model_stat.st_mtime_ns,
    )


def _validated_regular_file(path: Path, code: str, root: Path) -> Path:
    try:
        absolute = path.absolute()
        assert_no_reparse_points(root, absolute)
        resolved = absolute.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise OSError("not a regular file")
        return resolved
    except (OSError, ReparsePointError) as exc:
        raise SttRuntimeError(code) from exc


def _matches_sha256(path: Path, expected: str) -> bool:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


def _safe_zip_path(info: zipfile.ZipInfo) -> PurePosixPath:
    raw = info.filename.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise SttRuntimeError("stt_runtime_archive_invalid")
    return path


def _probe_version(executable: Path) -> None:
    try:
        result = subprocess.run(
            (str(executable), "--version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SttRuntimeError("stt_version_probe_failed") from exc
    if result.returncode != 0:
        raise SttRuntimeError("stt_version_probe_failed")


def _assert_managed_child(paths: AppPaths, target: Path) -> None:
    root = paths.models.absolute()
    try:
        target.absolute().relative_to(root)
    except ValueError as exc:
        raise SttRuntimeError("stt_install_failed") from exc


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _assert_tree_has_no_reparse_points(root: Path, directory: Path) -> None:
    """Refuse to recursively remove or replace a tree containing a reparse point."""

    assert_no_reparse_points(root, directory)
    pending = [directory]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    if is_reparse_point(child):
                        raise ReparsePointError("managed runtime tree contains a reparse point")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
    except FileNotFoundError as exc:
        raise ReparsePointError("managed runtime tree changed during inspection") from exc


__all__ = [
    "MANAGED_CHINESE_STT_MANIFEST",
    "MANAGED_STT_EXECUTABLE_RELATIVE",
    "MANAGED_STT_MODEL_RELATIVE",
    "MANAGED_STT_PROFILE",
    "ManagedChineseSttRuntime",
    "ManagedRuntimeHashes",
    "SttRuntimeError",
    "SttRuntimeInstallResult",
    "SttRuntimeState",
    "SttRuntimeStatus",
    "managed_runtime_hashes_for_paths",
    "managed_stt_settings_patch",
]
