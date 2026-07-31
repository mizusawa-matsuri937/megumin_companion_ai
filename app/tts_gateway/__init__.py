"""Authenticated single-user GPT-SoVITS gateway boundaries."""

from app.tts_gateway.contracts import (
    GATEWAY_PROTOCOL_VERSION,
    VOICE_SLOTS,
    GatewayHealth,
    GatewayTTSRequest,
    VoiceSlot,
)
from app.tts_gateway.engine import (
    GatewayEngine,
    GatewayEngineError,
    GatewayEngineErrorCode,
    InferenceBackend,
)
from app.tts_gateway.manifest import (
    GATEWAY_SOURCE_COMMIT,
    GatewayManifest,
    GatewayManifestError,
    load_gateway_manifest,
)
from app.tts_gateway.package_import import (
    ImportedVoicePackage,
    VoicePackageError,
    VoicePackageErrorCode,
    safe_import_voice_package,
)
from app.tts_gateway.server import create_gateway_app

__all__ = [
    "GATEWAY_PROTOCOL_VERSION",
    "GATEWAY_SOURCE_COMMIT",
    "VOICE_SLOTS",
    "GatewayEngine",
    "GatewayEngineError",
    "GatewayEngineErrorCode",
    "GatewayHealth",
    "GatewayManifest",
    "GatewayManifestError",
    "GatewayTTSRequest",
    "InferenceBackend",
    "ImportedVoicePackage",
    "VoicePackageError",
    "VoicePackageErrorCode",
    "create_gateway_app",
    "load_gateway_manifest",
    "safe_import_voice_package",
    "VoiceSlot",
]
