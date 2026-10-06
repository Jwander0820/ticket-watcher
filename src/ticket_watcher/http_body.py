"""Bound both wire bytes and decompressed bytes before buffering a response."""

import zlib

import httpx

ACCEPT_ENCODING = "gzip, deflate"
PUBLIC_BODY_LIMIT = 5_000_000
DISCORD_BODY_LIMIT = 65_536


class ResponseBodyError(ValueError):
    pass


async def read_body(response: httpx.Response, limit: int) -> bytes:
    # Preloaded responses (e.g. MockTransport) have already been decoded by HTTPX.
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise ResponseBodyError("回應超出資料大小限制")
        return response.content
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ResponseBodyError("回應壓縮格式不支援")
    decoder = (
        zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        if encoding != "identity"
        else None
    )
    body, received = bytearray(), 0
    try:
        async for chunk in response.aiter_raw():
            received += len(chunk)
            if received > limit:
                raise ResponseBodyError("回應超出資料大小限制")
            # HTTPX aiter_bytes() decompresses before yielding, so its chunk_size
            # alone cannot contain a compression bomb. Limit zlib's output here.
            decoded = decoder.decompress(chunk, limit - len(body) + 1) if decoder else chunk
            if len(body) + len(decoded) > limit:
                raise ResponseBodyError("回應超出資料大小限制")
            body.extend(decoded)
        if decoder and (not decoder.eof or decoder.unused_data):
            raise ResponseBodyError("回應壓縮資料不完整或含多餘內容")
    except zlib.error:
        raise ResponseBodyError("回應壓縮資料不正確") from None
    return bytes(body)
