"""W02 centralized LocalAppData path policy."""

import os
from pathlib import Path

import pytest
from app.paths import (
    APP_DIRECTORY_NAME,
    AppPathError,
    AppPaths,
    _default_non_windows_data_home,
)


@pytest.mark.parametrize(
    ("platform_name", "relative_base"),
    [
        ("Darwin", Path("Library/Application Support")),
        ("Linux", Path(".local/share")),
        ("FreeBSD", Path(".local/share")),
    ],
)
def test_default_non_windows_data_home(
    tmp_path: Path, platform_name: str, relative_base: Path
) -> None:
    assert _default_non_windows_data_home(tmp_path, platform_name) == (tmp_path / relative_base)


def test_redirected_local_appdata_exposes_every_managed_category(tmp_path: Path) -> None:
    redirected = tmp_path / "D 盘" / ("很长的目录" * 12)

    paths = AppPaths.discover({"LOCALAPPDATA": str(redirected)})

    assert paths.root == redirected / APP_DIRECTORY_NAME
    assert paths.settings == paths.root / "config" / "settings.yaml"
    assert paths.state == paths.root / "state"
    assert paths.secrets == paths.root / "secrets"
    assert paths.logs == paths.root / "logs"
    assert paths.audio_cache == paths.root / "cache" / "audio"
    assert paths.temp == paths.root / "temp"
    assert paths.models == paths.root / "models"
    assert not paths.root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter contract")
@pytest.mark.parametrize("drive", ["C", "D"])
def test_windows_drive_letter_is_preserved_without_disk_access(drive: str) -> None:
    base = Path(f"{drive}:\\用户 数据\\Local")

    paths = AppPaths.from_local_app_data(base)

    assert paths.root.drive.casefold() == f"{drive}:".casefold()


@pytest.mark.parametrize("value", [Path("../escape"), Path("nested/../../escape")])
def test_managed_paths_reject_parent_traversal(tmp_path: Path, value: Path) -> None:
    paths = AppPaths.from_local_app_data(tmp_path)

    with pytest.raises(AppPathError, match="不能离开"):
        paths.managed(paths.state, value, category="数据库")


def test_managed_paths_reject_absolute_override(tmp_path: Path) -> None:
    paths = AppPaths.from_local_app_data(tmp_path)

    with pytest.raises(AppPathError, match="受管目录"):
        paths.managed(paths.logs, tmp_path / "elsewhere.log", category="日志")


def test_package_resources_are_independent_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_local_app_data(tmp_path)
    monkeypatch.chdir(tmp_path)

    resource = paths.resource("default_config.yaml")

    assert "schema_version: 1" in resource.read_text(encoding="utf-8")
    with pytest.raises(AppPathError):
        paths.resource("../config.yaml")
