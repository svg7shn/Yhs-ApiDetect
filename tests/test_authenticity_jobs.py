"""Job-queue tests for web/authenticity_jobs.py.

The queue is what turns saved config into records, so these tests pin the two
promises the PRD makes about it: a sweep cannot be started twice, and a target
that cannot be resolved becomes a *config error* rather than a fabricated record.
"""

from __future__ import annotations

import asyncio

import pytest

from web import authenticity as A
from web import authenticity_jobs as J


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    J.reset()
    monkeypatch.setattr(A, "AUTH_DIR", tmp_path / "records")
    yield
    J.reset()


def _channel(**overrides):
    channel = {
        "channel_id": "chan-1",
        "channel_name": "示例渠道",
        "base_url": "https://relay.example.com/v1",
        "targets": [{"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}],
        "api_key": "sk-1234567890",
    }
    channel.update(overrides)
    return channel


def _fake_record(**kwargs):
    return {
        "family": "claude",
        "verdict": "authentic",
        "model": kwargs.get("model", ""),
        "source": kwargs.get("claimed_source", ""),
        "capability_accuracy": 1.0,
        "latency_ms": 100,
        "signals": [],
        "probe_trace": [],
    }


async def _wait(task_id, timeout=5.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = J.get_task(task_id)
        if task["status"] in {"done", "failed"}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not finish within " + str(timeout) + "s")


# ---------------------------------------------------------------------------
# single-channel run
# ---------------------------------------------------------------------------


async def test_single_test_writes_exactly_one_record(monkeypatch):
    calls = []

    async def fake_run_target(**kwargs):
        calls.append(kwargs)
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    task = await _wait(await J.submit_single(_channel()))

    assert task["status"] == "done"
    assert task["finished"] == 1
    assert len(calls) == 1
    assert calls[0]["model"] == "claude-sonnet-4-5"
    assert calls[0]["claimed_source"] == "anthropic_direct"

    records = A.list_records(hours=None)
    assert len(records) == 1
    assert "sk-1234567890" not in repr(records)


async def test_blank_model_is_resolved_from_the_relay_model_list(monkeypatch):
    async def fake_probe(base_url, api_key, **kwargs):
        return {"ok": True, "wire_family": "anthropic", "models": ["claude-haiku-4-5"], "attempts": []}

    calls = []

    async def fake_run_target(**kwargs):
        calls.append(kwargs)
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "probe_channel_models", fake_probe)
    monkeypatch.setattr(A, "run_target", fake_run_target)

    channel = _channel(targets=[{"model": "", "claimed_source": "anthropic_direct"}])
    task = await _wait(await J.submit_single(channel))

    assert task["status"] == "done"
    assert calls[0]["model"] == "claude-haiku-4-5"
    assert A.list_records(hours=None)[0]["model"] == "claude-haiku-4-5"


async def test_unresolvable_target_is_reported_and_never_recorded(monkeypatch):
    """No same-family model on the relay => config error, not a fake record."""

    async def fake_probe(base_url, api_key, **kwargs):
        return {"ok": True, "wire_family": "openai", "models": ["gpt-5.5"], "attempts": []}

    async def boom(**kwargs):
        raise AssertionError("run_target must not be called for an unresolvable target")

    monkeypatch.setattr(A, "probe_channel_models", fake_probe)
    monkeypatch.setattr(A, "run_target", boom)

    channel = _channel(targets=[{"model": "", "claimed_source": "anthropic_direct"}])
    task = await _wait(await J.submit_single(channel))

    assert task["record_ids"] == []
    assert task["config_errors"][0]["code"] == "no_same_family_model"
    assert A.list_records(hours=None) == []


async def test_one_refused_target_does_not_abort_the_rest(monkeypatch):
    calls = []

    async def fake_run_target(**kwargs):
        if kwargs["model"] == "claude-sonnet-4-5":
            raise A.ConfigError("no_same_family_model", "该渠道没有同族模型")
        calls.append(kwargs)
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    channel = _channel(
        targets=[
            {"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"},
            {"model": "claude-haiku-4-5", "claimed_source": "anthropic_direct"},
        ]
    )
    task = await _wait(await J.submit_single(channel))

    assert task["status"] == "done"
    assert task["finished"] == 2
    assert len(task["record_ids"]) == 1
    assert len(task["config_errors"]) == 1
    assert [c["model"] for c in calls] == ["claude-haiku-4-5"]


# ---------------------------------------------------------------------------
# sweep single-flight
# ---------------------------------------------------------------------------


async def test_a_second_sweep_is_refused_while_one_is_running(monkeypatch):
    gate = asyncio.Event()

    async def fake_run_target(**kwargs):
        await gate.wait()
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    first = await J.submit_sweep([_channel()])
    with pytest.raises(J.SweepInProgress) as err:
        await J.submit_sweep([_channel()])
    assert err.value.task_id == first
    assert J.status()["sweep_running"] is True

    gate.set()
    await _wait(first)
    assert J.status()["sweep_running"] is False


async def test_a_sweep_can_start_again_once_the_previous_one_finished(monkeypatch):
    async def fake_run_target(**kwargs):
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    first = await J.submit_sweep([_channel()])
    await _wait(first)
    second = await J.submit_sweep([_channel()])
    assert second != first
    await _wait(second)


async def test_sweep_covers_every_channel(monkeypatch):
    seen = []

    async def fake_run_target(**kwargs):
        seen.append(kwargs["channel_name"])
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    channels = [
        _channel(channel_id="chan-1", channel_name="甲"),
        _channel(channel_id="chan-2", channel_name="乙"),
    ]
    task = await _wait(await J.submit_sweep(channels))

    assert task["kind"] == "sweep"
    assert task["total"] == 2
    assert set(seen) == {"甲", "乙"}


# ---------------------------------------------------------------------------
# status / lookup
# ---------------------------------------------------------------------------


async def test_unknown_task_id_is_reported():
    with pytest.raises(J.TaskNotFound):
        J.get_task("does-not-exist")


async def test_status_lists_recent_tasks(monkeypatch):
    async def fake_run_target(**kwargs):
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)
    task_id = await J.submit_single(_channel())
    await _wait(task_id)

    overview = J.status()
    assert overview["sweep_running"] is False
    assert any(t["task_id"] == task_id for t in overview["recent"])


def test_options_are_clamped_and_probes_default_to_on():
    assert J._default_options({}) == {
        "question_count": 3,
        "error_probe": True,
        "cache_probe": True,
        "param_probe": True,
        "timeout": 45.0,
    }
    assert J._default_options({"question_count": 99})["question_count"] == 8
    # 0 is falsy, so it falls back to the default rather than asking zero questions.
    assert J._default_options({"question_count": 0})["question_count"] == 3
    assert J._default_options({"error_probe": False})["error_probe"] is False
    assert J._default_options({"cache_probe": False})["cache_probe"] is False
    # The per-request probe timeout is operator-tunable through the console, but
    # bounded so a typo cannot hang a sweep or time out every probe instantly.
    assert J._default_options({"timeout": 60})["timeout"] == 60.0
    assert J._default_options({"timeout": 1})["timeout"] == 5.0
    assert J._default_options({"timeout": 9999})["timeout"] == 300.0
    assert J._default_options({"timeout": "abc"})["timeout"] == 45.0


def test_resolve_targets_separates_runnable_from_config_errors():
    runnable, errors = J._resolve_targets(
        [
            {"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"},
            {"model": "", "claimed_source": "anthropic_direct"},
        ],
        ["gpt-5.5"],
    )
    assert runnable == [{"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}]
    assert errors[0]["code"] == "no_same_family_model"
# ---------------------------------------------------------------------------
# A failing model must not take the rest of the batch down with it
# ---------------------------------------------------------------------------


async def test_an_unexpected_error_skips_only_that_model(monkeypatch):
    """One model raising anything at all must not abort the remaining models."""
    calls = []

    async def fake_run_target(**kwargs):
        if kwargs["model"] == "claude-sonnet-4-5":
            raise AttributeError("'str' object has no attribute 'get'")
        calls.append(kwargs)
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    channel = _channel(
        targets=[
            {"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"},
            {"model": "claude-haiku-4-5", "claimed_source": "anthropic_direct"},
            {"model": "claude-opus-4-5", "claimed_source": "anthropic_direct"},
        ]
    )
    task = await _wait(await J.submit_single(channel))

    assert task["status"] == "done"
    assert not task["error"]
    assert task["finished"] == 3
    assert len(task["record_ids"]) == 2
    assert [c["model"] for c in calls] == ["claude-haiku-4-5", "claude-opus-4-5"]
    assert len(task["target_failures"]) == 1
    failure = task["target_failures"][0]
    assert failure["target"] == "claude-sonnet-4-5 / anthropic_direct"
    assert failure["code"] == "AttributeError"
    assert "has no attribute 'get'" in failure["message"]


async def test_a_non_mapping_target_is_reported_and_skipped(monkeypatch):
    """A stray string in saved targets must not abort the batch (the field bug)."""
    seen = []

    async def fake_run_target(**kwargs):
        seen.append(kwargs["model"])
        return _fake_record(**kwargs)

    monkeypatch.setattr(A, "run_target", fake_run_target)

    channel = _channel(
        targets=[
            {"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"},
            "claude-haiku-4-5",
            {"model": "claude-opus-4-5", "claimed_source": "anthropic_direct"},
        ]
    )
    task = await _wait(await J.submit_single(channel))

    assert task["status"] == "done"
    assert not task["error"]
    assert seen == ["claude-sonnet-4-5", "claude-opus-4-5"]
    assert len(task["record_ids"]) == 2
    assert task["finished"] == 3
    shape = [e for e in task["config_errors"] if e["code"] == "invalid_target"]
    assert len(shape) == 1
    assert shape[0]["target"] == "第 2 个目标"
    assert task["target_failures"] == []


def test_shape_errors_keeps_well_formed_targets():
    well_formed, errors = J._shape_errors(
        [None, {"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}, []]
    )
    assert well_formed == [{"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}]
    assert [e["code"] for e in errors] == ["invalid_target", "invalid_target"]
    assert [e["target"] for e in errors] == ["第 1 个目标", "第 3 个目标"]
    assert J._shape_errors("claude-sonnet-4-5") == ([], [])

