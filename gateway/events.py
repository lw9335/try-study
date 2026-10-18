"""第 1 步：定义与模型厂商无关的统一 RunEvent / Harness Event。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    MODEL_STARTED = "model.started"
    MODEL_DELTA = "model.delta"
    MODEL_REASONING = "model.reasoning"
    MODEL_USAGE = "model.usage"
    MODEL_FINISHED = "model.finished"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    CHECKPOINT_SAVED = "checkpoint.saved"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FAILED = "run.failed"


TERMINAL_EVENTS = {EventType.RUN_COMPLETED, EventType.RUN_CANCELLED, EventType.RUN_FAILED}


class RunEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    version: Literal[1] = 1
    run_id: str = Field(serialization_alias="runId")
    trace_id: str = Field(serialization_alias="traceId")
    seq: int
    type: EventType
    timestamp: str = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class GatewayError(Exception):
    """公开错误协议；message 必须经过脱敏，不携带厂商原始响应或密钥。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}
