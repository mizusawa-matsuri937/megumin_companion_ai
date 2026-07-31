"""Single-writer Avatar Runtime contracts and deterministic controllers."""

from app.avatar.controller import AvatarParameterController
from app.avatar.event_sink import AvatarTurnEventSink
from app.avatar.mapper import map_avatar_turn_plan
from app.avatar.models import (
    AvatarHealthSnapshot,
    AvatarParameterFrame,
    AvatarRuntimeState,
    AvatarTransitionClass,
    AvatarTurnPlan,
)
from app.avatar.red_eye import RedEyeCommand, RedEyeOwner, RedEyeOwnership
from app.avatar.runtime import AvatarRuntime
from app.emotion import FocusedVariant

__all__ = [
    "AvatarHealthSnapshot",
    "AvatarParameterController",
    "AvatarParameterFrame",
    "AvatarRuntimeState",
    "AvatarRuntime",
    "AvatarTransitionClass",
    "AvatarTurnPlan",
    "FocusedVariant",
    "AvatarTurnEventSink",
    "RedEyeCommand",
    "RedEyeOwner",
    "RedEyeOwnership",
    "map_avatar_turn_plan",
]
