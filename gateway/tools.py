"""Agent 的工具白名单；纯函数便于安全地从 checkpoint 恢复。"""

import json
import math

from gateway.events import GatewayError

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "计算两个有限数字的和；涉及加法时请使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    }
]


def execute_tool(name: str, raw_arguments: str) -> str:
    if name != "add":
        raise GatewayError("UNKNOWN_TOOL", "模型请求了未注册的工具")
    try:
        args = json.loads(raw_arguments)
        if not isinstance(args, dict) or set(args) != {"a", "b"}:
            raise ValueError
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in args.values()):
            raise ValueError
        result = args["a"] + args["b"]
        if not math.isfinite(result):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise GatewayError("INVALID_TOOL_ARGUMENTS", "add 只接受有限数字 a、b") from None
    return json.dumps({"result": result}, ensure_ascii=False, allow_nan=False)
