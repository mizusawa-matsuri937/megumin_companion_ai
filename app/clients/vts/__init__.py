"""VTube Studio protocol client, secure token storage, and bounded event bridge."""

from app.clients.vts.bridge import (
    VTSAction,
    VTSBridge,
    VTSBridgeSnapshot,
    VTSBridgeState,
)
from app.clients.vts.client import (
    VTSAPIError,
    VTSAuthenticationError,
    VTSClient,
    VTSConfigurationError,
    VTSConnectionError,
    VTSError,
    VTSEvent,
    VTSHotkeyTriggeredEvent,
    VTSModelLoadedEvent,
    VTSParameterCapability,
    VTSPreflight,
    VTSProtocolError,
    VTSRequestTimeout,
)
from app.clients.vts.event_sink import VTSTurnEventSink
from app.clients.vts.expression_mapper import ExpressionMapper
from app.clients.vts.token_store import (
    DPAPITokenStore,
    TokenStore,
    VTSToken,
    read_legacy_plaintext_token,
)

__all__ = [
    "ExpressionMapper",
    "DPAPITokenStore",
    "TokenStore",
    "VTSAPIError",
    "VTSAction",
    "VTSAuthenticationError",
    "VTSBridge",
    "VTSBridgeSnapshot",
    "VTSBridgeState",
    "VTSClient",
    "VTSConfigurationError",
    "VTSConnectionError",
    "VTSEvent",
    "VTSError",
    "VTSHotkeyTriggeredEvent",
    "VTSModelLoadedEvent",
    "VTSParameterCapability",
    "VTSPreflight",
    "VTSProtocolError",
    "VTSRequestTimeout",
    "VTSToken",
    "VTSTurnEventSink",
    "read_legacy_plaintext_token",
]
