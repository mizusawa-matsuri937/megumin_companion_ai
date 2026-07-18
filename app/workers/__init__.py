"""Bounded native-worker supervision primitives.

W12 intentionally provides only the reusable process and protocol substrate.
Audio, speech recognition, and perception handlers are owned by later PRs.
"""

from app.workers.access import ApprovedResourcePolicy, AuthorizedResource, ResourceReference
from app.workers.process import (
    ProcessAdapter,
    UnsupportedProcessAdapter,
    WindowsJobProcessAdapter,
    process_adapter_for_current_platform,
)
from app.workers.protocol import (
    HELPER_PROTOCOL_VERSION,
    MAX_HELPER_FRAME_BYTES,
    FrameDecoder,
    HelperMessage,
    ProtocolError,
    encode_message,
)
from app.workers.supervisor import (
    SupervisorConfig,
    SupervisorSnapshot,
    WorkerActualState,
    WorkerError,
    WorkerSupervisor,
)

__all__ = [
    "HELPER_PROTOCOL_VERSION",
    "MAX_HELPER_FRAME_BYTES",
    "ApprovedResourcePolicy",
    "AuthorizedResource",
    "FrameDecoder",
    "HelperMessage",
    "ProcessAdapter",
    "ProtocolError",
    "ResourceReference",
    "SupervisorConfig",
    "SupervisorSnapshot",
    "UnsupportedProcessAdapter",
    "WindowsJobProcessAdapter",
    "WorkerActualState",
    "WorkerError",
    "WorkerSupervisor",
    "encode_message",
    "process_adapter_for_current_platform",
]
