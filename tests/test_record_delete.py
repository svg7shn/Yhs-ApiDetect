"""Deletion tests use only pytest temporary directories, never user data."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from web import (
    authenticity,
    authenticity_jobs,
    channel_jobs,
    channel_store,
    image_jobs,
    jobs,
    record_delete,
)
from web.server import app

HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": "http://testserver"}
ROUTES = ["/api/authenticity/records/", "/api/channel/records/", "/api/history/", "/api/image/history/"]


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(authenticity, "AUTH_DIR", tmp_path / "auth")
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(image_jobs, "IMAGE_DIR", tmp_path / "image")
    monkeypatch.setenv("Yhs-ApiDetect_CHANNEL_DIR", str(tmp_path / "channel"))
    for module, key in [(authenticity_jobs, "_TASKS"), (channel_jobs, "JOBS"), (jobs, "_JOBS"), (image_jobs, "_TASKS")]:
        monkeypatch.setattr(module, key, {})
    monkeypatch.setattr(image_jobs, "_ACTIVE", None)
    with TestClient(app) as client:
        yield client


def seed(kind):
    ids = ["a" * 32, "b" * 32] if kind == 1 else ["delete01", "retain02"]
    for rid in ids:
        if kind == 0:
            authenticity.save_record({"record_id": rid, "model": "test"})
        elif kind == 1:
            conn = channel_store.connect()
            with conn:
                conn.execute("INSERT INTO records VALUES (?, ?, ?)", (rid, "2099-01-01", json.dumps({"id": rid, "base_url": "https://example.org"})))
            conn.close()
        elif kind == 2:
            for protocol in ("anthropic", "openai", "gemini"):
                jobs.report_path(rid, protocol).write_text(json.dumps({"protocol": protocol}), encoding="utf-8")
                jobs.image_path(rid, protocol).write_bytes(b"report image")
            (jobs.JOBS_DIR / (rid + ".json")).write_text("{}", encoding="utf-8")
            (jobs.JOBS_DIR / (rid + ".jpg")).write_bytes(b"legacy image")
            jobs._JOBS[rid] = jobs.Job(id=rid, status="done")
        else:
            directory = image_jobs.IMAGE_DIR / rid
            directory.mkdir(parents=True)
            (directory / "image.png").write_bytes(b"image")
            task = image_jobs.ImageTask(id=rid, base_url="https://example.org", status="done", out_dir=str(directory))
            image_jobs._TASKS[rid] = task
            image_jobs._write_artifacts(task, directory)
    if kind == 0:
        authenticity_jobs._TASKS["task"] = SimpleNamespace(status="done", record_ids=ids.copy())
    if kind == 1:
        channel_jobs.JOBS["task"] = {"status": "completed", "records": [{"id": rid} for rid in ids]}
    return ids


@pytest.mark.parametrize("kind", range(4))
def test_delete_and_reload_preserves_other_records(local, kind):
    deleted, kept = seed(kind)
    response = local.delete(ROUTES[kind] + deleted, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "deleted_id": deleted}
    assert local.delete(ROUTES[kind] + deleted, headers=HEADERS).status_code == 404
    if kind == 0:
        assert [r["record_id"] for r in authenticity.list_records(None)] == [kept]
        assert authenticity_jobs._TASKS["task"].record_ids == [kept]
    elif kind == 1:
        assert channel_store.get(deleted) is None
        assert channel_store.get(kept)["id"] == kept
        assert channel_jobs.JOBS["task"]["records"] == [{"id": kept}]
    elif kind == 2:
        assert asyncio.run(jobs.get(deleted)) is None
        assert asyncio.run(jobs.get(kept)) is not None
        assert all(r["job_id"] == kept for r in jobs.list_reports())
        assert not list(jobs.JOBS_DIR.rglob(deleted + ".*"))
        assert len(list(jobs.JOBS_DIR.rglob(kept + ".*"))) == 8
    else:
        assert [r["task_id"] for r in image_jobs.read_history()] == [kept]
        assert not (image_jobs.IMAGE_DIR / deleted).exists()
        assert (image_jobs.IMAGE_DIR / kept / "image.png").is_file()
        assert deleted not in image_jobs._TASKS
        assert local.get("/api/image/task_status", params={"task_id": deleted}).status_code == 404


@pytest.mark.parametrize("kind", range(4))
def test_running_record_conflicts(local, kind):
    deleted, kept = seed(kind)
    if kind == 0:
        authenticity_jobs._TASKS["task"].status = "running"
    elif kind == 1:
        channel_jobs.JOBS["task"]["status"] = "running"
    elif kind == 2:
        jobs._JOBS[deleted].status = "queued"
    else:
        image_jobs._TASKS[deleted].status = "running"
    result = local.delete(ROUTES[kind] + deleted, headers=HEADERS)
    assert result.status_code == 409
    assert result.json()["ok"] is False


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("rid", ["bad.id", "%2e%2e%5csecret", "C%3Asecret", "x" * 129])
def test_invalid_ids(local, route, rid):
    result = local.delete(route + rid, headers=HEADERS)
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "invalid_id"


@pytest.mark.parametrize("kind", range(4))
@pytest.mark.parametrize("extra", [{}, {"Origin": "https://other.example"}, {"Origin": "null"}, {"Referer": "http://other.example/x"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}])
def test_cross_site_delete_denied(local, kind, extra):
    deleted, _ = seed(kind)
    headers = dict(HEADERS, **extra) if extra else {}
    result = local.delete(ROUTES[kind] + deleted, headers=headers)
    assert result.status_code == 403
    assert local.delete(ROUTES[kind] + deleted, headers=HEADERS).status_code == 200


@pytest.mark.parametrize("kind", range(4))
def test_storage_failure_never_returns_ok(local, monkeypatch, kind):
    deleted, _ = seed(kind)
    def denied(*args, **kwargs):
        raise PermissionError("test only")
    if kind == 1:
        monkeypatch.setattr(channel_store, "connect", denied)
    elif kind == 3:
        monkeypatch.setattr(record_delete.shutil, "rmtree", denied)
    else:
        monkeypatch.setattr(Path, "unlink", denied)
    result = local.delete(ROUTES[kind] + deleted, headers=HEADERS)
    assert result.status_code == 500
    assert result.json()["ok"] is False


def test_image_untrusted_out_dir_and_duplicate_index(local, tmp_path):
    deleted, kept = seed(3)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "precious.txt").write_text("keep", encoding="utf-8")
    history = image_jobs.IMAGE_DIR / "history.jsonl"
    with history.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"task_id": deleted, "out_dir": str(external)}) + "\n")
        stream.write("malformed old line\n")
    image_jobs._TASKS.clear()  # Simulated process restart: no in-memory lookup.
    assert local.delete(ROUTES[3] + deleted, headers=HEADERS).status_code == 200
    assert (external / "precious.txt").read_text() == "keep"
    assert "malformed old line" in history.read_text(encoding="utf-8")
    assert [r["task_id"] for r in image_jobs.read_history()] == [kept]
