"""Typed YAML and environment configuration for the local application."""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    model_validator,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


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


class LoggingConfig(StrictModel):
    console_enabled: bool = True
    file_enabled: bool = True
    file_path: Path = Path("data/logs/app.jsonl")


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
    reconnect_initial_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    reconnect_max_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    expression_hotkeys: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_reconnect_bounds(self) -> VTSConfig:
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("reconnect_max_seconds 不能小于 reconnect_initial_seconds")
        if not self.uri.startswith(("ws://", "wss://")):
            raise ValueError("VTS uri 必须使用 WebSocket")
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
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)

    _environment: dict[str, str] = PrivateAttr(default_factory=dict)

    def require_secret(self, env_name: str) -> SecretStr:
        """Return a configured secret without exposing it in repr or serialization."""

        value = self._environment.get(env_name, "").strip()
        if not value or value == "replace_me":
            raise ConfigurationError(
                f"缺少必需的密钥环境变量 {env_name}。请复制 .env.example 为 .env，"
                "填入专用且额度受限的密钥；不要把 .env 提交到 Git。"
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
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(
            f"配置文件不存在：{path}。请从仓库根目录运行，或显式传入有效配置路径。"
        )
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"无法读取配置文件 {path}：{exc}") from exc
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ConfigurationError(f"配置文件 {path} 的顶层必须是键值映射。")
    return content


def _merged_environment(env_path: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    from_file = {
        key: value for key, value in dotenv_values(env_path).items() if isinstance(value, str)
    }
    process_env = dict(os.environ if environ is None else environ)
    return {**from_file, **process_env}


def _apply_environment_overrides(data: dict[str, Any], environment: Mapping[str, str]) -> None:
    for env_name, (section_name, field_name) in ENV_OVERRIDES.items():
        if env_name not in environment:
            continue
        section = data.setdefault(section_name, {})
        if not isinstance(section, dict):
            continue
        section[field_name] = environment[env_name]


def load_settings(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    env_path: Path | str = DEFAULT_ENV_PATH,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load YAML defaults, then overlay `.env` and process environment values."""

    resolved_config = Path(config_path).expanduser().resolve()
    resolved_env = Path(env_path).expanduser().resolve()
    raw_config = _read_yaml(resolved_config)
    environment = _merged_environment(resolved_env, environ)
    _apply_environment_overrides(raw_config, environment)
    try:
        settings = Settings.model_validate(raw_config)
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ConfigurationError(f"配置文件 {resolved_config} 无效：{issues}") from exc

    settings._environment = environment
    if settings.llm.provider.lower() not in {"none", "mock"}:
        settings.require_llm_api_key()
    return settings
