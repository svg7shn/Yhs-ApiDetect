"""Mount with app.include_router(router); no legacy detector dependencies."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates

from . import channel_jobs, channel_store
from .channel_probe import FAMILY_LABELS, MODEL_RE, SOURCE_MAP, SOURCES, list_models
from .channel_security import ChannelError, normalize_url, public_address

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_BODY = 1024 * 1024
MAX_TARGETS = 1000  # Same bounded capacity as the fetched model list.


def failure(exc):
    return JSONResponse(exc.payload(), status_code=exc.status, headers={"Cache-Control": "no-store"})


async def input_data(request):
    # Parse ourselves: FastAPI validation error input must never echo an API key.
    raw = bytearray()
    try:
        async def receive():
            async for part in request.stream():
                if len(raw) + len(part) > MAX_BODY:
                    raise ChannelError("body_too_large", "请求体超过限制", 413)
                raw.extend(part)
        await asyncio.wait_for(receive(), 10)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ChannelError:
        raise
    except Exception:
        raise ChannelError("invalid_request", "请求必须为有效 JSON 对象", 400) from None
    finally:
        raw.clear()


def credentials(data):
    key = data.get("api_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ChannelError("invalid_key", "API Key 为空、过长或含不支持字符", 400)
    base = normalize_url(data.get("base_url"))
    if key in base:
        raise ChannelError("invalid_url", "凭据不能放入地址", 400)
    return base, key


def filters(request):
    try:
        days = int(request.query_params.get("days", "7"))
        q = request.query_params.get("q", "")
        if not 1 <= days <= 365 or len(q) > 2048:
            raise ValueError
        return days, q
    except ValueError:
        raise ChannelError("invalid_filter", "days 须为 1–365，地址筛选长度不超过 2048", 400) from None


@router.get("/channel")
async def channel_page(request: Request):
    return templates.TemplateResponse(request, "channel.html", {"sources": SOURCES, "family_labels": FAMILY_LABELS})


@router.get("/api/channel/sources")
async def sources():
    return {"sources": SOURCES, "family_labels": FAMILY_LABELS}


@router.post("/api/channel/models")
async def models(request: Request):
    data, key = {}, ""
    try:
        data = await input_data(request)
        base, key = credentials(data)
        return JSONResponse(await list_models(base, key), headers={"Cache-Control": "no-store"})
    except ChannelError as exc:
        return failure(exc)
    except Exception:
        return failure(ChannelError("internal_error", "模型列表请求失败（详情不回显）", 500))
    finally:
        data.clear()
        key = ""


@router.post("/api/channel/run")
async def run(request: Request):
    data, key = {}, ""
    try:
        data = await input_data(request)
        base, key = credentials(data)
        targets = data.get("targets")
        timeout = data.get("timeout", 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise ChannelError("invalid_timeout", "timeout 须为 1–60 秒", 400)
        if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
            raise ChannelError("invalid_targets", f"每次任务须选择 1–{MAX_TARGETS} 个目标", 400)
        clean, seen = [], set()
        for target in targets:
            if not isinstance(target, dict):
                raise ChannelError("invalid_target", "目标格式无效", 400)
            model, family, source = target.get("model"), target.get("family"), target.get("claimed_source")
            if not isinstance(model, str) or not MODEL_RE.fullmatch(model) or key in model or any(x in {".", ".."} for x in model.split("/")):
                raise ChannelError("invalid_model", "模型标识无效", 400)
            if not isinstance(source, str) or source not in SOURCE_MAP or SOURCE_MAP[source]["family"] != family:
                raise ChannelError("invalid_source", "来源与模型族不匹配", 400)
            wire = target.get("wire_family", "gpt")
            if not isinstance(wire, str) or wire not in FAMILY_LABELS:
                raise ChannelError("invalid_wire_family", "传输协议须为 gpt、claude 或 gemini", 400)
            if wire != "gpt" and wire != family:
                raise ChannelError("invalid_wire_family", "原生协议仅支持匹配的模型族", 400)
            item = (model, family, source, wire)
            if item not in seen:
                clean.append({"model": model, "family": family, "claimed_source": source, "wire_family": wire})
                seen.add(item)
        await public_address(base)
        job_id = channel_jobs.start(base, key, clean, timeout)
        return JSONResponse({"ok": True, "job_id": job_id}, headers={"Cache-Control": "no-store"})
    except ChannelError as exc:
        return failure(exc)
    except Exception:
        return failure(ChannelError("internal_error", "任务创建失败（详情不回显）", 500))
    finally:
        data.clear()
        key = ""


@router.get("/api/channel/jobs/{job_id}")
async def job(job_id: str):
    result = channel_jobs.get(job_id)
    if result is None:
        return failure(ChannelError("not_found", "任务不存在或已过期", 404))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/channel/records")
async def records(request: Request):
    try:
        return {"records": channel_store.query(*filters(request))}
    except ChannelError as exc:
        return failure(exc)
    except Exception:
        return failure(ChannelError("store_error", "记录读取失败", 500))


@router.get("/api/channel/export")
async def export(request: Request):
    try:
        fmt = request.query_params.get("format", "csv")
        if fmt not in {"csv", "json", "md"}:
            raise ChannelError("invalid_format", "仅支持 csv、json、md", 400)
        content, media = channel_store.export(channel_store.query(*filters(request)), fmt)
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="channel-records.{fmt}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except ChannelError as exc:
        return failure(exc)
    except Exception:
        return failure(ChannelError("store_error", "记录导出失败", 500))


@router.get("/api/channel/records/{record_id}")
async def record(record_id: str):
    try:
        result = channel_store.get(record_id)
        if result is None:
            return failure(ChannelError("not_found", "记录不存在", 404))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except Exception:
        return failure(ChannelError("store_error", "记录读取失败", 500))
