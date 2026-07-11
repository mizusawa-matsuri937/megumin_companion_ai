"""Thin HTTP and WebSocket protocol adapters for the dialogue service."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app import __version__
from app.core import TurnService
from app.schemas import PipelineEvent, TurnInterruptRequest, TurnState, UserMessage

router = APIRouter()


def _turn_service(connection: Request | WebSocket) -> TurnService:
    service = connection.app.state.turn_service
    if not isinstance(service, TurnService):
        raise RuntimeError("TurnService 尚未初始化")
    return service


def _safe_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        dict(error)
        for error in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "megumin-companion-ai", "version": __version__}


@router.post("/api/chat", response_model=TurnState)
async def chat(message: UserMessage, request: Request) -> TurnState:
    return await _turn_service(request).accept(message)


@router.post("/api/interrupt", response_model=TurnState | None)
async def interrupt(payload: TurnInterruptRequest, request: Request) -> TurnState | None:
    return await _turn_service(request).cancel(
        session_id=payload.session_id,
        turn_id=payload.turn_id,
    )


@router.get("/debug/state")
async def debug_state(request: Request) -> dict[str, Any]:
    return _turn_service(request).snapshot()


@router.websocket("/ws/echo")
async def websocket_echo(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if text := message.get("text"):
                await websocket.send_text(text)
            elif data := message.get("bytes"):
                await websocket.send_bytes(data)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return


@router.websocket("/ws/client")
async def websocket_client(websocket: WebSocket) -> None:
    await websocket.accept()
    service = _turn_service(websocket)
    events = service.subscribe("*")
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_events() -> None:
        while True:
            event: PipelineEvent = await events.get()
            try:
                await send(event.model_dump(mode="json"))
            finally:
                events.task_done()

    async def receive_commands() -> None:
        while True:
            envelope = await websocket.receive_json()
            if not isinstance(envelope, dict):
                await send(
                    {
                        "type": "error",
                        "error": {
                            "code": "unsupported_message_type",
                            "message": "消息必须是包含 type 和 payload 的对象。",
                        },
                    }
                )
                continue
            if envelope.get("type") == "turn.cancel":
                payload = envelope.get("payload")
                try:
                    request = TurnInterruptRequest.model_validate(payload or {})
                except ValidationError as exc:
                    await send(
                        {
                            "type": "error",
                            "error": {
                                "code": "invalid_turn_cancel",
                                "message": "turn.cancel payload 校验失败。",
                                "details": _safe_validation_errors(exc),
                            },
                        }
                    )
                    continue
                await service.cancel(session_id=request.session_id, turn_id=request.turn_id)
                continue
            if envelope.get("type") != "user.message":
                await send(
                    {
                        "type": "error",
                        "error": {
                            "code": "unsupported_message_type",
                            "message": "当前支持 user.message 和 turn.cancel。",
                        },
                    }
                )
                continue
            try:
                message = UserMessage.model_validate(envelope.get("payload"))
            except ValidationError as exc:
                await send(
                    {
                        "type": "error",
                        "error": {
                            "code": "invalid_user_message",
                            "message": "user.message payload 校验失败。",
                            "details": _safe_validation_errors(exc),
                        },
                    }
                )
                continue
            await service.accept(message)

    try:
        sender = asyncio.create_task(send_events())
        receiver = asyncio.create_task(receive_commands())
        _done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    finally:
        service.unsubscribe("*", events)
