"""Manual Day 3 redaction check using an explicitly fake credential."""

from __future__ import annotations

import logging

from app.config.logging import configure_logging, log_event
from app.config.settings import ConfigurationError, load_settings

FAKE_PREFIX = "fake-day3-"


def run() -> None:
    settings = load_settings()
    secret = settings.require_llm_api_key().get_secret_value()
    if not secret.startswith(FAKE_PREFIX):
        raise ConfigurationError(
            f"人工验收值必须以 {FAKE_PREFIX} 开头。请勿用真实或生产密钥执行此检查。"
        )

    logger = configure_logging(settings)
    log_event(
        logger,
        logging.INFO,
        "manual.redaction_check",
        fake_secret=secret,
        fake_email="day3-review@example.invalid",
        fake_phone="13812345678",
        fake_verification_code="验证码：654321",
        result="以上字段均应显示为 [REDACTED]",
    )
    for handler in logger.handlers:
        handler.flush()


if __name__ == "__main__":
    run()
