"""Map provider-neutral expression names to configured VTS hotkeys."""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_EXPRESSION_HOTKEYS: dict[str, str] = {
    "neutral": "hk_neutral",
    "happy": "hk_happy",
    "shy": "hk_shy_blush",
    "proud": "hk_proud",
    "angry_cute": "hk_pout",
    "worried": "hk_worried",
    "shocked": "hk_shocked",
    "sleepy": "hk_sleepy",
    "focused": "hk_focused",
    "explosion_excited": "hk_explosion_excited",
}


class ExpressionMapper:
    def __init__(self, hotkeys: Mapping[str, str] | None = None) -> None:
        configured = DEFAULT_EXPRESSION_HOTKEYS if hotkeys is None else hotkeys
        self._hotkeys = {
            expression.strip().lower(): hotkey.strip()
            for expression, hotkey in configured.items()
            if expression.strip() and hotkey.strip()
        }

    def hotkey_for(self, expression: str) -> str | None:
        return self._hotkeys.get(expression.strip().lower())

    def required_hotkey_ids(self) -> frozenset[str]:
        return frozenset(self._hotkeys.values())
