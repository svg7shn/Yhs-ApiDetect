"""生图检测的任务运行器。

一次检测 = 一个 task:选定模型(或手填模型名)+ 选定测试项,后台跑完,
前端按站内既有范式轮询 GET /api/image/task_status 拿进度与逐项结果(不引 SSE)。

约定:
* 同一时间只允许一个检测任务在跑,重复启动返回冲突。
* API Key 只在该任务的内存里活到任务结束,不落盘、不进日志。
* 证据(请求 JSON / 响应原文 / 生成图片)落在 Yhs-ApiDetect_IMAGE_DIR 下按 task 分目录,
  任务结束时写一份 run.json,并向 history.jsonl 追加一行摘要供 /api/image/history 读取。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import image_detect as engine

# 默认落在项目内的 web_data/image:Windows 上 "/opt/..." 会被解析成当前盘根的 \opt(建不出来),
# 本地直接跑服务时不该因为没设环境变量就卡住;Linux/线上部署仍可用环境变量覆盖。
_DEFAULT_IMAGE_DIR = Path(__file__).resolve().parent.parent / "web_data" / "image"
IMAGE_DIR = Path(os.environ.get("Yhs-ApiDetect_IMAGE_DIR") or _DEFAULT_IMAGE_DIR)
_TASK_TTL_S = 6 * 3600
_HISTORY_LIMIT = 50

_TASKS: dict[str, ImageTask] = {}
_ACTIVE: str | None = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TaskNotFound(Exception):
    pass


class RunInProgress(Exception):
    """已经有一个检测任务在跑——调用方把它转成 409。"""

    def __init__(self, task_id: str) -> None:
        super().__init__("image run already in progress")
        self.task_id = task_id


@dataclass
class ImageTask:
    id: str
    base_url: str
    models: list[dict[str, Any]] = field(default_factory=list)
    manual_models: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    timeout_s: float = engine.DEFAULT_TIMEOUT_S
    retry: int = 0
    status: str = "queued"  # queued | running | done | failed
    total: int = 0
    finished: int = 0
    current: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    passed: int = 0
    failed: int = 0
    created_at: str = field(default_factory=_iso_now)
    started_at: str | None = None
    finished_at: str | None = None
    out_dir: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status,
            "total": self.total,
            "finished": self.finished,
            "current": self.current,
            "results": list(self.results),
            "error": self.error,
            "passed": self.passed,
            "failed": self.failed,
            "base_url": self.base_url,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _new_task_id() -> str:
    return secrets.token_urlsafe(6)


def _prune() -> None:
    cutoff = time.time() - _TASK_TTL_S
    for task_id, task in list(_TASKS.items()):
        if task.status in {"done", "failed"}:
            stamp = task.finished_at or task.created_at
            try:
                when = datetime.fromisoformat(stamp).timestamp()
            except ValueError:
                when = time.time()
            if when < cutoff:
                _TASKS.pop(task_id, None)


def get_task(task_id: str) -> ImageTask:
    task = _TASKS.get(task_id)
    if task is None:
        raise TaskNotFound(task_id)
    return task


def active_task_id() -> str | None:
    return _ACTIVE


def _phase_count(spec: engine.ModelSpec, phases: set[str]) -> int:
    """估算一个模型要跑几项,用于前端进度条分母。"""
    if spec.kind == "chat":
        return 1 if engine.want_phase(phases, engine.PHASE_CHAT) else 0
    selected = [
        bg for bg in engine.BACKGROUNDS if engine.want_phase(phases, "generation_" + bg)
    ]
    need_edit = engine.want_phase(phases, engine.PHASE_EDIT_IMAGES) or engine.want_phase(
        phases, engine.PHASE_EDIT_ALIAS
    )
    count = len(selected)
    if need_edit and not selected:
        count += 1  # 自动补跑一张原图
    if need_edit:
        count += sum(1 for name, _ in engine.EDIT_PATHS if engine.want_phase(phases, name))
    return count


def start_run(
    *,
    base_url: str,
    api_key: str,
    models: list[dict[str, Any]] | None,
    manual_models: list[str] | None,
    phases: list[str] | None,
    timeout_s: float | None,
    retry: int | None,
) -> ImageTask:
    """校验入参 → 建任务 → 起后台协程。校验失败抛 ValueError(调用方转 400)。"""
    global _ACTIVE, _TASKS

    if not str(api_key or "").strip():
        raise ValueError("API Key 不能为空")

    phase_set = engine.normalize_phases(phases)
    cfg = engine.ProbeConfig(
        base_url=base_url or engine.DEFAULT_BASE_URL,
        api_key=str(api_key).strip(),
        timeout_s=float(timeout_s or engine.DEFAULT_TIMEOUT_S),
        retry=int(retry or 0),
    ).normalized()

    _prune()
    if _ACTIVE and _TASKS.get(_ACTIVE) and _TASKS[_ACTIVE].status in {"queued", "running"}:
        raise RunInProgress(_ACTIVE)

    task_id = _new_task_id()
    task = ImageTask(
        id=task_id,
        base_url=cfg.base_url,
        models=[m for m in (models or []) if isinstance(m, dict)],
        manual_models=[str(m).strip() for m in (manual_models or []) if str(m).strip()],
        phases=sorted(phase_set) if phase_set else [],
        timeout_s=cfg.timeout_s,
        retry=cfg.retry,
        out_dir=str(IMAGE_DIR / task_id),
    )
    try:
        Path(task.out_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            "证据目录不可写:" + str(IMAGE_DIR) + "(" + str(exc) + ");"
            "请把环境变量 Yhs-ApiDetect_IMAGE_DIR 指到可写目录后重启服务。"
        ) from exc
    _TASKS[task_id] = task
    _ACTIVE = task_id
    asyncio.get_event_loop().create_task(_run(task, cfg))
    return task


async def _run(task: ImageTask, cfg: engine.ProbeConfig) -> None:
    global _ACTIVE
    task.status = "running"
    task.started_at = _iso_now()
    task.current = "准备中…"
    phase_set = set(task.phases)
    out_dir = Path(task.out_dir)

    def publish(res: engine.CheckResult) -> None:
        # 没真的写出图片就别给前端一个打不开的链接。
        if res.image_file and not Path(res.image_file).exists():
            res.image_file = ""
        task.results.append(res.to_dict())
        task.finished += 1
        task.current = f"{res.model} · {engine.PHASE_LABELS.get(res.phase, res.phase)}"
        if res.passed:
            task.passed += 1
        else:
            task.failed += 1

    try:
        # mkdir 必须在 try 内:否则它抛出的 OSError 会绕过下面的 except,
        # 让任务永远停在 running、_ACTIVE 也清不掉(之后的提交全被 409 挡住)。
        out_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            specs = await engine.resolve_models(cfg, client, task.models, task.manual_models)
            mismatch = engine.selection_mismatch(specs, phase_set)
            if mismatch:
                raise ValueError(mismatch)
            usable = [s for s in specs if engine.has_applicable_phase(s, phase_set)]
            task.total = sum(_phase_count(s, phase_set) for s in usable)
            for spec in usable:
                task.current = f"{spec.name}（{spec.kind} · {spec.size}）"
                await engine.test_model(cfg, client, spec, phase_set, out_dir, publish)
        task.status = "done"
    except Exception as exc:  # noqa: BLE001 — 任务级失败统一落到 error
        task.status = "failed"
        task.error = str(exc) or type(exc).__name__
    finally:
        task.finished_at = _iso_now()
        task.current = "" if task.status == "done" else task.current
        if _ACTIVE == task.id:
            _ACTIVE = None
        try:
            _write_artifacts(task, out_dir)
        except OSError:
            pass  # 证据目录不可用时不该把任务状态一起丢掉


def _write_artifacts(task: ImageTask, out_dir: Path) -> None:
    summary = {
        "task_id": task.id,
        "base_url": task.base_url,
        "status": task.status,
        "start_time": task.started_at or task.created_at,
        "end_time": task.finished_at,
        "selection": _selection_label(task),
        "total": task.finished,
        "passed": task.passed,
        "failed": task.failed,
        "error": task.error,
        "results": task.results,
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "run.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index = {
            "task_id": task.id,
            "base_url": task.base_url,
            "status": task.status,
            "start_time": summary["start_time"],
            "end_time": summary["end_time"],
            "selection": summary["selection"],
            "total": summary["total"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "out_dir": str(out_dir),
            "error": task.error,
        }
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        with (IMAGE_DIR / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(index, ensure_ascii=False) + "\n")
    except OSError:
        # 证据落盘失败不该把检测结果一起吞掉:结果仍在内存快照里。
        pass


def _selection_label(task: ImageTask) -> str:
    names = [str(m.get("name") or "") for m in task.models] or list(task.manual_models)
    label = ",".join(n for n in names if n)
    if len(names) > 3:
        label = f"{len(names)} 个模型"
    if not label:
        label = "全部图片模型"
    phase_label = "全部测试项" if not task.phases else ",".join(task.phases)
    return f"{label} | {phase_label}"


def read_history(limit: int = _HISTORY_LIMIT) -> list[dict[str, Any]]:
    """读最近 N 条检测摘要(来自 IMAGE_DIR/history.jsonl,越新越前)。"""
    path = IMAGE_DIR / "history.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError:
        return []
    return list(reversed(rows))[: max(1, limit)]


def evidence_path(task_id: str, rel: str) -> Path:
    """把证据相对路径解析到任务目录内,越界一律拒绝。"""
    task = get_task(task_id)
    root = (Path(task.out_dir)).resolve()
    target = (root / rel.lstrip("/\\")).resolve()
    if root != target and root not in target.parents:
        raise ValueError("证据路径越界")
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target
