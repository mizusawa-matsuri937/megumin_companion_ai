"""Structured logging and privacy redaction tests."""

import logging
from pathlib import Path

from app.config import load_settings
from app.config.logging import REDACTED, configure_logging, log_event


def test_structured_file_log_redacts_private_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    log_path = tmp_path / "application.jsonl"
    config_path.write_text(
        """
logging:
  console_enabled: false
  file_enabled: true
  file_path: ignored.jsonl
llm:
  provider: none
  api_key_env: TEST_LLM_KEY
""".strip(),
        encoding="utf-8",
    )
    fake_secret = "fake-day3-review-key-123"
    settings = load_settings(
        config_path,
        tmp_path / ".env",
        environ={"TEST_LLM_KEY": fake_secret},
    )
    logger = configure_logging(settings, file_path=log_path)

    log_event(
        logger,
        logging.INFO,
        "privacy.test",
        api_key="sk-1234567890abcdef",
        configured_secret=fake_secret,
        nested={
            "email": "reviewer@example.com",
            "phone": "13812345678",
            "verification": "验证码：654321",
            "identity": "11010519491231002X",
            "payment_card": "6222 0202 0123 4567",
        },
    )
    logger.error("authorization=Bearer this-token-must-disappear")
    for handler in logger.handlers:
        handler.flush()

    output = log_path.read_text(encoding="utf-8")
    for private_value in (
        "sk-1234567890abcdef",
        fake_secret,
        "reviewer@example.com",
        "13812345678",
        "654321",
        "11010519491231002X",
        "6222 0202 0123 4567",
        "this-token-must-disappear",
    ):
        assert private_value not in output
    assert output.count(REDACTED) >= 8
    assert '"event": "privacy.test"' in output
    assert '"level": "INFO"' in output
