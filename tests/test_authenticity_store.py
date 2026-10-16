"""Config persistence tests for web/authenticity_store.py.

The store is where the PRD's hard save-time rules live: the claimed source is
mandatory and never inferred, cross-family targets are refused, and a stale
version loses the optimistic-lock race with a 409.
"""

from __future__ import annotations

import json

import pytest

from web import authenticity as A
from web import authenticity_store as S


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("Yhs-ApiDetect_AUTHENTICITY_CONFIG", str(tmp_path / "configs.json"))
    return tmp_path


def _targets(*pairs):
    return [{"model": model, "claimed_source": source} for model, source in pairs]


def test_save_config_starts_at_version_one():
    config = S.save_config("chan-1", "https://relay.example.com/v1", _targets(("claude-sonnet-4-5", "anthropic_direct")), channel_name="测试渠道")
    assert config["version"] == 1
    assert config["enabled"] is True
    assert config["targets"] == [{"model": "claude-sonnet-4-5", "claimed_source": "anthropic_direct"}]
    assert S.get_config("chan-1")["channel_name"] == "测试渠道"


def test_saved_config_never_contains_an_api_key():
    S.save_config("chan-1", "https://relay.example.com", _targets(("gpt-5.5", "openai_direct")), channel_name="c")
    raw = S._config_path().read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "sk-" not in raw


def test_target_without_a_source_is_refused():
    with pytest.raises(A.ConfigError) as err:
        S.save_config("chan-1", "https://relay.example.com", [{"model": "claude-sonnet-4-5", "claimed_source": ""}])
    assert err.value.code == "missing_source"


def test_entirely_blank_rows_are_dropped():
    config = S.save_config(
        "chan-1",
        "https://relay.example.com",
        [{"model": "", "claimed_source": ""}, {"model": "gpt-5.5", "claimed_source": "openai_direct"}],
    )
    assert len(config["targets"]) == 1


def test_blank_model_with_a_source_is_allowed_at_save_time():
    config = S.save_config("chan-1", "https://relay.example.com", [{"model": "", "claimed_source": "openai_direct"}])
    assert config["targets"] == [{"model": "", "claimed_source": "openai_direct"}]


def test_cross_family_target_is_refused():
    with pytest.raises(A.ConfigError) as err:
        S.save_config("chan-1", "https://relay.example.com", _targets(("gemini-2.5-pro", "anthropic_direct")))
    assert err.value.code == "cross_family"


def test_duplicate_targets_are_collapsed():
    config = S.save_config(
        "chan-1",
        "https://relay.example.com",
        _targets(("gpt-5.5", "openai_direct"), ("gpt-5.5", "openai_direct"), ("gpt-5.5", "azure_openai")),
    )
    assert len(config["targets"]) == 2


def test_base_url_must_be_http():
    with pytest.raises(A.ConfigError) as err:
        S.save_config("chan-1", "relay.example.com", _targets(("gpt-5.5", "openai_direct")))
    assert err.value.code == "bad_base_url"


def test_config_without_targets_is_refused():
    with pytest.raises(A.ConfigError) as err:
        S.save_config("chan-1", "https://relay.example.com", [])
    assert err.value.code == "no_targets"


def test_update_bumps_the_version():
    first = S.save_config("chan-1", "https://relay.example.com", _targets(("gpt-5.5", "openai_direct")))
    second = S.save_config(
        "chan-1",
        "https://relay.example.com",
        _targets(("gpt-5.5", "openai_direct"), ("gpt-5.4-mini", "openai_direct")),
        expected_version=first["version"],
    )
    assert second["version"] == 2
    assert len(second["targets"]) == 2


def test_stale_version_raises_a_conflict_carrying_the_current_config():
    first = S.save_config("chan-1", "https://relay.example.com", _targets(("gpt-5.5", "openai_direct")))
    S.save_config(
        "chan-1",
        "https://relay.example.com",
        _targets(("gpt-5.5", "openai_direct")),
        expected_version=first["version"],
    )
    with pytest.raises(S.VersionConflict) as err:
        S.save_config(
            "chan-1",
            "https://relay.example.com",
            _targets(("gpt-5.5", "openai_direct")),
            expected_version=first["version"],
        )
    assert err.value.current["version"] == 2


def test_set_enabled_toggles_and_bumps_the_version():
    config = S.save_config("chan-1", "https://relay.example.com", _targets(("gpt-5.5", "openai_direct")))
    disabled = S.set_enabled("chan-1", False, expected_version=config["version"])
    assert disabled["enabled"] is False
    assert disabled["version"] == 2
    assert S.enabled_configs() == []


def test_set_enabled_on_a_missing_channel_is_refused():
    with pytest.raises(A.ConfigError) as err:
        S.set_enabled("nope", False)
    assert err.value.code == "unknown_channel"


def test_batch_update_reports_errors_without_aborting_the_batch():
    S.save_config("chan-1", "https://relay.example.com", _targets(("gpt-5.5", "openai_direct")))
    result = S.batch_update(
        [
            {"channel_id": "chan-1", "enabled": False},
            {"channel_id": "chan-missing", "enabled": False},
            # a row that violates a save-time rule must not abort the batch
            {"channel_id": "chan-1", "targets": [{"model": "gpt-5.5", "claimed_source": ""}]},
        ]
    )
    assert len(result["updated"]) == 1
    assert len(result["errors"]) == 2
    assert {e["channel_id"] for e in result["errors"]} == {"chan-missing", "chan-1"}
    assert S.get_config("chan-1")["enabled"] is False


def test_delete_config_removes_only_that_channel():
    S.save_config("chan-1", "https://a.example.com", _targets(("gpt-5.5", "openai_direct")))
    S.save_config("chan-2", "https://b.example.com", _targets(("gpt-5.5", "openai_direct")))
    assert S.delete_config("chan-1") is True
    assert S.get_config("chan-1") is None
    assert S.get_config("chan-2") is not None
    assert S.delete_config("chan-1") is False


def test_list_configs_is_sorted_and_corrupt_files_do_not_crash():
    S.save_config("chan-b", "https://b.example.com", _targets(("gpt-5.5", "openai_direct")), channel_name="乙")
    S.save_config("chan-a", "https://a.example.com", _targets(("gpt-5.5", "openai_direct")), channel_name="甲")
    names = [c["channel_name"] for c in S.list_configs()]
    assert names == sorted(names)

    S._config_path().write_text("{not json", encoding="utf-8")
    assert S.load_configs() == {}


def test_config_file_is_written_atomically_and_re_readable():
    S.save_config("chan-1", "https://relay.example.com", _targets(("gpt-5.5", "openai_direct")))
    payload = json.loads(S._config_path().read_text(encoding="utf-8"))
    assert set(payload) == {"chan-1"}
    assert payload["chan-1"]["base_url"] == "https://relay.example.com"
