"""Async task runner for authenticity probes.

Two entry points, matching the PRD:
  * a single-channel immediate test, and
  * a full asynchronous sweep over every enabled channel.

The sweep is single-flight: a second start while one is running is a conflict,
not a duplicate run. API keys live in memory for the duration of a task only —
they are never written to disk, and are dropped when the task finishes.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import authenticity as engine

_MAX_CONCURRENT = 3
_TASK_TTL_S = 6 * 3600

_SEMA = asyncio.Semaphore(_MAX_CONCURRENT)
_LOCK = asyncio.Lock()
_TASKS: dict[str, AuthTask] = {}
_ACTIVE_SWEEP: str | None = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SweepInProgress(Exception):
    """A full sweep is already running — the caller turns this into a 409."""

    def __init__(self, task_id: str) -> None:
        super().__init__("sweep already running")
        self.task_id = task_id


class TaskNotFound(Exception):
    pass


@dataclass
class AuthTask:
    id: str
    kind: str  # "single" | "sweep"
    channel_ids: list[str] = field(default_factory=list)
    status: str = "queued"  # queued | running | done | failed
    total: int = 0
    finished: int = 0
    current: str = ""
    record_ids: list[str] = field(default_factory=list)
    config_errors: list[dict[str, str]] = field(default_factory=list)
    target_failures: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=_iso_now)
    started_at: str | None = None
    finished_at: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "total": self.total,
            "finished": self.finished,
            "current": self.current,
            "record_ids": list(self.record_ids),
            "config_errors": list(self.config_errors),
            "target_failures": list(self.target_failures),
            "error": self.error,
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
            finished = task.finished_at or task.created_at
            try:
                when = datetime.fromisoformat(finished).timestamp()
            except ValueError:
                when = time.time()
            if when < cutoff:
                _TASKS.pop(task_id, None)


def get_task(task_id: str) -> dict[str, Any]:
    task = _TASKS.get(task_id)
    if task is None:
        raise TaskNotFound(task_id)
    return task.snapshot()


def status() -> dict[str, Any]:
    _prune()
    sweep = _TASKS.get(_ACTIVE_SWEEP) if _ACTIVE_SWEEP else None
    if sweep is not None and sweep.status in {"done", "failed"}:
        sweep = None
    recent = sorted(_TASKS.values(), key=lambda t: t.created_at, reverse=True)[:10]
    return {
        "sweep_running": sweep is not None,
        "running_sweep": sweep.snapshot() if sweep is not None else None,
        "recent": [t.snapshot() for t in recent],
    }


def _shape_errors(targets: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split a raw saved-target list into well-formed entries and shape errors.

    Saved config is operator-editable and can be written by an older schema, so
    an element is not guaranteed to be a mapping. A non-mapping element is a
    configuration problem to report and skip — never a reason to abandon the
    whole batch, which would strand every later model behind the bad entry.
    """
    well_formed: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    if not isinstance(targets, (list, tuple)):
        return well_formed, errors
    for index, item in enumerate(targets):
        if not isinstance(item, dict):
            errors.append(
                {
                    "target": "第 " + str(index + 1) + " 个目标",
                    "code": "invalid_target",
                    "message": "该目标不是 {model, claimed_source} 结构(实际是 "
                    + type(item).__name__
                    + "),已跳过该目标,其余模型继续检测。",
                }
            )
            continue
        well_formed.append(item)
    return well_formed, errors


def _resolve_targets(
    targets: list[dict[str, str]], available_models: list[str] | None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split saved targets into runnable ones and config errors.

    A target whose model was left blank needs the relay's model list; if no
    same-family model exists, that is a configuration error — reported as such,
    never turned into a detection record.
    """
    runnable: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    well_formed, shape_errors = _shape_errors(targets)
    errors.extend(shape_errors)
    for target in well_formed:
        model = str(target.get("model", "") or "")
        source = str(target.get("claimed_source", "") or "")
        try:
            resolved = engine.validate_target(model, source, available_models, allow_blank_model=False)
        except engine.ConfigError as exc:
            errors.append({"target": model + " / " + source, "code": exc.code, "message": exc.message})
            continue
        runnable.append({"model": resolved["model"], "claimed_source": resolved["claimed_source"]})
    return runnable, errors


async def _run_targets(
    task: AuthTask,
    base_url: str,
    api_key: str,
    targets: list[dict[str, str]],
    channel_name: str,
    options: dict[str, Any],
) -> None:
    well_formed, shape_errors = _shape_errors(targets)
    task.config_errors.extend(shape_errors)
    # A shape error maps 1:1 onto a real element of the batch, so it must also
    # advance the progress counter — otherwise the bar strands short of total.
    task.finished += len(shape_errors)

    available_models: list[str] | None = None
    if any(not str(t.get("model", "") or "").strip() for t in well_formed):
        probe = await engine.probe_channel_models(base_url, api_key)
        available_models = probe.get("models") or []
        if not probe.get("ok"):
            for target in well_formed:
                if not str(target.get("model", "") or "").strip():
                    task.config_errors.append(
                        {
                            "target": "自动选型:" + str(target.get("claimed_source", "")),
                            "code": "models_unavailable",
                            "message": "无法读取该渠道的模型列表,无法为留空的模型自动选型。",
                        }
                    )

    runnable, errors = _resolve_targets(well_formed, available_models)
    task.config_errors.extend(errors)

    for target in runnable:
        task.current = target["model"] + " / " + target["claimed_source"]
        try:
            record = await engine.run_target(
                base_url=base_url,
                api_key=api_key,
                model=target["model"],
                claimed_source=target["claimed_source"],
                channel_name=channel_name or base_url,
                **options,
            )
            record_id = engine.save_record(record)
        except engine.ConfigError as exc:
            task.config_errors.append(
                {"target": task.current, "code": exc.code, "message": exc.message}
            )
            task.finished += 1
            continue
        except Exception as exc:
            # 单个模型任何异常都只终结它自己:记录原因、计入进度、继续跑后面的模型。
            task.target_failures.append(
                {
                    "target": task.current,
                    "code": type(exc).__name__,
                    "message": str(exc) or type(exc).__name__,
                }
            )
            task.finished += 1
            continue
        task.record_ids.append(record_id)
        task.finished += 1
    task.current = ""


async def _run_single(task: AuthTask, channel: dict[str, Any], options: dict[str, Any]) -> None:
    task.status = "running"
    task.started_at = _iso_now()
    try:
        async with _SEMA:
            await _run_targets(
                task,
                str(channel.get("base_url", "")),
                str(channel.get("api_key", "")),
                list(channel.get("targets") or []),
                str(channel.get("channel_name", "")),
                options,
            )
        task.status = "done"
    except Exception as exc:  # pragma: no cover - defensive
        task.status = "failed"
        task.error = type(exc).__name__ + ": " + str(exc)
    finally:
        channel["api_key"] = None
        task.finished_at = _iso_now()
        task.current = ""


async def _run_sweep(task: AuthTask, channels: list[dict[str, Any]], options: dict[str, Any]) -> None:
    global _ACTIVE_SWEEP
    task.status = "running"
    task.started_at = _iso_now()
    try:
        for channel in channels:
            task.current = str(channel.get("channel_name") or channel.get("channel_id") or "")
            try:
                async with _SEMA:
                    await _run_targets(
                        task,
                        str(channel.get("base_url", "")),
                        str(channel.get("api_key", "")),
                        list(channel.get("targets") or []),
                        str(channel.get("channel_name", "")),
                        options,
                    )
            except Exception as exc:
                # 一个渠道崩塌不应带走后面所有渠道:记下原因,继续下一个。
                task.target_failures.append(
                    {
                        "target": task.current or "该渠道",
                        "code": type(exc).__name__,
                        "message": str(exc) or type(exc).__name__,
                    }
                )
            finally:
                channel["api_key"] = None
        task.status = "done"
    except Exception as exc:  # pragma: no cover - defensive
        task.status = "failed"
        task.error = type(exc).__name__ + ": " + str(exc)
    finally:
        task.finished_at = _iso_now()
        task.current = ""
        async with _LOCK:
            if _ACTIVE_SWEEP == task.id:
                _ACTIVE_SWEEP = None


def _default_options(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}

    def bounded(key: str, default: float, low: float, high: float) -> float:
        """Clamp an operator-supplied option; falsy / unparsable input uses the default."""
        try:
            value = float(raw.get(key) or default)
        except (TypeError, ValueError):
            value = float(default)
        return max(low, min(value, high))

    return {
        "question_count": int(bounded("question_count", 3, 1, 8)),
        "error_probe": bool(raw.get("error_probe", True)),
        "cache_probe": bool(raw.get("cache_probe", True)),
        "param_probe": bool(raw.get("param_probe", True)),
        # Per-request probe timeout in seconds (engine.run_target(timeout=...)).
        "timeout": bounded("timeout", 45, 5, 300),
    }


async def submit_single(channel: dict[str, Any], options: dict[str, Any] | None = None) -> str:
    """Start an immediate test for one saved channel config."""
    async with _LOCK:
        _prune()
        task = AuthTask(
            id=_new_task_id(),
            kind="single",
            channel_ids=[str(channel.get("channel_id", ""))],
            total=len(channel.get("targets") or []),
        )
        _TASKS[task.id] = task
        asyncio.create_task(_run_single(task, dict(channel), _default_options(options)))
        return task.id


async def submit_sweep(
    channels: list[dict[str, Any]], options: dict[str, Any] | None = None
) -> str:
    """Start a full sweep; refuses to start a second one concurrently."""
    global _ACTIVE_SWEEP
    async with _LOCK:
        _prune()
        active = _TASKS.get(_ACTIVE_SWEEP) if _ACTIVE_SWEEP else None
        if active is not None and active.status in {"queued", "running"}:
            raise SweepInProgress(active.id)
        total = sum(len(c.get("targets") or []) for c in channels)
        task = AuthTask(
            id=_new_task_id(),
            kind="sweep",
            channel_ids=[str(c.get("channel_id", "")) for c in channels],
            total=total,
        )
        _TASKS[task.id] = task
        _ACTIVE_SWEEP = task.id
        asyncio.create_task(_run_sweep(task, [dict(c) for c in channels], _default_options(options)))
        return task.id


def reset() -> None:
    """Test hook: forget every task."""
    global _ACTIVE_SWEEP
    _TASKS.clear()
    _ACTIVE_SWEEP = None


__all__ = [
    "AuthTask",
    "SweepInProgress",
    "TaskNotFound",
    "get_task",
    "reset",
    "status",
    "submit_single",
    "submit_sweep",
]
