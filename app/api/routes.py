"""Day 4 HTTP and WebSocket routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app import __version__
from app.core import TurnService
from app.schemas import TurnState, UserMessage

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
    except WebSocketDisconnect:
        return


@router.websocket("/ws/client")
async def websocket_client(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            envelope = await websocket.receive_json()
            if not isinstance(envelope, dict) or envelope.get("type") != "user.message":
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": {
                            "code": "unsupported_message_type",
                            "message": "当前只支持 user.message。",
                        },
                    }
                )
                continue
            try:
                message = UserMessage.model_validate(envelope.get("payload"))
            except ValidationError as exc:
                await websocket.send_json(
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
            state = await _turn_service(websocket).accept(message)
            await websocket.send_json(
                {
                    "type": "turn.accepted",
                    "payload": state.model_dump(mode="json"),
                }
            )
    except WebSocketDisconnect:
        return
