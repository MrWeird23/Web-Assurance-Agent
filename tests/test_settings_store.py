from pathlib import Path

import pytest

from triage_agent.settings_store import RuntimeSettings, SettingsStore


def test_settings_store_uses_defaults_when_file_is_missing(tmp_path: Path) -> None:
    defaults = RuntimeSettings(confirmation_attempts=3)
    store = SettingsStore(tmp_path / "settings.yaml", defaults=defaults)

    assert store.settings == defaults
    assert not (tmp_path / "settings.yaml").exists()


def test_settings_store_replace_validates_and_atomically_writes(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    store = SettingsStore(path, defaults=RuntimeSettings())

    saved = store.replace({"confirmation_attempts": 4, "log_level": "debug"})

    assert saved.confirmation_attempts == 4
    assert saved.log_level == "DEBUG"
    assert store.settings == saved
    assert "confirmation_attempts: 4" in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_settings_store_rejects_invalid_payload_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    store = SettingsStore(path, defaults=RuntimeSettings())

    with pytest.raises(ValueError):
        store.replace({"confirmation_attempts": 99})

    assert not path.exists()
    assert store.settings == RuntimeSettings()


def test_settings_store_rejects_non_discord_webhook_url(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.yaml", defaults=RuntimeSettings())

    with pytest.raises(ValueError):
        store.replace({"discord_webhook_url": "https://evil.example/steal"})


def test_settings_store_reloads_persisted_values_on_construction(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    SettingsStore(path, defaults=RuntimeSettings()).replace({"confirmation_attempts": 5})

    reloaded = SettingsStore(path, defaults=RuntimeSettings())

    assert reloaded.settings.confirmation_attempts == 5


def test_restart_required_vs_flags_only_cold_fields() -> None:
    base = RuntimeSettings()

    assert not base.restart_required_vs(base)
    # confirmation_attempts differs but isn't a cold field -> no restart needed
    assert not base.restart_required_vs(RuntimeSettings(confirmation_attempts=1))
    assert base.restart_required_vs(RuntimeSettings(request_timeout_seconds=30))
    assert base.restart_required_vs(RuntimeSettings(manual_check_concurrency=2))
