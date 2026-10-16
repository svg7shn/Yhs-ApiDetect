"""Local record deletion. Never trust resource paths stored in record payloads.

Handlers deliberately do not yield between the running-state check and mutation:
the application's single asyncio worker cannot finish/save a task in that gap.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import (
    authenticity,
    authenticity_jobs,
    channel_jobs,
    channel_store,
    image_jobs,
    jobs,
)

router = APIRouter()
ACTIVE = {"queued", "running"}


class DeleteError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise DeleteError(status, code, message)


def guard(request, record_id, channel=False):
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        fail(403, "cross_site_delete", "删除请求缺少同源校验标记。")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        fail(403, "cross_site_delete", "不允许跨站删除。")
    def origin(value):
        parsed = urlsplit(value)
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    for name in ("origin", "referer"):
        if name in request.headers:
            try:
                valid = origin(request.headers[name]) == origin(str(request.url))
            except ValueError:
                valid = False
            if not valid:
                fail(403, "cross_site_delete", "不允许跨站删除。")
    pattern = r"[0-9a-f]{32}" if channel else r"[A-Za-z0-9_-]{8}"
    if not re.fullmatch(pattern, record_id):
        fail(400, "invalid_id", "记录 ID 不合法。")


def checked(root, path):
    """Reject links/junctions and containment escapes, even between local records."""
    root = Path(root).absolute()
    path = Path(path).absolute()
    if root not in path.parents:
        fail(400, "unsafe_path", "记录资源路径越界。")
    current = path
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            fail(400, "unsafe_path", "记录资源包含链接，拒绝删除。")
        current = current.parent
    if root.resolve() not in path.resolve().parents:
        fail(400, "unsafe_path", "记录资源路径越界。")
    return path


def running(value):
    if value in ACTIVE:
        fail(409, "record_running", "该记录所属任务正在运行，请结束后重试。")


def delete_auth(record_id):
    related = [t for t in authenticity_jobs._TASKS.values() if record_id in t.record_ids]
    for task in related:
        running(task.status)
    path = checked(authenticity.AUTH_DIR, authenticity.AUTH_DIR / "records" / (record_id + ".json"))
    if not path.is_file():
        fail(404, "record_not_found", "记录不存在或已删除。")
    path.unlink()
    for task in related:
        task.record_ids[:] = [i for i in task.record_ids if i != record_id]


def delete_channel(record_id):
    related = [j for j in channel_jobs.JOBS.values() if any(r.get("id") == record_id for r in j.get("records", []))]
    for job in related:
        running(job["status"])
    conn = channel_store.connect()
    try:
        conn.execute("PRAGMA secure_delete=ON")
        with conn:
            count = conn.execute("DELETE FROM records WHERE id=?", (record_id,)).rowcount
            if not count:
                fail(404, "record_not_found", "记录不存在或已删除。")
    finally:
        conn.close()
    for job in related:
        job["records"][:] = [r for r in job["records"] if r.get("id") != record_id]


def delete_history(record_id):
    task = jobs._JOBS.get(record_id)
    if task:
        running(task.status)
    # Exact names only; never use a user-controlled glob or a payload path.
    reports = [checked(jobs.JOBS_DIR, p) for p in jobs._report_candidates(record_id)]
    images = [checked(jobs.JOBS_DIR, p.with_suffix(".jpg")) for p in reports]
    if not task and not any(p.exists() for p in reports + images):
        fail(404, "record_not_found", "记录不存在或已删除。")
    for path in images + reports:
        path.unlink(missing_ok=True)
    jobs._JOBS.pop(record_id, None)


def delete_image(record_id):
    task = image_jobs._TASKS.get(record_id)
    if task:
        running(task.status)
    if image_jobs._ACTIVE == record_id:
        fail(409, "record_running", "该生图任务尚未结束。")
    root = image_jobs.IMAGE_DIR
    directory = checked(root, root / record_id)
    history = checked(root, root / "history.jsonl")
    lines = history.read_text(encoding="utf-8").splitlines(keepends=True) if history.exists() else []
    retained, found = [], False
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            retained.append(line)
            continue
        if isinstance(row, dict) and row.get("task_id") == record_id:
            running(row.get("status"))
            found = True
        else:
            retained.append(line)
    if not found and not directory.exists() and not task:
        fail(404, "record_not_found", "记录不存在或已删除。")
    if directory.exists():
        for path in directory.rglob("*"):
            checked(root, path)
    # Prepare the full index before destroying anything. Preserve all unrelated
    # lines, including malformed old entries. Atomic replacement avoids truncation.
    temporary = None
    try:
        if found:
            fd, temporary = tempfile.mkstemp(prefix=".history-delete-", dir=root)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.writelines(retained)
                stream.flush()
                os.fsync(stream.fileno())
        if directory.exists():
            shutil.rmtree(directory)
        if temporary:
            os.replace(temporary, history)
            temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    image_jobs._TASKS.pop(record_id, None)


def execute(request, record_id, delete, channel=False):
    try:
        guard(request, record_id, channel)
        delete(record_id)
        return JSONResponse({"ok": True, "deleted_id": record_id}, headers={"Cache-Control": "no-store"})
    except DeleteError as exc:
        return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)
    except (OSError, sqlite3.Error):
        # Never claim success after a partial filesystem failure. Retrying clears
        # any remaining files/index; do not leak local paths or database details.
        return JSONResponse({"ok": False, "error": {"code": "delete_failed", "message": "本地删除失败，可能仍有残留；请检查文件权限后重试。"}}, status_code=500)


@router.delete("/api/authenticity/records/{record_id}")
async def authenticity_delete(request: Request, record_id: str):
    return execute(request, record_id, delete_auth)


@router.delete("/api/channel/records/{record_id}")
async def channel_delete(request: Request, record_id: str):
    return execute(request, record_id, delete_channel, channel=True)


@router.delete("/api/history/{job_id}")
async def history_delete(request: Request, job_id: str):
    return execute(request, job_id, delete_history)


@router.delete("/api/image/history/{run_id}")
async def image_delete(request: Request, run_id: str):
    # The history API calls this identifier task_id, not run_id.
    return execute(request, run_id, delete_image)
