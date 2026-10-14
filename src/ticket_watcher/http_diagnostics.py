"""Bounded HTTP rejection evidence, without raw bodies, URLs or arbitrary headers."""

import asyncio
import math
import re

import httpx

from .http_body import ResponseBodyError, read_body

BLOCKED_BODY_LIMIT = 16_384
BLOCKED_BODY_TIMEOUT = 1.0
ID_PARAMS = ("eventId", "sessionId", "ticketAreaId", "productId")
ENUM_FIELDS = {
    "endpoint": {"get", "getS3", "other"},
    "static_resource": {"event", "sessions", "products", "ticketAreas", "other"},
    "route": {"direct", "configured_proxy"},
    "http_version": {"HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/3"},
    "server": {"cloudfront", "cloudflare", "amazons3", "awselb/2.0", "other", "missing"},
    "content_type": {
        "text/html",
        "application/json",
        "application/xml",
        "text/xml",
        "text/plain",
        "other",
        "missing",
    },
    "content_encoding": {"identity", "gzip", "deflate", "other"},
    "x_cache": {
        "Error from cloudfront",
        "Hit from cloudfront",
        "Miss from cloudfront",
        "RefreshHit from cloudfront",
        "other",
        "missing",
    },
    "cf_mitigated": {"challenge"},
    "body_read": {
        "not_read",
        "complete",
        "timeout",
        "network_error",
        "unavailable_or_too_large",
        "cancelled",
    },
}
RESPONSE_IDS = {
    "cf-ray": r"[a-fA-F0-9]{16}(?:-[A-Z]{3})?",
    "x-amz-cf-id": r"[A-Za-z0-9_+/=-]{8,200}",
    "x-amz-cf-pop": r"[A-Z]{3}[0-9]{1,3}-[A-Z][0-9]{1,3}",
    "x-amz-request-id": r"[A-Za-z0-9]{8,100}",
    "x-amzn-requestid": r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}",
}
BODY_SIGNALS = {
    "cloudfront": (b"cloudfront",),
    "request_blocked": (b"request blocked",),
    "access_denied": (b"access denied", b"accessdenied"),
    "cloudflare_challenge": (b"cf-chl-", b"challenge-platform", b"verify you are human"),
    "captcha": (b"captcha",),
    "rate_limited": (b"too many requests", b"rate limit"),
    "geo_restriction": (b"geo restriction", b"geographic restriction", b"blocked in your country"),
}


def safe_http_diagnostic(value) -> dict:
    """Enforce the same allowlist at both result and durable-log boundaries."""
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key, choices in ENUM_FIELDS.items():
        if isinstance(value.get(key), str) and value[key] in choices:
            safe[key] = value[key]
    if type(value.get("status")) is int and value["status"] in (401, 403):
        safe["status"] = value["status"]
    for key, low, high in (
        ("request_number", 1, 1_000_000),
        ("body_bytes", 0, BLOCKED_BODY_LIMIT),
    ):
        number = value.get(key)
        if type(number) is int and low <= number <= high:
            safe[key] = number
    ua = value.get("user_agent")
    if (
        isinstance(ua, str)
        and len(ua) <= 80
        and (
            ua in {"other", "missing"} or re.fullmatch(r"TicketWatcher/[0-9]+\.[0-9]+\.[0-9]+", ua)
        )
    ):
        safe["user_agent"] = ua
    delay = value.get("retry_after_seconds")
    if type(delay) in (int, float) and 0 <= delay <= 1e12 and math.isfinite(delay):
        safe["retry_after_seconds"] = delay
    if isinstance(value.get("requested_ids"), dict):
        safe["requested_ids"] = {
            key: count
            for key, count in value["requested_ids"].items()
            if key in ID_PARAMS and type(count) is int and 0 <= count <= 1000
        }
    if isinstance(value.get("response_ids"), dict):
        safe["response_ids"] = {
            key: ident
            for key, ident in value["response_ids"].items()
            if key in RESPONSE_IDS
            and isinstance(ident, str)
            and len(ident) <= 200
            and re.fullmatch(RESPONSE_IDS[key], ident)
        }
    if isinstance(value.get("body_signals"), (list, tuple)):
        safe["body_signals"] = [
            signal for signal in BODY_SIGNALS if signal in value["body_signals"]
        ]
    return safe


def response_diagnostic(response, params, *, proxy_configured, request_number, retry_after):
    """Record observed hints only; a CDN marker does not prove an IP or rate ban."""
    url = response.request.url
    endpoint = url.path.rsplit("/", 1)[-1]
    if url.host != "apis.ticketplus.com.tw" or url.path not in {
        "/config/api/v1/get",
        "/config/api/v1/getS3",
    }:
        endpoint = "other"
    resource = str(params.get("path", "")).rsplit("/", 1)[-1].removesuffix(".json")
    headers = response.headers
    ua = response.request.headers.get("user-agent", "missing")
    diagnostic = {
        "status": response.status_code,
        "endpoint": endpoint,
        "request_number": request_number,
        "route": "configured_proxy"
        if (proxy_configured and url.scheme == "https" and url.host == "apis.ticketplus.com.tw")
        else "direct",
        "http_version": response.http_version,
        "user_agent": ua if re.fullmatch(r"TicketWatcher/[0-9]+\.[0-9]+\.[0-9]+", ua) else "other",
        "requested_ids": {
            key: len(str(params[key]).split(",")) for key in ID_PARAMS if params.get(key)
        },
        "response_ids": {key: headers[key] for key in RESPONSE_IDS if key in headers},
        "body_read": "not_read",
        "body_signals": [],
    }
    if endpoint == "getS3":
        diagnostic["static_resource"] = (
            resource if resource in ENUM_FIELDS["static_resource"] else "other"
        )
    for key, header, default in (
        ("server", "server", "missing"),
        ("content_type", "content-type", "missing"),
        ("content_encoding", "content-encoding", "identity"),
        ("x_cache", "x-cache", "missing"),
    ):
        value = headers.get(header, default).strip()
        if key != "x_cache":
            value = value.split(";", 1)[0].lower()
        diagnostic[key] = value if value in ENUM_FIELDS[key] else "other"
    if headers.get("cf-mitigated", "").lower() == "challenge":
        diagnostic["cf_mitigated"] = "challenge"
    if "retry-after" in headers:
        diagnostic["retry_after_seconds"] = retry_after
    return safe_http_diagnostic(diagnostic)


async def read_blocked_diagnostic(response, diagnostic):
    # Read only this already-issued response, never probe or retry. Bound both
    # wire and decompressed bytes, and avoid delaying platform pause indefinitely.
    try:
        async with asyncio.timeout(BLOCKED_BODY_TIMEOUT):
            body = await read_body(response, BLOCKED_BODY_LIMIT)
    except TimeoutError:
        diagnostic["body_read"] = "timeout"
    except httpx.HTTPError:
        diagnostic["body_read"] = "network_error"
    except ResponseBodyError:
        diagnostic["body_read"] = "unavailable_or_too_large"
    else:
        diagnostic["body_read"] = "complete"
        diagnostic["body_bytes"] = len(body)
        lower = body.lower()
        diagnostic["body_signals"] = [
            name
            for name, markers in BODY_SIGNALS.items()
            if any(marker in lower for marker in markers)
        ]
