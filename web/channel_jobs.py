"""Bounded in-process channel jobs; credential lifetime ends with each worker."""
from __future__ import annotations

import asyncio
import copy
import time
import uuid

from . import channel_store
from .channel_probe import evidence, probe_target, record_for
from .channel_security import ChannelError, redact

MAX_JOBS = 100
MAX_RUNNING = 3
JOBS = {}
TASKS = set()


def start(base, key, targets, timeout):
    if sum(j["status"] == "running" for j in JOBS.values()) >= MAX_RUNNING:
        raise ChannelError("busy", "运行中任务已达上限", 429)
    while len(JOBS) >= MAX_JOBS:
        stale = next((i for i, j in JOBS.items() if j["status"] != "running"), None)
        if stale is None:
            raise ChannelError("busy", "任务保留容量已满", 429)
        del JOBS[stale]
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"id": job_id, "status": "running", "completed": 0, "total": len(targets), "current": None, "records": []}
    task = asyncio.create_task(_worker(job_id, base, key, targets, timeout))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return job_id


async def _worker(job_id, base, key, targets, timeout):
    job = JOBS[job_id]
    try:
        for target in targets:
            job["current"] = redact(target["model"], key)
            started = time.monotonic()
            try:
                record = await asyncio.wait_for(probe_target(base, key, target, timeout), timeout * 2 + 10)
            except Exception:
                record = record_for(base, target, [evidence("请求", "error", "模型探测失败或超时（详情不回显）")], started)
            record = redact(record, key)
            channel_store.save(record)
            job["records"].append(record)
            job["completed"] += 1
        job["status"] = "completed"
    except asyncio.CancelledError:
        job["status"] = "failed"
        job["error"] = {"code": "cancelled", "message": "任务被中止"}
    except Exception:
        job["status"] = "failed"
        job["error"] = {"code": "job_failed", "message": "任务处理或记录保存失败（详情不回显）"}
    finally:
        job["current"] = None
        key = ""
        targets = []


def get(job_id):
    return copy.deepcopy(JOBS.get(job_id))
