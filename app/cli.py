"""Explicit command-line entry point for configuration checks and the development API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.clients.vts import DPAPITokenStore, read_legacy_plaintext_token
from app.config import ConfigurationError, Settings, load_settings
from app.config.user_settings import upgrade_user_settings
from app.legacy_migration import LegacyMigrationError, migrate_legacy_data
from app.main import create_app
from app.paths import AppPathError, AppPaths
from app.secret_store import (
    LLM_API_KEY_ID,
    VTS_TOKEN_ID,
    SecretStoreError,
    SecretStoreErrorCode,
    llm_api_key_file,
    vts_token_file,
)

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megumin-companion-api",
        description=(
            "Validate packaged configuration or explicitly start the local development API. "
            "The Windows production desktop does not use this network entry point."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        help="Use an explicit YAML configuration instead of the packaged default.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Use an explicit development .env file; process environment values still win.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting the database, devices, or network.",
    )
    action.add_argument(
        "--serve",
        action="store_true",
        help="Explicitly start the current local development HTTP/WebSocket API.",
    )
    action.add_argument(
        "--upgrade-settings",
        action="store_true",
        help="Explicitly upgrade LocalAppData user settings with an atomic backup.",
    )
    action.add_argument(
        "--migrate-from",
        type=Path,
        metavar="OLD_DATA",
        help="Explicitly copy and verify an old repository data/ tree; never delete it.",
    )
    action.add_argument(
        "--import-llm-key-env",
        nargs="?",
        const="",
        metavar="ENV_NAME",
        help=(
            "Import or replace the LLM key from an explicit process environment variable "
            "into current-user DPAPI; defaults to the configured api_key_env."
        ),
    )
    action.add_argument(
        "--import-vts-token",
        type=Path,
        metavar="TOKEN_FILE",
        help="Import or replace one explicitly selected legacy VTS token file into DPAPI.",
    )
    action.add_argument(
        "--revoke-secret",
        choices=(LLM_API_KEY_ID, VTS_TOKEN_ID),
        metavar="SECRET_ID",
        help="Revoke one encrypted secret by stable id.",
    )
    action.add_argument(
        "--reset-secret",
        choices=(LLM_API_KEY_ID, VTS_TOKEN_ID),
        metavar="SECRET_ID",
        help="Delete one unreadable encrypted secret so it can be re-entered.",
    )
    parser.add_argument(
        "--delete-import-source",
        action="store_true",
        help=(
            "After a verified VTS import, delete the explicitly selected plaintext source. "
            "This is file-level deletion, not guaranteed physical erasure."
        ),
    )
    parser.add_argument("--host", help="Override the configured development API host.")
    parser.add_argument("--port", type=int, help="Override the configured development API port.")
    return parser


def _load_settings(config: Path | None, env_file: Path | None) -> Settings:
    return load_settings(config_path=config, env_path=env_file)


def check_configuration(config: Path | None, env_file: Path | None) -> int:
    """Validate and summarize configuration without constructing runtime resources."""

    try:
        settings = _load_settings(config, env_file)
    except ConfigurationError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return 2
    summary = {
        "status": "ok",
        "schema_version": settings.schema_version,
        "schema_upgrade_required": settings.settings_schema_upgrade_required,
        "source": settings.config_source,
        "environment": settings.app.environment,
        "llm_provider": settings.llm.provider,
        "tts_provider": settings.tts.provider,
        "playback_mode": settings.pipeline.playback_mode,
        "storage_enabled": settings.storage.enabled,
        "vts_enabled": settings.vts.enabled,
        "stt_enabled": settings.stt.enabled,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _upgrade_local_settings() -> int:
    try:
        result = upgrade_user_settings()
    except (ConfigurationError, AppPathError) as exc:
        print(f"settings_upgrade_error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "changed": result.changed,
                "backup_created": result.backup_created,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _migrate_old_data(source: Path) -> int:
    try:
        paths = AppPaths.discover()
        result = migrate_legacy_data(source, app_paths=paths)
    except (LegacyMigrationError, AppPathError, OSError) as exc:
        print(f"migration_error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    print(
        "migration_next_action: 旧数据仍保留。请先启动并人工核对对话/模型，"
        "确认无误后再手动删除旧 data；.env/VTS token 必须重新输入。"
    )
    return 0


def _load_production_settings_for_secret_action() -> Settings:
    """Load user/default settings without applying any process overrides."""

    paths = AppPaths.discover()
    return load_settings(environ={}, app_paths=paths)


def _import_llm_key(environment_name: str) -> int:
    try:
        settings = _load_production_settings_for_secret_action()
        selected_name = environment_name or settings.llm.api_key_env or ""
        if not _ENVIRONMENT_NAME.fullmatch(selected_name):
            raise ConfigurationError("LLM secret 环境变量名无效。")
        value = os.environ.get(selected_name, "")
        if not value.strip():
            raise ConfigurationError("指定的 LLM secret 环境变量为空或不存在。")
        secret_file = llm_api_key_file(settings.paths)
        replaced = secret_file.exists
        metadata = secret_file.write_text(value)
        os.environ.pop(selected_name, None)
    except (ConfigurationError, AppPathError, SecretStoreError) as exc:
        print(f"secret_import_error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "secret_id": metadata.key_id,
                "scope": metadata.scope,
                "replaced": replaced,
                "source": "explicit_environment",
                "next_action": "remove the plaintext value from the parent environment and .env",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _import_vts_token(source: Path, *, delete_source: bool) -> int:
    try:
        settings = _load_production_settings_for_secret_action()
        token = read_legacy_plaintext_token(source)
        target_path = settings.vts_token_path()
        encrypted = vts_token_file(settings.paths, target_path)
        store = DPAPITokenStore(encrypted)
        replaced = encrypted.exists
        same_file = source.absolute() == target_path.absolute()
        if same_file and not delete_source:
            raise ConfigurationError(
                "明文 VTS source 与密文目标相同；请先保留人工确认用备份，"
                "或显式使用 --delete-import-source 授权原位替换。"
            )
        asyncio.run(store.save(token))
        if asyncio.run(store.load()) != token:
            raise SecretStoreError(SecretStoreErrorCode.corrupt, VTS_TOKEN_ID)
        source_removed = same_file
        if delete_source and not same_file:
            try:
                source.unlink()
            except OSError as exc:
                raise SecretStoreError(SecretStoreErrorCode.io_failed, VTS_TOKEN_ID) from exc
            source_removed = True
    except (ConfigurationError, AppPathError, SecretStoreError) as exc:
        print(f"secret_import_error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "secret_id": VTS_TOKEN_ID,
                "scope": encrypted.metadata.scope,
                "replaced": replaced,
                "plaintext_source_removed": source_removed,
                "physical_erasure_guaranteed": False,
                "next_action": (
                    "remove the retained plaintext source after validating VTS authentication"
                    if not source_removed
                    else "validate VTS authentication and keep the encrypted store"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _delete_secret(secret_id: str, *, reset: bool) -> int:
    try:
        settings = _load_production_settings_for_secret_action()
        if secret_id == LLM_API_KEY_ID:
            secret_file = llm_api_key_file(settings.paths)
        else:
            secret_file = vts_token_file(
                settings.paths,
                settings.vts_token_path(),
            )
        changed = secret_file.reset() if reset else secret_file.revoke()
    except (ConfigurationError, AppPathError, SecretStoreError) as exc:
        print(f"secret_delete_error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "action": "reset" if reset else "revoke",
                "secret_id": secret_id,
                "changed": changed,
                "physical_erasure_guaranteed": False,
                "next_action": "re-enter the secret before enabling its provider",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_server(application: FastAPI, *, host: str, port: int) -> None:
    uvicorn.run(application, host=host, port=port, log_config=None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_config:
        return check_configuration(args.config, args.env_file)
    if args.upgrade_settings:
        if args.config is not None or args.env_file is not None:
            parser.error("--upgrade-settings cannot be combined with development overrides")
        return _upgrade_local_settings()
    if args.migrate_from is not None:
        if args.config is not None or args.env_file is not None:
            parser.error("--migrate-from cannot be combined with development overrides")
        return _migrate_old_data(args.migrate_from)
    if args.import_llm_key_env is not None:
        if args.config is not None or args.env_file is not None or args.delete_import_source:
            parser.error("--import-llm-key-env cannot use config/env-file/source deletion options")
        return _import_llm_key(args.import_llm_key_env)
    if args.import_vts_token is not None:
        if args.config is not None or args.env_file is not None:
            parser.error("--import-vts-token cannot be combined with development overrides")
        return _import_vts_token(
            args.import_vts_token,
            delete_source=args.delete_import_source,
        )
    if args.revoke_secret is not None:
        if args.config is not None or args.env_file is not None or args.delete_import_source:
            parser.error("--revoke-secret cannot use config/env-file/source deletion options")
        return _delete_secret(args.revoke_secret, reset=False)
    if args.reset_secret is not None:
        if args.config is not None or args.env_file is not None or args.delete_import_source:
            parser.error("--reset-secret cannot use config/env-file/source deletion options")
        return _delete_secret(args.reset_secret, reset=True)
    if args.delete_import_source:
        parser.error("--delete-import-source requires --import-vts-token")
    if not args.serve:
        parser.print_help()
        return 0

    try:
        settings = _load_settings(args.config, args.env_file)
    except ConfigurationError as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        return 2
    host = args.host or settings.server.host
    port = args.port if args.port is not None else settings.server.port
    if not 1 <= port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    _run_server(create_app(settings), host=host, port=port)
    return 0
