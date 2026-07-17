"""VTube Studio protocol client, secure token storage, and bounded event bridge."""

from app.clients.vts.bridge import (
    VTSAction,
    VTSBridge,
    VTSBridgeSnapshot,
    VTSBridgeState,
)
from app.clients.vts.client import (
    VTSAPIError,
    VTSClient,
    VTSConnectionError,
    VTSError,
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
    "VTSBridge",
    "VTSBridgeSnapshot",
    "VTSBridgeState",
    "VTSClient",
    "VTSConnectionError",
    "VTSError",
    "VTSProtocolError",
    "VTSRequestTimeout",
    "VTSToken",
    "VTSTurnEventSink",
    "read_legacy_plaintext_token",
]
