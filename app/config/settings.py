"""Typed YAML and environment configuration for the local application."""

from __future__ import annotations

import os
from collections.abc import Mapping
from copy import deepcopy
from importlib import resources
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from app.limits import LimitsConfig
from app.paths import AppPathError, AppPaths

DEFAULT_CONFIG_PACKAGE = "app.resources"
DEFAULT_CONFIG_NAME = "default_config.yaml"
CURRENT_SETTINGS_SCHEMA_VERSION = 1


class ConfigurationError(RuntimeError):
    """Raised when local configuration is absent or invalid."""


class StrictModel(BaseModel):
    """Reject misspelled configuration keys instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class AppConfig(StrictModel):
    name: str = "Megumin Desktop Companion AI"
    environment: Literal["dev", "test", "prod"] = "dev"
    timezone: str = "Asia/Shanghai"
    language: str = "zh-CN"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def require_numeric_loopback(cls, value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("[") != candidate.endswith("]"):
            raise ValueError("server.host 必须是数字 loopback 地址")
        if candidate.startswith("["):
            candidate = candidate[1:-1]
        try:
            address = ip_address(candidate)
        except ValueError as exc:
            raise ValueError("server.host 必须是数字 loopback 地址") from exc
        if not address.is_loopback:
            raise ValueError("server.host 必须是数字 loopback 地址")
        return address.compressed


class LoggingConfig(StrictModel):
    console_enabled: bool = True
    file_enabled: bool = True
    file_path: Path = Path("data/logs/app.jsonl")
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=256, le=10 * 1024 * 1024)
    file_count: int = Field(default=5, ge=1, le=5)
    retention_days: int = Field(default=14, ge=1, le=14)


class LLMConfig(StrictModel):
    provider: str = "none"
    api_key_env: str | None = "COMPANION_LLM_API_KEY"
    base_url: str = "https://api.openai.com"
    endpoint: str = "/v1/chat/completions"
    model: str = ""
    timeout_seconds: float = Field(default=45.0, gt=0.0, le=300.0)
    temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    max_tokens: int = Field(default=600, ge=1, le=100_000)


class GPTSoVITSPresetConfig(StrictModel):
    ref_audio_path: str = Field(min_length=1)
    ref_audio_scope: Literal["service_resource", "local_file"] = "service_resource"
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    top_k: int = Field(default=5, ge=1)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    temperature: float = Field(default=1.0, gt=0.0)
    text_split_method: str = "cut5"
    batch_size: int = Field(default=1, ge=1)
    batch_threshold: float = 0.75
    split_bucket: bool = True
    speed_factor: float = Field(default=1.0, gt=0.0)
    fragment_interval: float = Field(default=0.3, ge=0.0)
    seed: int = -1
    parallel_infer: bool = True
    repetition_penalty: float = Field(default=1.35, gt=0.0)


class TTSConfig(StrictModel):
    provider: str = "mock"
    base_url: str = "http://127.0.0.1:9880"
    output_directory: Path = Path("data/cache/audio/gpt-sovits/ephemeral")
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_audio_bytes: int = Field(default=32 * 1024 * 1024, ge=44)
    default_preset: str = "default"
    presets: dict[str, GPTSoVITSPresetConfig] = Field(default_factory=dict)
    cache_enabled: bool = False
    cache_directory: Path = Path("data/cache/audio/gpt-sovits/persistent")
    cache_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    cache_ttl_seconds: float = Field(default=7 * 24 * 60 * 60, gt=0.0)


class VTSConfig(StrictModel):
    enabled: bool = False
    uri: str = "ws://127.0.0.1:8001"
    plugin_name: str = Field(default="Megumin Companion", min_length=3, max_length=32)
    plugin_developer: str = Field(default="Local User", min_length=3, max_length=32)
    token_path: Path = Path("data/private/vts-token.json")
    request_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    queue_capacity: int = Field(default=16, ge=1, le=256)
    reconnect_initial_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    reconnect_max_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    expression_hotkeys: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_reconnect_bounds(self) -> VTSConfig:
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds 不能小于 reconnect_initial_seconds")
        if not self.uri.startswith(("ws://", "wss://")):
            raise ValueError("VTS uri 必须使用 WebSocket")
        if any(
            not expression.strip() or not hotkey_id.strip()
            for expression, hotkey_id in self.expression_hotkeys.items()
        ):
            raise ValueError("VTS expression/hotkey ID 不能为空")
        return self


class EmotionConfig(StrictModel):
    enabled: bool = True
    max_delta_per_event: float = Field(default=0.15, gt=0.0, le=1.0)
    max_delta_per_minute: float = Field(default=0.25, gt=0.0, le=1.0)
    label_min_duration_seconds: float = Field(default=15.0, ge=0.0, le=300.0)
    explosion_cooldown_seconds: float = Field(default=300.0, ge=0.0, le=3600.0)
    expression_cooldown_seconds: float = Field(default=4.0, ge=0.0, le=300.0)


class StorageConfig(StrictModel):
    enabled: bool = False
    database_path: Path = Path("data/private/companion.sqlite3")
    busy_timeout_ms: int = Field(default=5_000, ge=0, le=60_000)


class MemoryConfig(StrictModel):
    history_retention_days: int = Field(default=7, ge=1, le=365)
    confirmation_ttl_minutes: float = Field(default=15.0, gt=0.0, le=1440.0)
    candidate_analysis_enabled: bool = False


class PerceptionConfig(StrictModel):
    operation_timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    max_frame_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    minimum_hash_distance: float = Field(default=0.08, ge=0.0, le=1.0)
    max_tracked_windows: int = Field(default=128, ge=1, le=10_000)
    ocr_max_spans: int = Field(default=2_000, ge=1, le=20_000)
    ocr_max_span_chars: int = Field(default=2_000, ge=1, le=20_000)
    max_ocr_text_chars: int = Field(default=100_000, ge=1, le=1_000_000)
    max_summary_chars: int = Field(default=2_000, ge=1, le=20_000)
    cloud_max_calls: int = Field(default=6, ge=1, le=10_000)
    cloud_period_seconds: float = Field(default=60.0, gt=0.0, le=86_400.0)


class ProactiveConfig(StrictModel):
    minimum_score: float = Field(default=0.62, ge=0.0, le=1.0)
    cooldown_seconds: float = Field(default=1_200.0, ge=0.0, le=604_800.0)
    idle_minimum_seconds: float = Field(default=180.0, ge=0.0, le=86_400.0)
    perception_max_age_seconds: float = Field(default=30.0, gt=0.0, le=3_600.0)
    daily_limit: int = Field(default=8, ge=1, le=1_000)
    quiet_start_hour: int = Field(default=23, ge=0, le=23)
    quiet_end_hour: int = Field(default=8, ge=0, le=23)


class STTConfig(StrictModel):
    enabled: bool = False
    provider: str = "whisper_cpp"
    executable: Path = Path("vendor/whisper.cpp/build/bin/whisper-cli")
    model_path: Path = Path("data/models/whisper/ggml-base.bin")
    language: str = "auto"
    threads: int | None = Field(default=None, ge=1)
    temporary_directory: Path = Path("data/private/stt")
    terminate_grace_seconds: float = Field(default=0.5, gt=0.0, le=30.0)
    max_audio_bytes: int = Field(default=64 * 1024 * 1024, ge=44)
    max_output_bytes: int = Field(default=2 * 1024 * 1024, ge=1)
    max_recording_seconds: float = Field(default=120.0, gt=0.0, le=3_600.0)
    transcription_timeout_seconds: float = Field(default=60.0, gt=0.0, le=3_600.0)
    device: int | str | None = None
    blocksize: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_runtime_names(self) -> STTConfig:
        if not self.provider.strip():
            raise ValueError("STT provider 不能为空")
        if not self.language.strip():
            raise ValueError("STT language 不能为空")
        return self


class PipelineConfig(StrictModel):
    tts_worker_count: int = Field(default=2, ge=1, le=8)
    segment_min_chars: int = Field(default=6, ge=1, le=100)
    segment_max_chars: int = Field(default=42, ge=1, le=500)
    segment_max_words: int = Field(default=25, ge=1, le=100)
    mock_token_delay_ms: int = Field(default=20, ge=0, le=10_000)
    mock_audio_duration_ms: int = Field(default=180, ge=0, le=10_000)
    mock_audio_volume: float = Field(default=0.12, ge=0.0, le=1.0)
    audio_cache_path: Path = Path("data/cache/audio/mock")
    playback_mode: Literal["silent", "system"] = "silent"

    @model_validator(mode="after")
    def validate_segment_lengths(self) -> PipelineConfig:
        if self.segment_max_chars < self.segment_min_chars:
            raise ValueError("segment_max_chars 必须不小于 segment_min_chars")
        return self


class Settings(StrictModel):
    schema_version: int = Field(
        default=CURRENT_SETTINGS_SCHEMA_VERSION,
        ge=CURRENT_SETTINGS_SCHEMA_VERSION,
        le=CURRENT_SETTINGS_SCHEMA_VERSION,
    )
    app: AppConfig = Field(default_factory=AppConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    vts: VTSConfig = Field(default_factory=VTSConfig)
    emotion: EmotionConfig = Field(default_factory=EmotionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    _environment: dict[str, str] = PrivateAttr(default_factory=dict)
    _paths: AppPaths = PrivateAttr(default_factory=AppPaths.discover)
    _config_source: str = PrivateAttr(default="programmatic settings")
    _settings_schema_upgrade_required: bool = PrivateAttr(default=False)

    @property
    def config_source(self) -> str:
        """Return a non-sensitive description of the active configuration source."""

        return self._config_source

    @property
    def paths(self) -> AppPaths:
        return self._paths

    @property
    def settings_schema_upgrade_required(self) -> bool:
        """Whether user settings were upgraded in memory but not written back."""

        return self._settings_schema_upgrade_required

    def log_file_path(self) -> Path:
        return self._managed_path(self._paths.logs, self.logging.file_path, "日志")

    def database_path(self) -> Path:
        return self._managed_path(self._paths.state, self.storage.database_path, "数据库")

    def vts_token_path(self) -> Path:
        return self._managed_path(self._paths.secrets, self.vts.token_path, "VTS token")

    def tts_output_directory(self) -> Path:
        return self._managed_path(self._paths.temp / "audio", self.tts.output_directory, "TTS 临时")

    def tts_cache_directory(self) -> Path:
        return self._managed_path(self._paths.audio_cache, self.tts.cache_directory, "TTS 缓存")

    def mock_audio_directory(self) -> Path:
        return self._managed_path(
            self._paths.temp / "audio", self.pipeline.audio_cache_path, "Mock 音频"
        )

    def stt_temporary_directory(self) -> Path:
        return self._managed_path(self._paths.temp, self.stt.temporary_directory, "STT 临时")

    def stt_executable_path(self) -> Path:
        return self._paths.external_or_model(self.stt.executable, category="STT 可执行文件")

    def stt_model_path(self) -> Path:
        return self._paths.external_or_model(self.stt.model_path, category="STT 模型")

    def _managed_path(self, base: Path, value: Path, category: str) -> Path:
        try:
            return self._paths.managed(base, value, category=category)
        except AppPathError as exc:
            raise ConfigurationError(str(exc)) from exc

    def require_secret(self, env_name: str) -> SecretStr:
        """Return a configured secret without exposing it in repr or serialization."""

        value = self._environment.get(env_name, "").strip()
        if not value or value == "replace_me":
            raise ConfigurationError(
                f"缺少必需的密钥 {env_name}。开发模式只能通过显式 --env-file 或进程环境"
                "提供；生产密钥必须在 W03 后重新输入到 DPAPI secret store。"
            )
        return SecretStr(value)

    def require_llm_api_key(self) -> SecretStr:
        if not self.llm.api_key_env:
            raise ConfigurationError(
                "llm.provider 已启用，但 llm.api_key_env 为空；"
                "请在 config.yaml 中指定密钥环境变量名。"
            )
        return self.require_secret(self.llm.api_key_env)

    def known_secret_values(self) -> tuple[str, ...]:
        """Return configured secret values solely for exact-match log redaction."""

        if not self.llm.api_key_env:
            return ()
        value = self._environment.get(self.llm.api_key_env, "").strip()
        return (value,) if value else ()

    def validate_runtime_limits(self) -> None:
        """Fail startup when a provider preference bypasses a W07 hard cap."""

        if self.llm.max_tokens > self.limits.provider_output_tokens:
            raise ValueError("llm.max_tokens exceeds provider output hard limit")
        if self.tts.max_audio_bytes > self.limits.audio_single_result_bytes:
            raise ValueError("tts.max_audio_bytes exceeds audio result hard limit")


ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "MEGUMIN_APP_NAME": ("app", "name"),
    "MEGUMIN_ENVIRONMENT": ("app", "environment"),
    "MEGUMIN_TIMEZONE": ("app", "timezone"),
    "MEGUMIN_LANGUAGE": ("app", "language"),
    "MEGUMIN_LOG_LEVEL": ("app", "log_level"),
    "MEGUMIN_SERVER_HOST": ("server", "host"),
    "MEGUMIN_SERVER_PORT": ("server", "port"),
    "MEGUMIN_LOG_FILE": ("logging", "file_path"),
    "MEGUMIN_LLM_PROVIDER": ("llm", "provider"),
    "MEGUMIN_LLM_API_KEY_ENV": ("llm", "api_key_env"),
    "MEGUMIN_LLM_BASE_URL": ("llm", "base_url"),
    "MEGUMIN_LLM_MODEL": ("llm", "model"),
    "MEGUMIN_TTS_PROVIDER": ("tts", "provider"),
    "MEGUMIN_TTS_BASE_URL": ("tts", "base_url"),
    "MEGUMIN_VTS_ENABLED": ("vts", "enabled"),
    "MEGUMIN_VTS_URI": ("vts", "uri"),
    "MEGUMIN_STORAGE_ENABLED": ("storage", "enabled"),
    "MEGUMIN_DATABASE_PATH": ("storage", "database_path"),
    "MEGUMIN_MEMORY_CANDIDATE_ANALYSIS_ENABLED": (
        "memory",
        "candidate_analysis_enabled",
    ),
    "MEGUMIN_STT_ENABLED": ("stt", "enabled"),
    "MEGUMIN_STT_EXECUTABLE": ("stt", "executable"),
    "MEGUMIN_STT_MODEL_PATH": ("stt", "model_path"),
}


LEGACY_PATH_REWRITES: tuple[tuple[str, str, str, str], ...] = (
    ("logging", "file_path", "data/logs/app.jsonl", "app.jsonl"),
    (
        "tts",
        "output_directory",
        "data/cache/audio/gpt-sovits/ephemeral",
        "tts/gpt-sovits/ephemeral",
    ),
    (
        "tts",
        "cache_directory",
        "data/cache/audio/gpt-sovits/persistent",
        "tts/gpt-sovits/persistent",
    ),
    ("vts", "token_path", "data/private/vts-token.json", "vts-token.json"),
    ("storage", "database_path", "data/private/companion.sqlite3", "companion.sqlite3"),
    ("stt", "model_path", "data/models/whisper/ggml-base.bin", "whisper/ggml-base.bin"),
    ("stt", "temporary_directory", "data/private/stt", "stt"),
    ("pipeline", "audio_cache_path", "data/cache/audio/mock", "mock"),
)


def _parse_yaml(content: str, *, source: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"无法解析{source}：{exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigurationError(f"{source}的顶层必须是键值映射。")
    return parsed


def _read_yaml_path(path: Path, *, source_label: str = "配置文件") -> dict[str, Any]:
    source = f"{source_label} {path.name or '<unnamed>'}"
    if not path.is_file():
        raise ConfigurationError(f"{source}不存在；请显式传入有效路径。")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"无法读取{source}：{exc.strerror or type(exc).__name__}") from exc
    return _parse_yaml(content, source=source)


def _read_default_yaml() -> dict[str, Any]:
    try:
        content = (
            resources.files(DEFAULT_CONFIG_PACKAGE)
            .joinpath(DEFAULT_CONFIG_NAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError) as exc:
        raise ConfigurationError("内置默认配置资源不可用；安装产物可能不完整。") from exc
    return _parse_yaml(content, source="内置默认配置")


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _upgrade_config_data(data: Mapping[str, Any], *, source: str) -> tuple[dict[str, Any], bool]:
    upgraded = deepcopy(dict(data))
    raw_version = upgraded.get("schema_version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 0:
        raise ConfigurationError(f"{source}的 schema_version 必须是非负整数。")
    if raw_version > CURRENT_SETTINGS_SCHEMA_VERSION:
        raise ConfigurationError(
            f"{source}使用较新的设置 schema_version={raw_version}；"
            f"当前仅支持 {CURRENT_SETTINGS_SCHEMA_VERSION}。"
        )
    changed = False
    if raw_version == 0:
        for section_name, field_name, legacy, replacement in LEGACY_PATH_REWRITES:
            section = upgraded.get(section_name)
            if isinstance(section, dict) and section.get(field_name) == legacy:
                section[field_name] = replacement
        upgraded["schema_version"] = 1
        raw_version = 1
        changed = True
    if raw_version != CURRENT_SETTINGS_SCHEMA_VERSION:
        raise ConfigurationError(f"{source}无法升级到当前设置 schema。")
    return upgraded, changed


def _validate_settings_data(data: Mapping[str, Any], *, source: str) -> Settings:
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ConfigurationError(f"{source}无效：{issues}") from exc


def _resolve_explicit_path(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    return candidate.resolve(strict=False)


def _read_explicit_environment(
    env_path: Path | str | None, environment: Mapping[str, str]
) -> dict[str, str]:
    from_file: dict[str, str] = {}
    if env_path is not None:
        resolved_env = _resolve_explicit_path(env_path)
        if not resolved_env.is_file():
            raise ConfigurationError(
                f"开发环境文件 {resolved_env.name or '<unnamed>'}不存在；不会自动回退到仓库 .env。"
            )
        try:
            from_file = {
                key: value
                for key, value in dotenv_values(resolved_env, interpolate=False).items()
                if isinstance(value, str)
            }
        except OSError as exc:
            raise ConfigurationError(
                "无法读取显式开发环境文件：" + (exc.strerror or type(exc).__name__)
            ) from exc
    return {**from_file, **environment}


def _apply_environment_overrides(data: dict[str, Any], environment: Mapping[str, str]) -> None:
    for env_name, (section_name, field_name) in ENV_OVERRIDES.items():
        if env_name not in environment:
            continue
        section = data.setdefault(section_name, {})
        if not isinstance(section, dict):
            continue
        section[field_name] = environment[env_name]


def _validate_runtime_paths(settings: Settings) -> None:
    """Validate every managed path without creating or opening it."""

    settings.log_file_path()
    settings.database_path()
    settings.vts_token_path()
    settings.tts_output_directory()
    settings.tts_cache_directory()
    settings.mock_audio_directory()
    settings.stt_temporary_directory()
    settings.stt_executable_path()
    settings.stt_model_path()
    try:
        settings.validate_runtime_limits()
    except ValueError as exc:
        raise ConfigurationError(f"运行时 hard limits 无效：{exc}") from exc


def load_settings(
    config_path: Path | str | None = None,
    env_path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    app_paths: AppPaths | None = None,
) -> Settings:
    """Load defaults, user settings, then explicit development-only overrides.

    The loader never searches the CWD for configuration or ``.env`` files and
    never writes an upgraded schema during preflight.
    """

    process_environment = dict(os.environ if environ is None else environ)
    try:
        paths = app_paths or AppPaths.discover(process_environment)
    except AppPathError as exc:
        raise ConfigurationError(str(exc)) from exc

    defaults, default_changed = _upgrade_config_data(_read_default_yaml(), source="内置默认配置")
    if default_changed:
        raise ConfigurationError("内置默认配置缺少当前 schema_version；安装产物不完整。")

    source_parts = ["内置默认配置"]
    user_changed = False
    if paths.settings.exists():
        user_raw = _read_yaml_path(paths.settings, source_label="用户设置")
        user_layer, user_changed = _upgrade_config_data(user_raw, source="用户设置")
        raw_config = _deep_merge(defaults, user_layer)
        source_parts.append("用户设置")
    else:
        raw_config = deepcopy(defaults)

    base_settings = _validate_settings_data(raw_config, source="基础设置")
    recognized_process_override = any(name in process_environment for name in ENV_OVERRIDES)
    dev_override_requested = (
        config_path is not None or env_path is not None or recognized_process_override
    )
    if base_settings.app.environment == "prod" and dev_override_requested:
        raise ConfigurationError(
            "生产设置禁止 --config、--env-file 和 MEGUMIN_* 环境覆盖；请更新用户设置。"
        )

    effective_environment: dict[str, str] = {}
    if base_settings.app.environment != "prod":
        if config_path is not None:
            resolved_config = _resolve_explicit_path(config_path)
            explicit_raw = _read_yaml_path(resolved_config, source_label="开发配置")
            explicit_layer, _ = _upgrade_config_data(explicit_raw, source="开发配置")
            raw_config = _deep_merge(raw_config, explicit_layer)
            source_parts.append(f"开发配置 {resolved_config.name or '<unnamed>'}")
        effective_environment = _read_explicit_environment(env_path, process_environment)
        if env_path is not None:
            source_parts.append("显式开发环境文件")
        if recognized_process_override:
            source_parts.append("开发环境覆盖")
        _apply_environment_overrides(raw_config, effective_environment)

    settings = _validate_settings_data(raw_config, source=" + ".join(source_parts))
    if settings.app.environment == "prod" and dev_override_requested:
        raise ConfigurationError("不能通过开发覆盖进入生产模式；请把 production 设置写入用户设置。")

    settings._environment = effective_environment
    settings._paths = paths
    settings._config_source = " + ".join(source_parts)
    settings._settings_schema_upgrade_required = user_changed
    _validate_runtime_paths(settings)
    return settings
