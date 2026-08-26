import pytest
from pydantic import ValidationError

from gateway.schemas import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)


def test_valid_request_parses():
    req = ChatCompletionRequest.model_validate(
        {
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    assert req.model == "claude-sonnet"
    assert req.stream is False
    assert req.messages[0].role == "user"


def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {
                "model": "claude-sonnet",
                "messages": [{"role": "tool", "content": "hello"}],
            }
        )


def test_response_round_trips_through_json():
    response = ChatCompletionResponse(
        id="chatcmpl-123",
        created=1234567890,
        model="claude-sonnet",
        choices=[
            {
                "index": 0,
                "message": ChatMessage(role="assistant", content="hi there"),
                "finish_reason": "stop",
            }
        ],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    dumped = response.model_dump_json()
    restored = ChatCompletionResponse.model_validate_json(dumped)

    assert restored == response
    assert restored.object == "chat.completion"


def test_chunk_round_trips_through_json():
    chunk = ChatCompletionChunk(
        id="chatcmpl-123",
        created=1234567890,
        model="claude-sonnet",
        choices=[
            ChatCompletionChunkChoice(
                index=0,
                delta=ChatCompletionChunkDelta(role="assistant", content="hi"),
            )
        ],
    )

    restored = ChatCompletionChunk.model_validate_json(chunk.model_dump_json())

    assert restored == chunk
    assert restored.object == "chat.completion.chunk"
