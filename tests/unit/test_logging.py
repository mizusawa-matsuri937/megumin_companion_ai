"""Structured logging and privacy redaction tests."""

import json
import logging
from pathlib import Path

from app.config import load_settings
from app.config.logging import REDACTED, configure_logging, log_event
from app.paths import AppPaths


def test_structured_file_log_redacts_private_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
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
    paths = AppPaths.from_local_app_data(tmp_path / "Local")
    settings = load_settings(
        config_path,
        environ={"TEST_LLM_KEY": fake_secret},
        app_paths=paths,
    )
    logger = configure_logging(settings, file_path=paths.logs / "application.jsonl")

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

    log_path = next(paths.logs.glob("application.main.*.jsonl"))
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
    assert output.count(REDACTED) >= 1
    records = [json.loads(line) for line in output.splitlines()]
    assert records[0]["event"] == "privacy.test"
    assert records[0]["level"] == "INFO"
    assert records[0]["redaction_applied"] == REDACTED
    assert records[1]["event"] == "logging.unstructured_message"
