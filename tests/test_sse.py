import pytest

from gateway.sse import SSEFrame, decode_sse, encode_sse


async def chunks(data: bytes, size: int = 1):
    for index in range(0, len(data), size):
        yield data[index : index + size]


@pytest.mark.parametrize("size", [1, 2, 3, 17, 1000])
async def test_arbitrary_utf8_boundaries_and_roundtrip(size):
    frame = SSEFrame("你好\n第二行 😀", "model.delta", "42")
    decoded = [frame async for frame in decode_sse(chunks(encode_sse(frame), size))]
    assert decoded == [frame]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_line_endings_comments_and_sticky_id(newline):
    source = newline.join(
        ["\ufeff: ping", "id: 3", "event: hello", "data: a", "data: b", "", "data: c", "", ""]
    )
    frames = [frame async for frame in decode_sse(chunks(source.encode()))]
    assert frames == [SSEFrame("a\nb", "hello", "3"), SSEFrame("c", None, "3")]


async def test_partial_frame_at_eof_is_not_dispatched():
    assert [x async for x in decode_sse(chunks(b"data: truncated\n"))] == []


async def test_empty_data_is_a_valid_event():
    assert [x async for x in decode_sse(chunks(b"data:\n\n"))] == [SSEFrame("")]


async def test_oversized_frame_and_invalid_utf8_fail():
    with pytest.raises(ValueError, match="too large"):
        _ = [x async for x in decode_sse(chunks(b"data: abcdef\n\n"), max_frame=5)]
    with pytest.raises(UnicodeDecodeError):
        _ = [x async for x in decode_sse(chunks(b"data: \xff\n\n"))]
