"""Storage layer tests — read/write LocalTuya's slice of core.config_entries.

We don't talk to a real HA here; tests use a tmp file with a hand-
crafted JSON shaped like what HA writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tuya_autoadd.storage import (
    append_devices,
    find_localtuya_entry,
    list_local_devices,
    load_config_entries,
    restore_backup,
    write_storage_atomic,
)


# Minimal valid config_entries shape with a LocalTuya entry + one
# existing device. Mirrors the real Shannon storage closely enough.
SAMPLE_STORAGE = {
    "version": 1,
    "minor_version": 1,
    "key": "core.config_entries",
    "data": {
        "entries": [
            {
                "entry_id": "abc123",
                "domain": "localtuya",
                "title": "localtuya",
                "data": {
                    "client_id": "tuya_client_id",
                    "client_secret": "tuya_client_secret",
                    "region": "eu",
                    "no_cloud": False,
                    "user_id": "tuya_user_id",
                    "username": "user@example.com",
                    "updated_at": "1778703584907",
                    "devices": {
                        "bf2285403741AAAA": {
                            "friendly_name": "Gold Light 1",
                            "host": "192.168.4.50",
                            "local_key": "0123456789abcdef",
                            "protocol_version": "3.4",
                            "product_key": "abc",
                            "platform": "light",
                            "entities": [
                                {"id": 20, "platform": "light"},
                            ],
                        },
                    },
                },
                "options": {},
            },
            {
                "entry_id": "other999",
                "domain": "weather",
                "title": "weather",
                "data": {},
            },
        ]
    },
}


@pytest.fixture
def storage_file(tmp_path: Path) -> Path:
    path = tmp_path / "core.config_entries"
    path.write_text(json.dumps(SAMPLE_STORAGE, indent=2))
    return path


class TestLoadAndFind:
    def test_load_round_trip(self, storage_file: Path):
        data = load_config_entries(storage_file)
        assert data["key"] == "core.config_entries"

    def test_find_localtuya_entry(self, storage_file: Path):
        data = load_config_entries(storage_file)
        entry = find_localtuya_entry(data)
        assert entry is not None
        assert entry["domain"] == "localtuya"
        assert entry["entry_id"] == "abc123"

    def test_find_localtuya_returns_none_if_absent(self):
        data = {"data": {"entries": [{"domain": "weather", "data": {}}]}}
        assert find_localtuya_entry(data) is None


class TestListLocalDevices:
    def test_lists_existing(self, storage_file: Path):
        data = load_config_entries(storage_file)
        devices = list_local_devices(data)
        assert len(devices) == 1
        assert devices[0].device_id == "bf2285403741AAAA"
        assert devices[0].friendly_name == "Gold Light 1"
        assert devices[0].has_local_key is True

    def test_empty_when_localtuya_missing(self):
        data = {"data": {"entries": []}}
        assert list_local_devices(data) == []


class TestAppendDevices:
    def test_idempotent_no_new(self, storage_file: Path):
        data = load_config_entries(storage_file)
        out = append_devices(data, {})
        assert out == data

    def test_appends_new_device(self, storage_file: Path):
        data = load_config_entries(storage_file)
        new = {
            "bf_closet_XYZ": {
                "friendly_name": "Closet",
                "host": "192.168.4.123",
                "local_key": "abcdef0123456789",
                "protocol_version": "3.4",
                "product_key": "xyz",
                "platform": "light",
                "entities": [{"id": 20, "platform": "light"}],
            }
        }
        out = append_devices(data, new)
        entry = find_localtuya_entry(out)
        devices = entry["data"]["devices"]
        assert "bf_closet_XYZ" in devices
        assert devices["bf_closet_XYZ"]["friendly_name"] == "Closet"
        # Original device still present
        assert "bf2285403741AAAA" in devices

    def test_doesnt_mutate_input(self, storage_file: Path):
        data = load_config_entries(storage_file)
        before = json.dumps(data)
        new = {"bf_x": {"friendly_name": "X", "host": "", "local_key": "k",
                        "protocol_version": "3.4", "product_key": "p",
                        "platform": "light", "entities": []}}
        append_devices(data, new)
        after = json.dumps(data)
        assert before == after, "append_devices must not mutate input"

    def test_raises_if_localtuya_missing(self):
        data = {"data": {"entries": [{"domain": "weather"}]}}
        with pytest.raises(RuntimeError, match="LocalTuya"):
            append_devices(data, {"bf_x": {}})


class TestWriteStorageAtomic:
    def test_writes_and_backs_up(self, storage_file: Path):
        data = load_config_entries(storage_file)
        modified = append_devices(data, {
            "bf_test_OOO": {
                "friendly_name": "Test Add",
                "host": "192.168.4.222",
                "local_key": "kkk",
                "protocol_version": "3.4",
                "product_key": "p",
                "platform": "light",
                "entities": [{"id": 20, "platform": "light"}],
            }
        })
        backup = write_storage_atomic(modified, storage_file)
        assert backup.exists()
        # Backup contains the OLD content:
        backed = json.loads(backup.read_text())
        assert "bf_test_OOO" not in find_localtuya_entry(backed)["data"]["devices"]
        # File contains the NEW content:
        new = json.loads(storage_file.read_text())
        assert "bf_test_OOO" in find_localtuya_entry(new)["data"]["devices"]

    def test_restore_backup(self, storage_file: Path):
        data = load_config_entries(storage_file)
        original_json = storage_file.read_text()
        modified = append_devices(data, {
            "bf_test_REVERT": {
                "friendly_name": "Will be reverted",
                "host": "",
                "local_key": "k",
                "protocol_version": "3.4",
                "product_key": "p",
                "platform": "light",
                "entities": [],
            }
        })
        backup = write_storage_atomic(modified, storage_file)
        restore_backup(backup, storage_file)
        restored = storage_file.read_text()
        # JSON-equivalent (the read might have whitespace differences;
        # compare parsed content):
        assert json.loads(restored) == json.loads(original_json)
