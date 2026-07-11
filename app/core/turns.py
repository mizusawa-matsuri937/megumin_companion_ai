"""Single entry point shared by text and transcribed voice messages."""

from __future__ import annotations

import logging

from app.config.logging import log_event
from app.schemas import TurnState, UserMessage


class TurnService:
    """Accept normalized messages without implementing the Day 5 dialogue pipeline."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    async def accept(self, message: UserMessage) -> TurnState:
        state = TurnState(
            session_id=message.session_id,
            source_message_id=message.message_id,
            input_mode=message.input_mode,
        )
        # Deliberately log identifiers and length, never message text or metadata.
        log_event(
            self._logger,
            logging.INFO,
            "turn.accepted",
            turn_id=state.turn_id,
            message_id=message.message_id,
            session_id=message.session_id,
            input_mode=message.input_mode.value,
            text_length=len(message.text),
        )
        return state
