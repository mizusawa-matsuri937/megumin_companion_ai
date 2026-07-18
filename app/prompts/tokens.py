"""Deterministic provider/model-aware conservative prompt token estimation."""

from __future__ import annotations


class ProviderTokenEstimator:
    """Estimate without network/tokenizer assets and never split Unicode text."""

    def __init__(self, *, provider: str, model: str) -> None:
        normalized_provider = provider.strip().casefold()
        normalized_model = model.strip().casefold()
        if normalized_provider in {"openai", "openai_compatible"} or normalized_model.startswith(
            ("gpt-", "o1", "o3", "o4")
        ):
            self.profile = "openai_cl100k_conservative"
            self._utf8_weighted = True
        else:
            self.profile = "unicode_conservative"
            self._utf8_weighted = False

    def estimate(self, value: str) -> int:
        """Return a deliberately conservative upper estimate for supported profiles."""

        if not value:
            return 0
        if self._utf8_weighted:
            return len(value.encode("utf-8"))
        return len(value)

    def estimate_message(self, value: str) -> int:
        # Four tokens cover role/framing separators in the supported chat envelopes.
        return self.estimate(value) + 4

    def truncate(self, value: str, token_limit: int) -> str:
        if token_limit <= 0:
            return ""
        if self.estimate(value) <= token_limit:
            return value
        marker = "…"
        marker_cost = self.estimate(marker)
        if marker_cost > token_limit:
            marker = "."
            marker_cost = self.estimate(marker)
        if marker_cost > token_limit:
            return ""
        low = 0
        high = len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if self.estimate(value[:middle]) + marker_cost <= token_limit:
                low = middle
            else:
                high = middle - 1
        return value[:low].rstrip() + marker
