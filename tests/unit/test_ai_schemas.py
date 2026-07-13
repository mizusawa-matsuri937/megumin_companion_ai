"""Provider-neutral AI contract validation tests."""

import base64

import pytest
from app.schemas import (
    ChatMessage,
    ChatRequest,
    ChatRole,
    ImageURLContent,
    TextContent,
)
from pydantic import ValidationError


def test_chat_request_accepts_text_and_local_image_data() -> None:
    encoded = base64.b64encode(b"synthetic-image").decode("ascii")
    request = ChatRequest(
        messages=[
            ChatMessage(
                role=ChatRole.user,
                content=[
                    TextContent(text="只描述已经脱敏的画面"),
                    ImageURLContent(url=f"data:image/png;base64,{encoded}"),
                ],
            )
        ],
        response_format="json_object",
    )

    assert request.messages[0].role is ChatRole.user
    assert len(request.messages[0].content) == 2


@pytest.mark.parametrize(
    "content",
    [" ", [], [{"type": "image_url", "url": "file:///private/screen.png"}]],
)
def test_chat_contract_rejects_empty_or_unsafe_content(content: object) -> None:
    with pytest.raises(ValidationError):
        ChatMessage.model_validate({"role": "user", "content": content})
