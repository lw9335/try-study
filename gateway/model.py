"""第 3 步：抽象 StreamingModel；Agent 仅依赖这个协议。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ModelProfile(BaseModel):
    id: str
    label: str
    family: Literal["Qwen", "DeepSeek"] = "Qwen"
    thinking_mode: Literal["none", "optional", "required"] = "none"
    tools: bool = True

    @computed_field
    @property
    def thinking(self) -> bool:
        return self.thinking_mode != "none"


MODEL_PROFILES = [
    ModelProfile(id="qwen-plus", label="Qwen Plus · 均衡", thinking_mode="optional"),
    ModelProfile(id="qwen3.8-flash", label="Qwen 3.8 Flash · 快速", thinking_mode="optional"),
    ModelProfile(id="qwen3.7-plus", label="Qwen 3.7 Plus · 通用", thinking_mode="optional"),
    ModelProfile(id="qwen-max", label="Qwen Max · 复杂任务"),
    ModelProfile(
        id="deepseek-v4.1-flash",
        label="DeepSeek V4.1 Flash · 快速",
        family="DeepSeek",
        thinking_mode="optional",
    ),
    ModelProfile(
        id="deepseek-v4-pro", label="DeepSeek V4 Pro · 推理", family="DeepSeek", thinking_mode="optional"
    ),
]


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=16000)
    model: str = "qwen-plus"
    tools: bool = False
    thinking: bool = False


class ResumeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkpoint_id: str | None = Field(default=None, alias="checkpointId", max_length=36)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_message(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class ModelChunk:
    type: Literal["delta", "reasoning", "usage", "finish"]
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: list[dict]
    tools: bool = False
    thinking: bool = False


class StreamingModel(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        """只返回统一增量。取消由 asyncio 任务向底层 HTTP 请求传播。"""
        ...
