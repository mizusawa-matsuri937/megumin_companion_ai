"""Authenticated HTTP and WebSocket adapters for the explicit development API."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app import __version__
from app.api.protocol import (
    DevAPIProtocolError,
    ensure_authorized_identity,
    ensure_authorized_session,
    error_envelope,
    event_envelope,
    parse_websocket_command,
    reset_envelope,
    validate_user_message,
)
from app.api.security import (
    DevAPIPrincipal,
    DevAPIScope,
    DevAPISecurity,
    DevAPISecurityError,
    principal_from_scope,
    security_from_scope,
)
from app.core import TurnService
from app.core.idempotency import (
    IdempotencyAccessError,
    IdempotencyConflictError,
    IdempotencyUnavailableError,
)
from app.core.turns import (
    EventSubscription,
    SlowConsumerError,
    SubscriptionClosedError,
    TurnAccessError,
)
from app.memory import (
    ConfirmationNotFoundError,
    CredentialRejectedError,
    FeatureDisabledError,
)
from app.memory.runtime import MemoryRuntime
from app.schemas import (
    FeatureName,
    FeaturePatchRequest,
    HistoryClearRequest,
    MemoryConfirmRequest,
    MemoryUpdateRequest,
    PipelineEvent,
    SessionReset,
    SessionResumeRequest,
    TurnInterruptRequest,
    TurnState,
    UserMessage,
)

router = APIRouter()


def _turn_service(connection: Request | WebSocket) -> TurnService:
    service = connection.app.state.turn_service
    if not isinstance(service, TurnService):
        raise RuntimeError("TurnService 尚未初始化")
    return service


def _memory_runtime(request: Request) -> MemoryRuntime:
    runtime = getattr(request.app.state, "memory_runtime", None)
    if not isinstance(runtime, MemoryRuntime):
        raise HTTPException(status_code=503, detail="private_state_runtime_disabled")
    return runtime


def _request_principal(request: Request, required: DevAPIScope) -> DevAPIPrincipal:
    principal = principal_from_scope(request.scope)
    security = security_from_scope(request.scope)
    try:
        security.require_scope(principal, required)
    except DevAPISecurityError as exc:
        security.log_rejection(exc, transport="http", principal=principal)
        raise HTTPException(status_code=exc.http_status, detail=exc.code) from exc
    return principal


def _chat_principal(request: Request) -> DevAPIPrincipal:
    return _request_principal(request, DevAPIScope.chat)


def _admin_principal(request: Request) -> DevAPIPrincipal:
    return _request_principal(request, DevAPIScope.admin)


ChatPrincipal = Annotated[DevAPIPrincipal, Depends(_chat_principal)]
AdminPrincipal = Annotated[DevAPIPrincipal, Depends(_admin_principal)]
BoundedPathID = Annotated[str, Path(min_length=1, max_length=128)]


def _validate_http_message(
    message: UserMessage,
    request: Request,
    principal: DevAPIPrincipal,
) -> None:
    security = security_from_scope(request.scope)
    try:
        validate_user_message(message, principal, security.config)
    except DevAPIProtocolError as exc:
        status = 403 if exc.code == "session_forbidden" else 422
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.get("/health")
async def health(request: Request, _principal: ChatPrincipal) -> dict[str, str | bool]:
    ready = isinstance(getattr(request.app.state, "turn_service", None), TurnService)
    if not ready:
        raise HTTPException(status_code=503, detail="service_not_ready")
    return {
        "status": "ready",
        "service": "megumin-companion-ai",
        "version": __version__,
        "private_state": isinstance(
            getattr(request.app.state, "memory_runtime", None), MemoryRuntime
        ),
    }


@router.post("/api/chat", response_model=TurnState)
async def chat(message: UserMessage, request: Request, principal: ChatPrincipal) -> TurnState:
    _validate_http_message(message, request, principal)
    try:
        return await _turn_service(request).accept(message, client_id=principal.client_id)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except IdempotencyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/api/interrupt", response_model=TurnState | None)
async def interrupt(
    payload: TurnInterruptRequest,
    request: Request,
    principal: ChatPrincipal,
) -> TurnState | None:
    try:
        ensure_authorized_session(principal, payload.session_id)
    except DevAPIProtocolError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    try:
        return await _turn_service(request).cancel(
            client_id=principal.client_id,
            session_id=payload.session_id,
            turn_id=payload.turn_id,
        )
    except (IdempotencyAccessError, TurnAccessError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IdempotencyUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/debug/state")
async def debug_state(request: Request, _principal: AdminPrincipal) -> dict[str, Any]:
    return _turn_service(request).snapshot()


@router.get("/api/features")
async def list_features(request: Request, _principal: AdminPrincipal) -> list[dict[str, Any]]:
    states = await _memory_runtime(request).list_features()
    return [state.model_dump(mode="json") for state in states]


@router.patch("/api/features/{feature}")
async def patch_feature(
    feature: FeatureName,
    payload: FeaturePatchRequest,
    request: Request,
    _principal: AdminPrincipal,
) -> dict[str, Any]:
    state = await _memory_runtime(request).set_feature(feature, payload.enabled)
    return state.model_dump(mode="json")


@router.get("/api/memory")
async def list_memories(
    request: Request,
    _principal: AdminPrincipal,
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    items = await _memory_runtime(request).list_memories(
        user_id=user_id,
        include_superseded=include_superseded,
    )
    return [item.model_dump(mode="json") for item in items]


@router.get("/api/memory/search")
async def search_memories(
    request: Request,
    _principal: AdminPrincipal,
    query: str = Query(min_length=1, max_length=5_000),
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
    limit: int = Query(default=10, ge=1, le=100),
) -> list[dict[str, Any]]:
    items = await _memory_runtime(request).search_memories(
        user_id=user_id,
        query=query,
        limit=limit,
    )
    return [item.model_dump(mode="json") for item in items]


@router.get("/api/memory/confirmations")
async def list_memory_confirmations(
    request: Request,
    _principal: AdminPrincipal,
) -> list[dict[str, Any]]:
    pending = await _memory_runtime(request).pending_confirmations()
    return [item.model_dump(mode="json") for item in pending]


@router.post("/api/memory/confirm/{confirmation_id}")
async def confirm_memory(
    confirmation_id: BoundedPathID,
    payload: MemoryConfirmRequest,
    request: Request,
    _principal: AdminPrincipal,
) -> dict[str, Any]:
    try:
        item = await _memory_runtime(request).confirm_memory(
            confirmation_id,
            approved=payload.approved,
        )
    except ConfirmationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="confirmation_not_found") from exc
    except FeatureDisabledError as exc:
        raise HTTPException(status_code=409, detail="long_term_memory_disabled") from exc
    return {"approved": payload.approved, "memory": item.model_dump(mode="json") if item else None}


@router.get("/api/memory/export")
async def export_memories(
    request: Request,
    _principal: AdminPrincipal,
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
) -> dict[str, Any]:
    return await _memory_runtime(request).export(user_id=user_id)


@router.delete("/api/memory")
async def clear_memories(
    request: Request,
    _principal: AdminPrincipal,
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
) -> dict[str, int]:
    deleted = await _memory_runtime(request).clear_memories(user_id=user_id)
    return {"deleted": deleted}


@router.patch("/api/memory/{memory_id}")
async def update_memory(
    memory_id: BoundedPathID,
    payload: MemoryUpdateRequest,
    request: Request,
    _principal: AdminPrincipal,
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
) -> dict[str, Any]:
    try:
        item = await _memory_runtime(request).update_memory(
            memory_id,
            user_id=user_id,
            content=payload.content,
        )
    except CredentialRejectedError as exc:
        raise HTTPException(status_code=422, detail="credential_content_forbidden") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    return item.model_dump(mode="json")


@router.delete("/api/memory/{memory_id}")
async def delete_memory(
    memory_id: BoundedPathID,
    request: Request,
    _principal: AdminPrincipal,
    user_id: str = Query(default="local_user", min_length=1, max_length=128),
) -> dict[str, bool]:
    deleted = await _memory_runtime(request).delete_memory(memory_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="memory_not_found")
    return {"deleted": True}


@router.post("/api/history/clear")
async def clear_history(
    payload: HistoryClearRequest,
    request: Request,
    principal: AdminPrincipal,
) -> dict[str, int]:
    if payload.session_id is not None:
        try:
            ensure_authorized_session(principal, payload.session_id)
        except DevAPIProtocolError as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
    deleted = await _memory_runtime(request).clear_history(
        user_id=payload.user_id,
        session_id=payload.session_id,
    )
    return {"deleted": deleted}


async def _admit_websocket(
    websocket: WebSocket,
    required_scope: DevAPIScope,
) -> tuple[DevAPIPrincipal, DevAPISecurity] | None:
    principal = principal_from_scope(websocket.scope)
    security = security_from_scope(websocket.scope)
    try:
        security.require_scope(principal, required_scope)
        security.begin_websocket()
    except DevAPISecurityError as exc:
        security.log_rejection(exc, transport="websocket", principal=principal)
        await websocket.close(code=exc.websocket_code, reason=exc.code)
        return None
    try:
        await websocket.accept()
    except BaseException:
        security.end_websocket()
        raise
    return principal, security


async def _receive_bounded_frame(
    websocket: WebSocket,
    principal: DevAPIPrincipal,
    security: DevAPISecurity,
) -> str | bytes | None:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    try:
        security.consume_websocket_message()
    except DevAPISecurityError as exc:
        security.log_rejection(exc, transport="websocket", principal=principal)
        await websocket.close(code=exc.websocket_code, reason=exc.code)
        return None
    text = message.get("text")
    data = message.get("bytes")
    payload: str | bytes
    if isinstance(text, str):
        payload = text
        size = len(text.encode("utf-8"))
    elif isinstance(data, bytes):
        payload = data
        size = len(data)
    else:
        await websocket.close(code=1003, reason="unsupported_frame_type")
        return None
    if size > security.config.max_websocket_frame_bytes:
        error = DevAPISecurityError(
            "websocket_frame_too_large",
            reason="websocket_frame_limit",
            http_status=413,
            websocket_code=1009,
        )
        security.log_rejection(error, transport="websocket", principal=principal)
        await websocket.close(code=error.websocket_code, reason=error.code)
        return None
    return payload


async def _close_websocket_when_token_expires(
    websocket: WebSocket,
    principal: DevAPIPrincipal,
    security: DevAPISecurity,
) -> None:
    await asyncio.sleep(security.token_seconds_remaining())
    try:
        security.ensure_token_fresh()
    except DevAPISecurityError as exc:
        security.log_rejection(exc, transport="websocket", principal=principal)
        await websocket.close(code=1008, reason=exc.code)


@router.websocket("/ws/echo")
async def websocket_echo(websocket: WebSocket) -> None:
    admitted = await _admit_websocket(websocket, DevAPIScope.admin)
    if admitted is None:
        return
    principal, security = admitted
    try:
        while True:
            payload = await _receive_bounded_frame(websocket, principal, security)
            if payload is None:
                return
            if isinstance(payload, str):
                await websocket.send_text(payload)
            else:
                await websocket.send_bytes(payload)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    finally:
        security.end_websocket()


@router.websocket("/ws/client")
async def websocket_client(websocket: WebSocket) -> None:
    admitted = await _admit_websocket(websocket, DevAPIScope.chat)
    if admitted is None:
        return
    principal, security = admitted
    service = _turn_service(websocket)
    events: EventSubscription | None = None
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > security.config.max_websocket_frame_bytes:
                error = DevAPISecurityError(
                    "websocket_frame_too_large",
                    reason="outgoing_websocket_frame_limit",
                    http_status=500,
                    websocket_code=1009,
                )
                security.log_rejection(error, transport="websocket", principal=principal)
                await websocket.close(code=error.websocket_code, reason=error.code)
                raise WebSocketDisconnect(code=error.websocket_code, reason=error.code)
            await websocket.send_text(serialized)

    async def send_error(code: str) -> None:
        await send(error_envelope(session_id=principal.session_id, code=code))

    async def send_events() -> None:
        subscription = events
        assert subscription is not None
        try:
            while True:
                item = await subscription.get()
                try:
                    if isinstance(item, PipelineEvent):
                        await send(event_envelope(item))
                    else:
                        assert isinstance(item, SessionReset)
                        await send(reset_envelope(item))
                finally:
                    subscription.task_done()
        except SlowConsumerError:
            await websocket.close(code=1013, reason="slow_consumer")
        except SubscriptionClosedError:
            return

    async def receive_commands() -> None:
        while True:
            frame = await _receive_bounded_frame(websocket, principal, security)
            if frame is None:
                return
            if isinstance(frame, bytes):
                await websocket.close(code=1003, reason="binary_commands_forbidden")
                return
            try:
                envelope = parse_websocket_command(frame)
            except DevAPIProtocolError as exc:
                await send_error(exc.code)
                continue
            try:
                ensure_authorized_identity(
                    principal,
                    client_id=envelope.client_id,
                    session_id=envelope.session_id,
                )
            except DevAPIProtocolError as exc:
                await send_error(exc.code)
                await websocket.close(code=1008, reason=exc.code)
                return
            if envelope.type == "turn.cancel":
                try:
                    request = TurnInterruptRequest.model_validate(envelope.payload)
                    ensure_authorized_session(principal, request.session_id)
                except ValidationError:
                    await send_error("invalid_turn_cancel")
                    continue
                except DevAPIProtocolError as exc:
                    await send_error(exc.code)
                    await websocket.close(code=1008, reason=exc.code)
                    return
                try:
                    await service.cancel(
                        client_id=principal.client_id,
                        session_id=request.session_id,
                        turn_id=request.turn_id,
                    )
                except (IdempotencyAccessError, TurnAccessError) as exc:
                    await send_error(str(exc))
                except IdempotencyUnavailableError as exc:
                    await send_error(str(exc))
                continue
            if envelope.type != "user.message":
                await send_error("unsupported_message_type")
                continue
            try:
                message = UserMessage.model_validate(envelope.payload)
                validate_user_message(message, principal, security.config)
            except ValidationError:
                await send_error("invalid_user_message")
                continue
            except DevAPIProtocolError as exc:
                await send_error(exc.code)
                if exc.code == "session_forbidden":
                    await websocket.close(code=1008, reason=exc.code)
                    return
                continue
            try:
                await service.accept(message, client_id=principal.client_id)
            except (
                IdempotencyAccessError,
                IdempotencyConflictError,
                IdempotencyUnavailableError,
            ) as exc:
                await send_error(str(exc))

    try:
        while events is None:
            frame = await _receive_bounded_frame(websocket, principal, security)
            if frame is None:
                return
            if isinstance(frame, bytes):
                await websocket.close(code=1003, reason="binary_commands_forbidden")
                return
            try:
                envelope = parse_websocket_command(frame)
                ensure_authorized_identity(
                    principal,
                    client_id=envelope.client_id,
                    session_id=envelope.session_id,
                )
            except DevAPIProtocolError as exc:
                await send_error(exc.code)
                if exc.code in {"client_identity_forbidden", "session_forbidden"}:
                    await websocket.close(code=1008, reason=exc.code)
                    return
                continue
            if envelope.type != "session.resume":
                await send_error("resume_required")
                continue
            try:
                resume = SessionResumeRequest.model_validate(envelope.payload)
            except ValidationError:
                await send_error("invalid_session_resume")
                continue
            try:
                events = await service.subscribe(
                    principal.session_id,
                    client_id=principal.client_id,
                    last_seq=resume.last_seq,
                )
            except IdempotencyAccessError as exc:
                await send_error(str(exc))
                await websocket.close(code=1008, reason=str(exc))
                return
            except IdempotencyUnavailableError as exc:
                await send_error(str(exc))
                await websocket.close(code=1013, reason=str(exc))
                return

        sender = asyncio.create_task(send_events())
        receiver = asyncio.create_task(receive_commands())
        expiry = asyncio.create_task(
            _close_websocket_when_token_expires(websocket, principal, security)
        )
        _done, pending = await asyncio.wait(
            {sender, receiver, expiry},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(sender, receiver, expiry, return_exceptions=True)
    except SlowConsumerError:
        await websocket.close(code=1013, reason="slow_consumer")
        return
    except SubscriptionClosedError:
        return
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    finally:
        if events is not None:
            service.unsubscribe(events)
        security.end_websocket()
