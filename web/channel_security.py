"""Channel-only transport: public-address pinning, bounded IO, no upstream echoes."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ACTIVE_REQUESTS = 8
_active_requests = 0


class ChannelError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def payload(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def redact(value, secret: str = ""):
    """Defense in depth; raw upstream bodies are never included in records."""
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(r"api.?key|authorization|password|secret|token", str(k), re.IGNORECASE)
                         else redact(v, secret)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if not isinstance(value, str):
        return value
    if secret:
        for token in {secret, quote(secret, safe="")}:
            value = value.replace(token, "[REDACTED]")
    value = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)\b(?:sk-|AIza)[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    return re.sub(r"(?i)(api[_-]?key|access[_-]?token|authorization|password|secret)(\s*[:=]\s*)[^\s&,;]+", r"\1\2[REDACTED]", value)


def normalize_url(raw: str) -> str:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048 or any(ord(c) < 33 for c in raw) or "\\" in raw:
        raise ChannelError("invalid_url", "地址无效")
    try:
        p = urlsplit(raw)
        if p.scheme not in {"https", "http"} or not p.hostname or p.username is not None or p.password is not None or "?" in raw or "#" in raw:
            raise ValueError
        port = p.port
        host = p.hostname.encode("idna").decode("ascii").lower()
        if "%" in host or host.endswith(".") or host in {"localhost", "localhost.localdomain"}:
            raise ValueError
        # Avoid ambiguous encoded separators / path traversal and credential-looking paths.
        if "%" in p.path or any(s in {".", ".."} for s in p.path.split("/")):
            raise ValueError
        hostpart = f"[{host}]" if ":" in host else host
        return urlunsplit((p.scheme, hostpart + (f":{port}" if port else ""), p.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise ChannelError("invalid_url", "仅支持不含凭据、查询参数或片段的 HTTP(S) 地址") from None


async def public_address(url: str) -> str:
    p = urlsplit(url)
    try:
        results = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM), 4)
    except (OSError, asyncio.TimeoutError):
        raise ChannelError("unreachable", "无法解析或连接上游地址", 502) from None
    addresses = list(dict.fromkeys(row[4][0] for row in results))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ChannelError("unsafe_address", "不允许访问内网、回环、链路本地或保留地址", 400)
    return addresses[0]


def status_error(status: int) -> ChannelError:
    code, message = {
        401: ("unauthorized", "上游拒绝凭据（401）"),
        403: ("forbidden", "上游禁止访问（403）"),
        429: ("rate_limited", "上游请求限流（429）"),
        404: ("not_found", "上游端点或模型不存在（404），不能确认凭据有效"),
    }.get(status, ("upstream_error", f"上游返回 HTTP {status}"))
    return ChannelError(code, message, status if status in {401, 403, 429, 404} else 502)


async def request_json(method: str, url: str, key: str, *, body=None, params=None, timeout=30):
    """Resolve once then connect to that public IP, retaining Host and TLS SNI.

    No environmental proxy, redirects, retries, response/body logging, or raw errors.
    The per-call client also prevents cross-host cookie/auth persistence.
    """
    global _active_requests
    if _active_requests >= MAX_ACTIVE_REQUESTS:
        raise ChannelError("busy", "并发请求已达上限，请稍后重试", 429)
    _active_requests += 1
    headers = {}
    try:
        url = normalize_url(url)
        p = urlsplit(url)
        address = await public_address(url)
        pinned = httpx.URL(url).copy_with(host=address)
        headers = {"Host": p.netloc, "Authorization": f"Bearer {key}", "x-api-key": key,
                   "x-goog-api-key": key, "anthropic-version": "2023-06-01", "Accept-Encoding": "identity"}
        async def perform():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout) as client:
                async with client.stream(method, pinned, headers=headers, json=body, params=params,
                                         extensions={"sni_hostname": p.hostname}) as response:
                    if 300 <= response.status_code < 400:
                        raise ChannelError("redirect_blocked", "上游重定向已阻止", 502)
                    if response.status_code >= 400:
                        raise status_error(response.status_code)
                    if response.status_code != 200:
                        raise ChannelError("invalid_response", "上游未返回有效 JSON 结果", 502)
                    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        raise ChannelError("unsupported_encoding", "上游压缩响应已阻止以限制解压资源", 502)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ChannelError("response_too_large", "上游响应超过大小限制", 502)
                        data.extend(chunk)
                    try:
                        return json.loads(data)
                    except (ValueError, UnicodeError, RecursionError):
                        raise ChannelError("invalid_response", "上游响应不是有效 JSON", 502) from None
        return await asyncio.wait_for(perform(), timeout)
    except ChannelError:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ChannelError("timeout", "上游请求超时", 504) from None
    except Exception:
        raise ChannelError("unreachable", "上游连接失败", 502) from None
    finally:
        headers.clear()
        key = ""
        _active_requests -= 1
