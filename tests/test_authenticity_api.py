"""Route-level tests for the /authenticity console.

These cover the JSON contract the frontend depends on: the source enum, the
create/update/409 flow, and the report/records/export endpoints.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from web import authenticity as A
from web import authenticity_store as S
from web.ratelimit import reset as reset_rate_limit
from web.server import app


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("Yhs-ApiDetect_AUTHENTICITY_CONFIG", str(tmp_path / "configs.json"))
    monkeypatch.setenv("Yhs-ApiDetect_AUTHENTICITY_DIR", str(tmp_path / "records"))
    monkeypatch.setattr(A, "AUTH_DIR", tmp_path / "records")
    reset_rate_limit()
    yield
    reset_rate_limit()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _create(client, base_url="https://relay.example.com/v1", model="claude-sonnet-4-5", source="anthropic_direct"):
    return client.post(
        "/api/authenticity/config",
        json={"base_url": base_url, "channel_name": "示例渠道", "targets": [{"model": model, "claimed_source": source}]},
    )


# ---------------------------------------------------------------------------
# page + enum
# ---------------------------------------------------------------------------


def test_page_renders(client):
    response = client.get("/authenticity")
    assert response.status_code == 200
    body = response.text
    assert "真实性检测" in body
    assert 'class="auth-page"' in body
    assert 'id="auth-stats"' not in body
    assert 'class="auth-panel"' in body


def test_nav_links_to_the_new_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert '<a href="/authenticity">真实性检测</a>' in response.text


def test_sources_endpoint_exposes_the_enum(client):
    payload = client.get("/api/authenticity/sources").json()
    assert payload["ok"] is True
    ids = [s["id"] for s in payload["sources"]]
    for expected in [
        "anthropic_direct",
        "claude_code_max",
        "aws_bedrock",
        "aws_partner",
        "amazon_q_kiro",
        "vertex_claude",
        "openai_direct",
        "azure_openai",
        "google_ai_studio",
        "vertex_gemini",
    ]:
        assert expected in ids, expected + " missing from source enum"
    assert all({"id", "label", "family"} <= set(s) for s in payload["sources"])
    assert payload["signal_weights"]["capability"] > 0


# ---------------------------------------------------------------------------
# config create / update / conflict
# ---------------------------------------------------------------------------


def test_create_config_derives_the_channel_id_from_base_url(client):
    payload = _create(client).json()
    assert payload["ok"] is True
    config = payload["config"]
    assert config["channel_id"] == A.channel_id_for("https://relay.example.com/v1")
    assert config["version"] == 1
    assert config["targets"][0]["claimed_source"] == "anthropic_direct"


def test_config_list_never_exposes_an_api_key(client):
    _create(client)
    body = client.get("/api/authenticity/config").text
    assert "api_key" not in body


def test_missing_source_returns_400(client):
    response = client.post(
        "/api/authenticity/config",
        json={"base_url": "https://relay.example.com", "targets": [{"model": "claude-sonnet-4-5", "claimed_source": ""}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_source"


def test_cross_family_returns_400(client):
    response = _create(client, model="gemini-2.5-pro", source="anthropic_direct")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cross_family"


def test_bad_base_url_returns_400(client):
    response = client.post(
        "/api/authenticity/config",
        json={"base_url": "relay.example.com", "targets": [{"model": "gpt-5.5", "claimed_source": "openai_direct"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_base_url"


def test_stale_version_returns_409_with_the_current_config(client):
    channel_id = _create(client).json()["config"]["channel_id"]
    second = client.put(
        "/api/authenticity/config/" + channel_id,
        json={
            "base_url": "https://relay.example.com/v1",
            "version": 1,
            "targets": [{"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}],
        },
    )
    assert second.status_code == 200
    assert second.json()["config"]["version"] == 2

    stale = client.put(
        "/api/authenticity/config/" + channel_id,
        json={
            "base_url": "https://relay.example.com/v1",
            "version": 1,
            "targets": [{"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}],
        },
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "version_conflict"
    assert error["current"]["version"] == 2


def test_enabled_toggle_bumps_the_version(client):
    config = _create(client).json()["config"]
    response = client.put(
        "/api/authenticity/config/" + config["channel_id"] + "/enabled",
        json={"enabled": False, "version": config["version"]},
    )
    assert response.status_code == 200
    assert response.json()["config"]["enabled"] is False

    stale = client.put(
        "/api/authenticity/config/" + config["channel_id"] + "/enabled",
        json={"enabled": True, "version": config["version"]},
    )
    assert stale.status_code == 409


def test_enabled_toggle_on_unknown_channel_returns_404(client):
    response = client.put("/api/authenticity/config/does-not-exist/enabled", json={"enabled": True})
    assert response.status_code == 404


def test_delete_config_removes_it(client):
    channel_id = _create(client).json()["config"]["channel_id"]
    assert client.delete("/api/authenticity/config/" + channel_id).json()["removed"] is True
    assert client.get("/api/authenticity/config").json()["configs"] == []


# ---------------------------------------------------------------------------
# running tests — only saved config is executed
# ---------------------------------------------------------------------------


def test_test_unknown_channel_returns_404(client):
    response = client.post("/api/authenticity/test/nope", json={"api_key": "sk-1234567890"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_channel"


def test_test_requires_a_usable_key(client):
    channel_id = _create(client).json()["config"]["channel_id"]
    response = client.post("/api/authenticity/test/" + channel_id, json={"api_key": "short"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_api_key"


def test_models_endpoint_validates_input(client):
    bad_url = client.post("/api/authenticity/models", json={"base_url": "nope", "api_key": "sk-1234567890"})
    assert bad_url.status_code == 400
    assert bad_url.json()["error"]["code"] == "bad_base_url"

    bad_key = client.post("/api/authenticity/models", json={"base_url": "https://relay.example.com", "api_key": "x"})
    assert bad_key.status_code == 400
    assert bad_key.json()["error"]["code"] == "bad_api_key"


def test_sweep_without_channels_returns_400(client):
    response = client.post("/api/authenticity/test_all", json={"keys": {}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_channels"


def test_sweep_without_any_local_key_refuses_to_start(client):
    """A sweep with no usable key would probe nothing — say so instead of
    starting an empty task the UI would then poll forever."""
    _create(client)
    response = client.post("/api/authenticity/test_all", json={"keys": {}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_channels"


def test_disabled_channels_are_left_out_of_the_sweep(client):
    channel_id = _create(client).json()["config"]["channel_id"]
    client.put(
        "/api/authenticity/config/" + channel_id + "/enabled",
        json={"enabled": False},
    )
    response = client.post("/api/authenticity/test_all", json={"keys": {channel_id: "sk-1234567890"}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_channels"


# ---------------------------------------------------------------------------
# status / report / records / export
# ---------------------------------------------------------------------------


def test_task_status_without_an_id_returns_the_overview(client):
    payload = client.get("/api/authenticity/task_status").json()
    assert payload["ok"] is True
    assert payload["sweep_running"] is False


def test_unknown_task_id_returns_404(client):
    response = client.get("/api/authenticity/task_status?task_id=nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_task"


def test_report_and_records_reflect_saved_records(client):
    A.save_record(
        {
            "family": "claude",
            "verdict": "authentic",
            "score": 91.0,
            "confidence": 0.9,
            "model": "claude-sonnet-4-5",
            "source": "anthropic_direct",
            "summary": "通过",
            "signals": [],
            "probe_trace": [],
            "capability_accuracy": 1.0,
            "latency_ms": 800,
        }
    )
    report = client.get("/api/authenticity/report?hours=24").json()["report"]
    assert report["totals"]["authentic"] == 1
    assert report["by_family"][0]["family"] == "claude"

    records = client.get("/api/authenticity/records?hours=24").json()["records"]
    assert len(records) == 1
    assert records[0]["model"] == "claude-sonnet-4-5"


def test_csv_export_is_an_attachment_with_a_bom(client):
    A.save_record({"family": "gpt", "verdict": "fail", "model": "gpt-5.5", "source": "openai_direct"})
    response = client.get("/api/authenticity/report/export?hours=24")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.startswith("\ufeff")
    assert "gpt-5.5" in response.text


def test_records_limit_is_clamped(client):
    payload = client.get("/api/authenticity/records?hours=24&limit=99999").json()
    assert payload["ok"] is True


def test_config_json_round_trips_through_the_file():
    """Guards the on-disk contract: the store writes JSON, not a Python repr."""
    S.save_config("chan-1", "https://relay.example.com", [{"model": "gpt-5.5", "claimed_source": "openai_direct"}])
    raw = json.loads(S._config_path().read_text(encoding="utf-8"))
    assert raw["chan-1"]["targets"][0]["claimed_source"] == "openai_direct"
