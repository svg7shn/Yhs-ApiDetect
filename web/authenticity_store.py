"""Config store for the authenticity feature.

The browser keeps the *secret* (base_url + api_key) in page memory only — it is
never written to localStorage; this module keeps the *targets* — the (model,
claimed_source) pairs — on the server, because those must be trusted: cross-family
rejection and the mandatory-source rule are security decisions, and a client-side
check is not a boundary.

Optimistic locking follows the PRD: every save carries the version the client
read, and a stale version is a conflict (HTTP 409 by the caller), never a silent
overwrite.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import authenticity as engine


def _config_path() -> Path:
    override = os.environ.get("Yhs-ApiDetect_AUTHENTICITY_CONFIG")
    if override:
        return Path(override)
    return engine.AUTH_DIR / "configs.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class VersionConflict(Exception):
    """The client's version is stale — the caller turns this into a 409."""

    def __init__(self, current: dict[str, Any] | None) -> None:
        super().__init__("config version conflict")
        self.current = current


def load_configs() -> dict[str, dict[str, Any]]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def _write_configs(configs: dict[str, dict[str, Any]]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(configs, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_configs() -> list[dict[str, Any]]:
    configs = load_configs()
    return sorted(configs.values(), key=lambda c: str(c.get("channel_name") or c.get("base_url") or ""))


def get_config(channel_id: str) -> dict[str, Any] | None:
    return load_configs().get(channel_id)


def normalise_targets(
    raw_targets: Iterable[Any], available_models: Iterable[str] | None = None
) -> list[dict[str, str]]:
    """Validate and de-duplicate the target list.

    An entirely blank row (no model *and* no source) is dropped — that is just an
    empty input line. A row carrying a model but no source is rejected: the
    claimed source is mandatory and must never be inferred.
    """
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_targets or []:
        if isinstance(item, dict):
            model = str(item.get("model", "") or "").strip()
            source = str(item.get("claimed_source", "") or "").strip()
        else:
            continue
        if not model and not source:
            continue
        target = engine.validate_target(model, source, available_models, allow_blank_model=True)
        key = (target["model"], target["claimed_source"])
        if key in seen:
            continue
        seen.add(key)
        targets.append({"model": target["model"], "claimed_source": target["claimed_source"]})
    return targets


def save_config(
    channel_id: str,
    base_url: str,
    targets: Iterable[Any],
    channel_name: str = "",
    enabled: bool = True,
    expected_version: int | None = None,
    available_models: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Create or update one channel config. Raises ConfigError / VersionConflict."""
    channel_id = (channel_id or "").strip() or engine.channel_id_for(base_url)
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise engine.ConfigError("bad_base_url", "base_url 必须以 http:// 或 https:// 开头。")

    normalised = normalise_targets(targets, available_models)
    if not normalised:
        raise engine.ConfigError("no_targets", "至少需要一个检测目标(模型 + 声称来源)。")

    configs = load_configs()
    existing = configs.get(channel_id)
    if existing is not None and expected_version is not None:
        if int(existing.get("version", 0)) != int(expected_version):
            raise VersionConflict(existing)

    version = int(existing.get("version", 0)) + 1 if existing else 1
    config = {
        "channel_id": channel_id,
        "channel_name": (channel_name or "").strip() or base_url,
        "base_url": base_url,
        "enabled": bool(enabled),
        "version": version,
        "updated_at": _iso_now(),
        "targets": normalised,
    }
    configs[channel_id] = config
    _write_configs(configs)
    return config


def set_enabled(channel_id: str, enabled: bool, expected_version: int | None = None) -> dict[str, Any]:
    configs = load_configs()
    config = configs.get(channel_id)
    if config is None:
        raise engine.ConfigError("unknown_channel", "该渠道尚未保存检测配置。")
    if expected_version is not None and int(config.get("version", 0)) != int(expected_version):
        raise VersionConflict(config)
    config["enabled"] = bool(enabled)
    config["version"] = int(config.get("version", 0)) + 1
    config["updated_at"] = _iso_now()
    configs[channel_id] = config
    _write_configs(configs)
    return config


def batch_update(items: Iterable[Any]) -> dict[str, Any]:
    """Apply enabled/targets changes to several channels in one write.

    Malformed entries are reported per-channel instead of aborting the batch, so
    one bad row cannot block an operator's bulk action.
    """
    configs = load_configs()
    updated: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("channel_id", "") or "").strip()
        config = configs.get(channel_id)
        if config is None:
            errors.append({"channel_id": channel_id, "message": "该渠道尚未保存检测配置。"})
            continue
        try:
            if "enabled" in item:
                config["enabled"] = bool(item["enabled"])
            if "targets" in item:
                config["targets"] = normalise_targets(item["targets"])
            if "channel_name" in item:
                config["channel_name"] = str(item["channel_name"] or "").strip() or config.get("base_url", "")
            config["version"] = int(config.get("version", 0)) + 1
            config["updated_at"] = _iso_now()
            configs[channel_id] = config
            updated.append(config)
        except engine.ConfigError as exc:
            errors.append({"channel_id": channel_id, "message": exc.message})
    _write_configs(configs)
    return {"updated": updated, "errors": errors}


def delete_config(channel_id: str) -> bool:
    configs = load_configs()
    if channel_id not in configs:
        return False
    del configs[channel_id]
    _write_configs(configs)
    return True


def enabled_configs() -> list[dict[str, Any]]:
    return [c for c in list_configs() if c.get("enabled")]


__all__ = [
    "VersionConflict",
    "batch_update",
    "delete_config",
    "enabled_configs",
    "get_config",
    "list_configs",
    "load_configs",
    "normalise_targets",
    "save_config",
    "set_enabled",
]
