from __future__ import annotations

import json
import os
import stat

import pytest

from src.core.settings import (
    MAX_ROUNDS,
    AppSettings,
    ModelSlot,
    SettingsStore,
    app_dir,
    clamp_rounds,
    parse_int,
    settings_to_dict,
    user_dir,
)

SECRET = "sk-super-secreta-1234567890"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    return tmp_path


def _write_legacy(home, with_key=True, extra=None):
    slot = {"id": "s1", "name": "IA 1", "provider": "openai", "model": "gpt", "enabled": True}
    if with_key:
        slot["api_key"] = SECRET
    data = {"configured": True, "rounds": 2, "slots": [slot], **(extra or {})}
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_user_dir_is_subfolder(home):
    assert user_dir() == app_dir() / "usuario"
    assert user_dir().is_dir()


def test_default_path_is_in_user_dir(home):
    st = SettingsStore()
    assert st.path == home / "usuario" / "config.json"


def test_legacy_config_migrates_with_backup_and_keeps_original(home):
    _write_legacy(home, with_key=False)
    original = (home / "config.json").read_bytes()
    st = SettingsStore()
    assert st.migrated_from == home / "config.json"
    assert st.settings.rounds == 2
    assert (home / "usuario" / "config.json").read_bytes() == original
    assert (home / "config.json.bak").read_bytes() == original
    assert (home / "config.json").exists()


def test_migration_runs_only_once(home):
    _write_legacy(home, with_key=False)
    SettingsStore()
    st = SettingsStore()
    st.settings.rounds = 7
    st.save()
    (home / "config.json").write_text(json.dumps({"rounds": 1}), encoding="utf-8")
    st2 = SettingsStore()
    assert st2.migrated_from is None
    assert st2.settings.rounds == 7


def test_new_install_has_no_legacy(home):
    st = SettingsStore()
    assert st.migrated_from is None and st.legacy_files == []


def test_key_not_serialized_when_save_disabled(tmp_path):
    st = SettingsStore(tmp_path / "c.json")
    slot = st.add_slot()
    slot.api_key = SECRET
    st.settings.director.api_key = SECRET + "-dir"
    st.save()
    text = (tmp_path / "c.json").read_text(encoding="utf-8")
    assert SECRET not in text
    assert "api_key" not in text
    assert st.settings.slots[0].api_key == SECRET


def test_key_serialized_when_save_enabled(tmp_path):
    st = SettingsStore(tmp_path / "c.json")
    slot = st.add_slot()
    slot.api_key = SECRET
    slot.save_key = True
    st.save()
    text = (tmp_path / "c.json").read_text(encoding="utf-8")
    assert SECRET in text
    st2 = SettingsStore(tmp_path / "c.json")
    assert st2.settings.slots[0].api_key == SECRET
    assert st2.settings.slots[0].save_key is True


def test_mixed_slots_only_marked_ones_save_key(tmp_path):
    st = SettingsStore(tmp_path / "c.json")
    a, b = st.add_slot(), st.add_slot()
    a.api_key, a.save_key = "KEY-A-111", True
    b.api_key = "KEY-B-222"
    d = settings_to_dict(st.settings)
    assert d["slots"][0]["api_key"] == "KEY-A-111"
    assert "api_key" not in d["slots"][1]


def test_repr_never_shows_key():
    assert SECRET not in repr(ModelSlot(api_key=SECRET))
    assert SECRET not in repr(AppSettings(slots=[ModelSlot(api_key=SECRET)]))


@pytest.mark.skipif(os.name == "nt", reason="permisos POSIX")
def test_config_file_permissions_600(tmp_path):
    st = SettingsStore(tmp_path / "c.json")
    st.save()
    assert stat.S_IMODE((tmp_path / "c.json").stat().st_mode) == 0o600


def test_legacy_keys_are_preserved_and_flagged(home):
    _write_legacy(home, with_key=True)
    st = SettingsStore()
    assert st.needs_key_notice
    assert st.settings.slots[0].api_key == SECRET
    assert st.settings.slots[0].save_key is True
    st.save()
    assert SECRET in (home / "usuario" / "config.json").read_text(encoding="utf-8")


def test_legacy_keys_keep_choice(home):
    _write_legacy(home, with_key=True)
    st = SettingsStore()
    st.keep_legacy_keys()
    st2 = SettingsStore()
    assert not st2.needs_key_notice
    assert st2.settings.slots[0].api_key == SECRET
    assert (home / "config.json").exists()


def test_legacy_keys_drop_choice_removes_from_disk(home):
    _write_legacy(home, with_key=True)
    st = SettingsStore()
    removed = st.drop_legacy_keys()
    assert len(removed) == 2
    assert not (home / "config.json").exists() and not (home / "config.json.bak").exists()
    assert SECRET not in (home / "usuario" / "config.json").read_text(encoding="utf-8")
    assert st.settings.slots[0].api_key == SECRET
    st2 = SettingsStore()
    assert not st2.needs_key_notice
    assert st2.settings.slots[0].api_key == ""


def test_no_notice_without_legacy_keys(home):
    _write_legacy(home, with_key=False)
    assert not SettingsStore().needs_key_notice


def test_old_config_without_new_fields_loads(tmp_path):
    (tmp_path / "c.json").write_text(json.dumps({"rounds": 3, "agent": {"max_steps": 12}}), encoding="utf-8")
    s = SettingsStore(tmp_path / "c.json").settings
    assert s.rounds == 3 and s.agent.max_steps == 12
    assert s.agent.unlimited_steps is False
    assert s.language == "es"


def test_parse_int_strict():
    assert parse_int("3") == 3 and parse_int(" 12 ") == 12 and parse_int(7) == 7
    for bad in ("", "0", "-1", "2.5", "1,5", "abc", "1e3", "٣", "1 2", None, True, 0, "9" * 30):
        assert parse_int(bad) is None, bad
    assert parse_int("999", 1, 50) == 50


def test_clamp_rounds():
    assert clamp_rounds(0) == 1 and clamp_rounds(-4) == 1 and clamp_rounds("x") == 1
    assert clamp_rounds(4) == 4 and clamp_rounds(10**6) == MAX_ROUNDS
