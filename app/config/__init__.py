"""Application configuration loading."""

from app.config.settings import (
    CURRENT_SETTINGS_SCHEMA_VERSION,
    AvatarConfig,
    ConfigurationError,
    DesktopConfig,
    Settings,
    load_settings,
)
from app.config.user_settings import (
    UserSettingsWriteResult,
    patch_user_settings,
    read_user_settings,
    upgrade_user_settings,
    write_user_settings,
)
from app.limits import LimitsConfig

__all__ = [
    "AvatarConfig",
    "CURRENT_SETTINGS_SCHEMA_VERSION",
    "ConfigurationError",
    "DesktopConfig",
    "LimitsConfig",
    "Settings",
    "UserSettingsWriteResult",
    "load_settings",
    "patch_user_settings",
    "read_user_settings",
    "upgrade_user_settings",
    "write_user_settings",
]
