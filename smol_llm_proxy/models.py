"""Pydantic models for requests and responses."""

from typing import Any, Optional
from pydantic import BaseModel


# ── Admin request/response models ──────────────────────────────────────

class CreateServerRequest(BaseModel):
    name: str
    url: str
    api_key: str = ""


class UpdateServerRequest(BaseModel):
    url: Optional[str] = None
    api_key: Optional[str] = None
    active: Optional[bool] = None


class AssignModelRequest(BaseModel):
    model_name: str


class CreateKeyRequest(BaseModel):
    name: str


class ToggleKeyRequest(BaseModel):
    active: bool


class UsageFilter(BaseModel):
    key_id: Optional[int] = None
    server_id: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


# ── Proxy request/response models (OpenAI-compatible) ─────────────────

class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stream: bool = False
    stop: Optional[list[str]] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None

    model_config = {"extra": "allow"}


class CompletionRequest(BaseModel):
    model: str
    prompt: str | list[int] | list[list[int]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stream: bool = False
    stop: Optional[list[str]] = None

    model_config = {"extra": "allow"}


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: Optional[str] = None

    model_config = {"extra": "allow"}


# ── Proxy response models (OpenAI-compatible) ─────────────────────────

class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int
    message: Optional[ChatMessage] = None
    finish_reason: Optional[str] = None
    text: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class CompletionResponse(BaseModel):
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class EmbeddingData(BaseModel):
    index: int
    object: str = "embedding"
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    id: str
    object: str = "list"
    created: int
    model: str
    data: list[EmbeddingData]
    usage: Usage


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "system"


class ModelsListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
