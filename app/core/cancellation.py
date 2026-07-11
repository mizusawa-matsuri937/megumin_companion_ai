"""Cooperative cancellation primitives scoped to a dialogue turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.schemas.messages import prefixed_id


class TurnCancelledError(asyncio.CancelledError):
    """Raised when work notices that its owning turn was cancelled."""


@dataclass(slots=True)
class CancellationToken:
    turn_id: str
    token_id: str = field(default_factory=lambda: prefixed_id("cancel"))
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TurnCancelledError

    async def wait(self) -> None:
        await self._event.wait()

    async def wait_or_timeout(self, delay_seconds: float) -> bool:
        """Return True when cancelled, False when the delay elapsed."""

        if self.cancelled:
            return True
        if delay_seconds <= 0:
            await asyncio.sleep(0)
            return self.cancelled
        try:
            await asyncio.wait_for(self.wait(), timeout=delay_seconds)
        except TimeoutError:
            return False
        return True
