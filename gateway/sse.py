"""第 2 步：SSE 编解码；支持任意网络分片、UTF-8、CR/LF/CRLF 和多行 data。"""

import codecs
import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEFrame:
    data: str
    event: str | None = None
    id: str | None = None


def encode_sse(frame: SSEFrame) -> bytes:
    def safe(value: str) -> str:
        return re.sub(r"[\r\n\x00]", "", value)

    lines = []
    if frame.id is not None:
        lines.append(f"id: {safe(frame.id)}")
    if frame.event:
        lines.append(f"event: {safe(frame.event)}")
    lines.extend(f"data: {line}" for line in re.split(r"\r\n|\r|\n", frame.data))
    return ("\n".join(lines) + "\n\n").encode()


async def decode_sse(source: AsyncIterable[bytes], max_frame: int = 1_048_576) -> AsyncIterator[SSEFrame]:
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    buffer = ""
    data: list[str] = []
    event = None
    event_id = None
    frame_size = 0

    def consume(line: str) -> SSEFrame | None:
        nonlocal event, event_id, frame_size
        frame_size += len(line)
        if frame_size > max_frame:
            raise ValueError("SSE frame too large")
        if not line:
            result = SSEFrame("\n".join(data), event, event_id) if data else None
            data.clear()
            event = None
            frame_size = 0
            return result
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "event":
            event = value
        elif field == "id" and "\x00" not in value:
            event_id = value
        return None

    async for chunk in source:
        buffer += decoder.decode(chunk)
        while match := re.search(r"[\r\n]", buffer):
            pos = match.start()
            if buffer[pos] == "\r" and pos == len(buffer) - 1:
                break  # CRLF 可能恰好被切成两个网络包。
            width = 2 if buffer[pos : pos + 2] == "\r\n" else 1
            frame = consume(buffer[:pos])
            buffer = buffer[pos + width :]
            if frame is not None:
                yield frame
        if len(buffer) + frame_size > max_frame:
            raise ValueError("SSE frame too large")
    buffer += decoder.decode(b"", final=True)
    if buffer.endswith("\r"):
        frame = consume(buffer[:-1])
        if frame is not None:
            yield frame
    # EOF 不能提交缺少空行的半帧；上游缺失 DONE 由 Adapter 判定失败。
