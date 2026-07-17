"""Schema-aware, atomic persistence for per-user configuration overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from app.config.settings import (
    CURRENT_SETTINGS_SCHEMA_VERSION,
    ConfigurationError,
    _deep_merge,
    _read_default_yaml,
    _read_yaml_path,
    _upgrade_config_data,
    _validate_runtime_paths,
    _validate_settings_data,
)
from app.paths import AppPaths

_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class UserSettingsWriteResult:
    changed: bool
    backup_created: bool


def _reject_plaintext_secrets(value: Any, *, location: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            if key in _SECRET_FIELD_NAMES or key.endswith("_password") or key.endswith("_secret"):
                dotted = ".".join((*location, key))
                raise ConfigurationError(f"用户设置禁止保存明文 secret 字段：{dotted}")
            _reject_plaintext_secrets(item, location=(*location, key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_plaintext_secrets(item, location=location)


def _validated_user_layer(overrides: Mapping[str, Any], paths: AppPaths) -> dict[str, Any]:
    _reject_plaintext_secrets(overrides)
    layer, _ = _upgrade_config_data(overrides, source="用户设置")
    defaults, changed = _upgrade_config_data(_read_default_yaml(), source="内置默认配置")
    if changed:
        raise ConfigurationError("内置默认配置缺少当前 schema_version；安装产物不完整。")
    settings = _validate_settings_data(_deep_merge(defaults, layer), source="用户设置")
    settings._paths = paths
    _validate_runtime_paths(settings)
    return layer


def _write_and_sync(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _yaml_compatible(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _yaml_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_yaml_compatible(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def write_user_settings(
    overrides: Mapping[str, Any], *, app_paths: AppPaths | None = None
) -> UserSettingsWriteResult:
    """Validate and atomically replace user overrides, preserving one backup."""

    paths = app_paths or AppPaths.discover()
    layer = _validated_user_layer(overrides, paths)
    layer["schema_version"] = CURRENT_SETTINGS_SCHEMA_VERSION
    serialized = yaml.safe_dump(
        _yaml_compatible(layer),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")

    target = paths.settings
    if target.is_symlink():
        raise ConfigurationError("用户设置不能是符号链接。")
    try:
        if target.is_file() and target.read_bytes() == serialized:
            return UserSettingsWriteResult(changed=False, backup_created=False)
    except OSError as exc:
        raise ConfigurationError(
            "无法读取现有用户设置：" + (exc.strerror or type(exc).__name__)
        ) from exc

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            "无法创建用户配置目录：" + (exc.strerror or type(exc).__name__)
        ) from exc

    nonce = uuid4().hex
    pending = target.parent / f".settings-{nonce}.tmp"
    backup_pending = target.parent / f".settings-backup-{nonce}.tmp"
    backup_created = False
    try:
        _write_and_sync(pending, serialized)
        if target.is_file():
            _write_and_sync(backup_pending, target.read_bytes())
            os.replace(backup_pending, paths.settings_backup)
            backup_created = True
        os.replace(pending, target)
    except OSError as exc:
        raise ConfigurationError(
            "用户设置原子写入失败；原文件保持不变：" + (exc.strerror or type(exc).__name__)
        ) from exc
    finally:
        for temporary in (pending, backup_pending):
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return UserSettingsWriteResult(changed=True, backup_created=backup_created)


def upgrade_user_settings(*, app_paths: AppPaths | None = None) -> UserSettingsWriteResult:
    """Persist an in-memory schema upgrade only after an explicit user action."""

    paths = app_paths or AppPaths.discover()
    raw = _read_yaml_path(paths.settings, source_label="用户设置")
    upgraded, changed = _upgrade_config_data(raw, source="用户设置")
    if not changed:
        _validated_user_layer(upgraded, paths)
        return UserSettingsWriteResult(changed=False, backup_created=False)
    return write_user_settings(upgraded, app_paths=paths)
