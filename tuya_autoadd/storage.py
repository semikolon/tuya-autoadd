"""Read + write LocalTuya's slice of HA's core.config_entries.

The format is `data.devices` keyed by Tuya device_id. We read it for
the diff pass + write it (atomically, with backup) when adding new
devices. HA picks up changes when LocalTuya's config_entry is
reloaded (`ha.reload_config_entry`); we do not write any other entry
in the file."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from .core import LocalTuyaDevice

log = logging.getLogger(__name__)

DEFAULT_STORAGE_PATH = Path("/config/.storage/core.config_entries")


def load_config_entries(path: Path = DEFAULT_STORAGE_PATH) -> dict:
    """Load HA's full config_entries storage as a dict. Caller is
    responsible for finding the LocalTuya slice via
    `find_localtuya_entry`."""
    with path.open() as f:
        return json.load(f)


def find_localtuya_entry(storage: dict) -> dict | None:
    """Return the LocalTuya config_entry dict (or None if not
    configured). Caller must NOT mutate the returned dict in-place
    if you don't want to corrupt the surrounding storage — use
    `replace_localtuya_devices` for safe writes."""
    for entry in storage.get("data", {}).get("entries", []):
        if entry.get("domain") == "localtuya":
            return entry
    return None


def list_local_devices(storage: dict) -> list[LocalTuyaDevice]:
    """Return identity-relevant fields for every LocalTuya device.
    Used by `diff_devices` to find what's already configured."""
    entry = find_localtuya_entry(storage)
    if entry is None:
        return []
    devices = entry.get("data", {}).get("devices", {}) or {}
    out: list[LocalTuyaDevice] = []
    for dev_id, dev in devices.items():
        out.append(
            LocalTuyaDevice(
                device_id=dev_id,
                friendly_name=dev.get("friendly_name", ""),
                has_local_key=bool(dev.get("local_key")),
            )
        )
    return out


def append_devices(
    storage: dict,
    new_devices: dict[str, dict],
) -> dict:
    """Return a COPY of storage with `new_devices` merged into the
    LocalTuya entry's `data.devices` dict.

    Raises RuntimeError if LocalTuya isn't configured (we don't bootstrap
    it from scratch — that's a one-time decision Fredrik makes via UI
    or the equivalent initial-setup flow). Idempotent: re-running with
    the same `new_devices` is a no-op (overwrites existing keys with
    identical content).
    """
    if not new_devices:
        return storage
    out = json.loads(json.dumps(storage))  # deep copy via json roundtrip
    entry = find_localtuya_entry(out)
    if entry is None:
        raise RuntimeError(
            "LocalTuya integration not configured in HA — bootstrap that "
            "first (one-time HA UI step or initial-setup flow); this tool "
            "only adds NEW devices to an existing config."
        )
    devices = entry.setdefault("data", {}).setdefault("devices", {})
    devices.update(new_devices)
    return out


def write_storage_atomic(
    storage: dict,
    path: Path = DEFAULT_STORAGE_PATH,
    *,
    backup_suffix: str = ".bak",
) -> Path:
    """Atomic write of storage JSON with a timestamped backup of the
    previous file. Returns the backup path so a caller can restore on
    failure.

    Atomicity model: write to a temp file in the same directory, fsync,
    rename over the target. Guarantees: either the new file is fully
    present, or the old one is — never a partial write.

    Permissions: copies the source file's permissions onto the temp
    file before rename, so HA's container sees the same mode.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(f"{path.suffix}{backup_suffix}-{ts}")
    if path.exists():
        shutil.copy2(path, backup)
        log.info("storage backup written: %s", backup)
    else:
        log.warning("storage file %s missing; backup skipped", path)

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=parent,
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(storage, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copystat(path, tmp_path)
        os.replace(tmp_path, path)
        log.info("storage written atomic: %s", path)
    except Exception:
        # On any write failure, try to clean up the temp file. The
        # backup already exists if we made it; caller can restore.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    return backup


def restore_backup(backup: Path, target: Path = DEFAULT_STORAGE_PATH) -> None:
    """Restore a backup created by `write_storage_atomic`. Used on
    HA-reload failure to roll back the new-device append."""
    if not backup.exists():
        raise FileNotFoundError(f"backup not found: {backup}")
    shutil.copy2(backup, target)
    log.warning("storage restored from backup: %s → %s", backup, target)
