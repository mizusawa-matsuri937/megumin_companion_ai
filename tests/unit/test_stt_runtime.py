from __future__ import annotations

import asyncio
import hashlib
import io
import stat
import sys
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from app import stt_runtime as runtime_module
from app.config.settings import Settings, STTConfig
from app.paths import AppPaths
from app.stt_runtime import (
    MANAGED_STT_EXECUTABLE_RELATIVE,
    MANAGED_STT_MODEL_RELATIVE,
    ManagedChineseSttRuntime,
    SttAsset,
    SttRuntimeError,
    SttRuntimeState,
)
from app.windows_security import PortableDirectorySecurity


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _runtime_archive(*, traversal: bool = False, link: bool = False) -> tuple[bytes, bytes]:
    executable = b"synthetic-whisper-cli"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        name = "Release/../whisper-cli.exe" if traversal else "Release/whisper-cli.exe"
        output.writestr(name, executable)
        output.writestr("Release/ggml.dll", b"synthetic-dll")
        if link:
            symlink = zipfile.ZipInfo("Release/linked.dll")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(symlink, "ggml.dll")
    return archive.getvalue(), executable


def _manifest(
    archive: bytes,
    executable: bytes,
    model: bytes,
    *,
    profile: str = "whispercpp_base_q5_1",
    model_filename: str = "ggml-base-q5_1.bin",
) -> runtime_module.ManagedSttRuntimeManifest:
    return runtime_module.ManagedSttRuntimeManifest(
        profile=profile,
        version="1.9.1",
        runtime_archive=SttAsset(
            url="https://assets.example.invalid/whisper-bin-x64.zip",
            sha256=_sha256(archive),
            maximum_bytes=len(archive) + 1,
        ),
        executable_sha256=_sha256(executable),
        model=SttAsset(
            url="https://assets.example.invalid/ggml-base-q5_1.bin",
            sha256=_sha256(model),
            maximum_bytes=len(model) + 1,
        ),
        model_filename=model_filename,
        display_name=f"Synthetic {profile}",
    )


def _set_profiles(
    monkeypatch: pytest.MonkeyPatch,
    *profiles: runtime_module.ManagedSttRuntimeManifest,
) -> None:
    monkeypatch.setattr(runtime_module, "MANAGED_WHISPER_STT_PROFILES", profiles)


@pytest.mark.parametrize(
    ("url", "sha256", "maximum_bytes", "message"),
    [
        ("http://assets.example.invalid/model.bin", "a" * 64, 1, "HTTPS"),
        ("https://assets.example.invalid/model.bin", "A" * 64, 1, "lowercase SHA-256"),
        ("https://assets.example.invalid/model.bin", "a" * 64, 0, "must be positive"),
    ],
)
def test_managed_stt_asset_rejects_invalid_integrity_metadata(
    url: str,
    sha256: str,
    maximum_bytes: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        SttAsset(url=url, sha256=sha256, maximum_bytes=maximum_bytes)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("profile", "whispercpp-small", "profile id"),
        ("version", "1.9.1/unsafe", "runtime version"),
        ("executable_sha256", "A" * 64, "executable hash"),
        ("model_filename", "../ggml-small.bin", "model filename"),
        ("display_name", " \t", "display name"),
        ("language", "en", "Chinese-only"),
    ],
)
def test_managed_whisper_manifest_rejects_unsafe_profile_metadata(
    field_name: str,
    value: object,
    message: str,
) -> None:
    changes = cast(Any, {field_name: value})
    with pytest.raises(ValueError, match=message):
        replace(runtime_module.MANAGED_CHINESE_STT_MANIFEST, **changes)


def test_managed_whisper_profile_catalogue_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    manifest = _manifest(archive, executable, b"synthetic-model")

    _set_profiles(monkeypatch)
    with pytest.raises(RuntimeError, match="catalogue is invalid"):
        runtime_module.managed_stt_profiles()

    _set_profiles(monkeypatch, manifest, manifest)
    with pytest.raises(RuntimeError, match="catalogue is invalid"):
        runtime_module.managed_stt_profiles()

    _set_profiles(monkeypatch, manifest)
    assert runtime_module.managed_stt_manifest(f"  {manifest.profile.upper()}  ") is manifest
    with pytest.raises(ValueError, match="not registered"):
        runtime_module.managed_stt_manifest("whispercpp_unreviewed_large")


def _client_factory(
    archive: bytes,
    model: bytes,
) -> tuple[Callable[[], httpx.AsyncClient], list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith(".zip"):
            return httpx.Response(
                200,
                content=archive,
                headers={"content-length": str(len(archive))},
                request=request,
            )
        if request.url.path.endswith(".bin"):
            return httpx.Response(
                200,
                content=model,
                headers={"content-length": str(len(model))},
                request=request,
            )
        return httpx.Response(404, request=request)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), requested


def test_managed_runtime_installs_atomically_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-chinese-q5-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    factory, requested = _client_factory(archive, model)
    paths = AppPaths(root=tmp_path / "LocalAppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda binary: _assert_synthetic_binary(binary, executable),
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        executable_path = paths.models / MANAGED_STT_EXECUTABLE_RELATIVE
        model_path = paths.models / MANAGED_STT_MODEL_RELATIVE
        assert (
            runtime.status_for_paths(
                executable_path,
                model_path,
            ).state
            is SttRuntimeState.missing
        )

        installed = await runtime.install()
        assert installed.changed
        assert installed.status.state is SttRuntimeState.verified
        assert executable_path.read_bytes() == executable
        assert model_path.read_bytes() == model
        assert not (executable_path.parents[1] / "whisper-bin-x64.zip").exists()
        assert (
            runtime.status_for_paths(
                executable_path,
                model_path,
            ).state
            is SttRuntimeState.verified
        )

        installed_again = await runtime.install()
        assert not installed_again.changed
        assert len(requested) == 2

    asyncio.run(scenario())


def test_registered_larger_whisper_profile_has_its_own_paths_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    base_model = b"synthetic-base-model"
    larger_model = b"synthetic-larger-whisper-model"
    base = _manifest(archive, executable, base_model)
    larger = _manifest(
        archive,
        executable,
        larger_model,
        profile="whispercpp_small_q5_1",
        model_filename="ggml-small-q5_1.bin",
    )
    _set_profiles(monkeypatch, base, larger)
    factory, _requested = _client_factory(archive, larger_model)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        profile=larger.profile,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda binary: _assert_synthetic_binary(binary, executable),
        directory_security=PortableDirectorySecurity(),
    )
    expected_executable = (
        paths.models
        / "stt"
        / "whispercpp"
        / "profiles"
        / larger.profile
        / "v1.9.1"
        / "bin"
        / "whisper-cli.exe"
    )
    expected_model = expected_executable.parents[1] / "model" / larger.model_filename

    async def scenario() -> None:
        installed = await runtime.install()
        assert installed.status.profile == larger.profile
        assert expected_executable.read_bytes() == executable
        assert expected_model.read_bytes() == larger_model
        assert not (paths.models / MANAGED_STT_MODEL_RELATIVE).exists()
        assert (
            runtime.status_for_paths(expected_executable, expected_model).state
            is SttRuntimeState.verified
        )

    asyncio.run(scenario())

    assert runtime_module.managed_stt_settings_patch(larger.profile) == {
        "stt": {
            "managed_profile": larger.profile,
            "provider": "whisper_cpp",
            "executable": (
                "stt/whispercpp/profiles/whispercpp_small_q5_1/v1.9.1/bin/whisper-cli.exe"
            ),
            "model_path": (
                "stt/whispercpp/profiles/whispercpp_small_q5_1/v1.9.1/model/ggml-small-q5_1.bin"
            ),
            "language": "zh",
        }
    }
    hashes = runtime_module.managed_runtime_hashes_for_paths(
        paths,
        expected_executable,
        expected_model,
    )
    assert hashes is not None
    assert hashes.executable_sha256 == larger.executable_sha256
    assert hashes.model_sha256 == larger.model.sha256


def test_unregistered_whisper_profile_is_rejected_before_any_runtime_work(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")

    with pytest.raises(ValueError, match="not registered"):
        ManagedChineseSttRuntime(paths, profile="whispercpp_unreviewed_large")

    assert not paths.root.exists()


def test_profile_runtime_status_rejects_mismatched_paths_and_reports_lifecycle_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    base = _manifest(archive, executable, b"synthetic-base-model")
    larger = _manifest(
        archive,
        executable,
        b"synthetic-larger-model",
        profile="whispercpp_small_q5_1",
        model_filename="ggml-small-q5_1.bin",
    )
    _set_profiles(monkeypatch, base, larger)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        profile=base.profile,
        platform_supported=lambda: True,
        directory_security=PortableDirectorySecurity(),
    )
    base_executable, base_model = runtime_module.managed_stt_relative_paths(base.profile)
    larger_executable, larger_model = runtime_module.managed_stt_relative_paths(larger.profile)
    settings = Settings(
        stt=STTConfig(
            managed_profile=base.profile,
            executable=larger_executable,
            model_path=larger_model,
        )
    )
    settings._paths = paths

    mismatched = runtime.status_for_settings(settings)
    assert mismatched.state is SttRuntimeState.unmanaged
    assert mismatched.profile == base.profile

    runtime._installing = True
    installing = runtime.status_for_paths(paths.models / base_executable, paths.models / base_model)
    assert installing.state is SttRuntimeState.installing
    assert installing.profile == base.profile
    runtime._installing = False

    unsupported = ManagedChineseSttRuntime(
        paths,
        profile=base.profile,
        platform_supported=lambda: False,
        directory_security=PortableDirectorySecurity(),
    )
    status = unsupported.status_for_paths(paths.models / base_executable, paths.models / base_model)
    assert status.state is SttRuntimeState.unsupported

    invalid_paths = Settings(
        stt=STTConfig(
            managed_profile=base.profile,
            executable=Path("../outside.exe"),
            model_path=base_model,
        )
    )
    invalid_paths._paths = paths
    invalid = runtime.status_for_settings(invalid_paths)
    assert invalid.state is SttRuntimeState.integrity_failed
    assert invalid.profile == base.profile


def test_managed_runtime_repairs_corrupt_asset_without_using_network_at_status_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    factory, requested = _client_factory(archive, model)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda binary: _assert_synthetic_binary(binary, executable),
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        await runtime.install()
        model_path = paths.models / MANAGED_STT_MODEL_RELATIVE
        model_path.write_bytes(b"tampered")
        executable_path = paths.models / MANAGED_STT_EXECUTABLE_RELATIVE
        assert (
            runtime.status_for_paths(
                executable_path,
                model_path,
            ).state
            is SttRuntimeState.integrity_failed
        )
        assert len(requested) == 2

        repaired = await runtime.install()
        assert repaired.changed
        assert model_path.read_bytes() == model
        assert len(requested) == 4

    asyncio.run(scenario())


def test_install_rejects_malicious_archive_and_keeps_final_runtime_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive(traversal=True)
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    factory, _requested = _client_factory(archive, model)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        with pytest.raises(SttRuntimeError) as caught:
            await runtime.install()
        assert caught.value.code == "stt_runtime_archive_invalid"
        assert not (paths.models / "stt" / "whispercpp" / "v1.9.1").exists()

    asyncio.run(scenario())


def test_install_rejects_archive_link_and_keeps_final_runtime_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive(link=True)
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    factory, _requested = _client_factory(archive, model)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        with pytest.raises(SttRuntimeError) as caught:
            await runtime.install()
        assert caught.value.code == "stt_runtime_archive_invalid"
        assert not (paths.models / "stt" / "whispercpp" / "v1.9.1").exists()

    asyncio.run(scenario())


def test_status_never_creates_or_downloads_for_unmanaged_paths(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    called = False

    def unexpected_client() -> httpx.AsyncClient:
        nonlocal called
        called = True
        raise AssertionError("status must not construct an HTTP client")

    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=unexpected_client,
        platform_supported=lambda: True,
    )

    status = runtime.status_for_paths(tmp_path / "manual-cli.exe", tmp_path / "manual-model.bin")

    assert status.state is SttRuntimeState.unmanaged
    assert not called
    assert not paths.root.exists()


def test_missing_managed_status_never_creates_or_downloads(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    called = False

    def unexpected_client() -> httpx.AsyncClient:
        nonlocal called
        called = True
        raise AssertionError("status must not construct an HTTP client")

    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=unexpected_client,
        platform_supported=lambda: True,
    )

    assert (
        runtime.status_for_paths(
            paths.models / MANAGED_STT_EXECUTABLE_RELATIVE,
            paths.models / MANAGED_STT_MODEL_RELATIVE,
        ).state
        is SttRuntimeState.missing
    )
    assert not called
    assert not paths.root.exists()


def test_install_rejects_hash_mismatch_without_activating_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    manifest = _manifest(archive, executable, model)
    _set_profiles(
        monkeypatch,
        replace(
            manifest,
            runtime_archive=SttAsset(
                url=manifest.runtime_archive.url,
                sha256="0" * 64,
                maximum_bytes=manifest.runtime_archive.maximum_bytes,
            ),
        ),
    )
    factory, _requested = _client_factory(archive, model)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        with pytest.raises(SttRuntimeError) as caught:
            await runtime.install()
        assert caught.value.code == "stt_runtime_integrity_failed"
        assert not (paths.models / "stt" / "whispercpp" / "v1.9.1").exists()
        assert not list((paths.models / "stt" / "whispercpp").glob(".install-*"))

    asyncio.run(scenario())


def test_install_timeout_cleans_staging_and_never_activates_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    monkeypatch.setattr(runtime_module, "_DOWNLOAD_TOTAL_TIMEOUT_SECONDS", 0.05)

    class _SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(10)
            yield archive

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowStream(), request=request)

    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        with pytest.raises(SttRuntimeError) as caught:
            await runtime.install()
        assert caught.value.code == "stt_download_failed"
        assert not (paths.models / "stt" / "whispercpp" / "v1.9.1").exists()
        assert not list((paths.models / "stt" / "whispercpp").glob(".install-*"))

    asyncio.run(scenario())


def test_install_rejects_https_redirect_downgrade_before_requesting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://assets.example.invalid/whisper-bin-x64.zip"},
            request=request,
        )

    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        with pytest.raises(SttRuntimeError) as caught:
            await runtime.install()
        assert caught.value.code == "stt_download_failed"
        assert requested == ["https://assets.example.invalid/whisper-bin-x64.zip"]
        assert not (paths.models / "stt" / "whispercpp" / "v1.9.1").exists()

    asyncio.run(scenario())


def test_install_never_imports_or_uses_microphone_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))

    class _ForbiddenSoundDevice:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"installer must not access sounddevice.{name}")

    monkeypatch.setitem(sys.modules, "sounddevice", _ForbiddenSoundDevice())
    factory, _requested = _client_factory(archive, model)
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        await runtime.install()

    asyncio.run(scenario())


def test_repair_rejects_existing_tree_with_reparse_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    target = paths.models / "stt" / "whispercpp" / "v1.9.1"
    target.mkdir(parents=True)
    marker = target / "synthetic-reparse-marker"
    marker.write_text("not a real reparse point", encoding="ascii")
    monkeypatch.setattr(runtime_module, "is_reparse_point", lambda path: path == marker)
    factory, _requested = _client_factory(archive, model)
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=factory,
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        with pytest.raises(SttRuntimeError) as caught:
            await runtime.install()
        assert caught.value.code == "stt_install_failed"
        assert marker.exists()
        assert not list(target.parent.glob(".install-*"))

    asyncio.run(scenario())


def test_concurrent_or_cancelled_install_never_activates_partial_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, executable = _runtime_archive()
    model = b"synthetic-model"
    _set_profiles(monkeypatch, _manifest(archive, executable, model))
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingArchive(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await release.wait()
            yield archive

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(".zip")
        return httpx.Response(200, stream=_BlockingArchive(), request=request)

    paths = AppPaths(root=tmp_path / "AppData" / "MeguminCompanion")
    runtime = ManagedChineseSttRuntime(
        paths,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        platform_supported=lambda: True,
        version_probe=lambda _binary: None,
        directory_security=PortableDirectorySecurity(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(runtime.install())
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(SttRuntimeError, match="stt_install_busy"):
            await runtime.install()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (paths.models / "stt" / "whispercpp" / "v1.9.1").exists()
        assert not list((paths.models / "stt" / "whispercpp").glob(".install-*"))
        release.set()

    asyncio.run(scenario())


def _assert_synthetic_binary(path: Path, expected: bytes) -> None:
    assert path.name == "whisper-cli.exe"
    assert path.read_bytes() == expected
