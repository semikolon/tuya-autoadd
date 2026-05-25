"""Orchestrator — the function called by the systemd-timer entry point.

Single pass: cloud-list → diff → discover LAN IPs → fetch DPs →
build entries → write storage (atomic + backup) → reload HA. Logs
each transition so a journalctl tail tells the full story of what
the system found + did this cycle.

Idempotent: re-running with no new devices is a no-op (zero writes,
zero side effects). Safe to schedule frequently.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Sequence

import requests

from .cloud import TuyaCloudClient
from .core import (
    CloudDevice,
    DpHint,
    SHANNON_LIFE_SUPPORT_NAME_DENYLIST,
    build_localtuya_entry,
    diff_devices,
)
from .discover import attach_lan_ips, discover_lan_ips
from .ha import HaClient
from .storage import (
    DEFAULT_STORAGE_PATH,
    append_devices,
    find_localtuya_entry,
    list_local_devices,
    load_config_entries,
    restore_backup,
    write_storage_atomic,
)

log = logging.getLogger(__name__)


class CycleResult:
    """What one poll cycle did. Suitable for logging."""

    def __init__(self) -> None:
        self.cloud_count = 0
        self.local_count = 0
        self.new_devices: list[CloudDevice] = []
        self.added: list[str] = []  # device_ids successfully added
        self.skipped_no_lan: list[str] = []  # cloud says online but LAN didn't broadcast
        self.errors: list[str] = []

    def summary(self) -> str:
        return (
            f"cloud={self.cloud_count} local={self.local_count} "
            f"new={len(self.new_devices)} added={len(self.added)} "
            f"skipped_no_lan={len(self.skipped_no_lan)} errors={len(self.errors)}"
        )


def run_once(
    *,
    cloud: TuyaCloudClient,
    ha: HaClient,
    storage_path: Path = DEFAULT_STORAGE_PATH,
    lan_scan_timeout: int = 12,
    require_lan_ip: bool = True,
    denylist_ids: frozenset[str] = frozenset(),
    denylist_name_substrings: tuple[str, ...] = SHANNON_LIFE_SUPPORT_NAME_DENYLIST,
) -> CycleResult:
    """One complete poll cycle.

    `require_lan_ip=True` (default) skips cloud devices that haven't
    been LAN-discovered yet — local control needs the IP. Cycle will
    pick them up next poll once the device broadcasts.

    `require_lan_ip=False` adds them with empty host so LocalTuya can
    still receive cloud state-change pushes (useful for sensors that
    don't expose LAN at all). Future-knob; off by default.
    """
    res = CycleResult()
    try:
        storage = load_config_entries(storage_path)
    except FileNotFoundError as e:
        res.errors.append(f"storage missing: {e}")
        log.error("storage file not found at %s", storage_path)
        return res

    entry = find_localtuya_entry(storage)
    if entry is None:
        msg = (
            "LocalTuya integration not configured; bootstrap required "
            "before this tool can run."
        )
        res.errors.append(msg)
        log.error(msg)
        return res

    local = list_local_devices(storage)
    res.local_count = len(local)

    try:
        cloud_devs = cloud.list_devices()
    except Exception as e:
        res.errors.append(f"cloud list failed: {e}")
        log.error("cloud list failed: %s", e)
        return res
    res.cloud_count = len(cloud_devs)

    # Denylist combines the hard-coded Shannon-life-support guard with
    # any caller-supplied extras (env var TUYA_AUTOADD_NAME_DENYLIST).
    # `require_online=False` — Tuya cloud's online flag is empirically
    # unreliable (devices Fredrik just toggled in HA still report
    # `online=false`); rely on LAN-scan presence later in the cycle as
    # the real reachability gate.
    new = diff_devices(
        cloud_devs,
        local,
        denylist_ids=denylist_ids,
        denylist_name_substrings=denylist_name_substrings,
        require_online=False,
    )
    res.new_devices = list(new)
    if not new:
        log.info("no new devices to add (%s)", res.summary())
        return res

    log.info(
        "%d new cloud device(s) detected: %s",
        len(new),
        [(d.device_id[:8] + "...", d.name) for d in new],
    )

    # LAN scan to find IPs for the new devices. Only scans once per
    # cycle regardless of how many new devices — broadcast catches all.
    try:
        lan_ips = discover_lan_ips(timeout_sec=lan_scan_timeout)
    except Exception as e:
        log.warning("LAN scan failed (%s); proceeding without IPs", e)
        lan_ips = {}
    new = attach_lan_ips(new, lan_ips)

    # Filter by IP-presence if required.
    if require_lan_ip:
        with_ip = [d for d in new if d.ip]
        skipped = [d for d in new if not d.ip]
        for d in skipped:
            res.skipped_no_lan.append(d.device_id)
            log.info(
                "skipping %s (%s): no LAN IP yet, will retry next cycle",
                d.device_id[:8] + "...", d.name,
            )
        new = with_ip
        if not new:
            log.info("nothing to add this cycle (%s)", res.summary())
            return res

    # Per-device DP fetch + entry build. We do this serially to be
    # gentle on Tuya's cloud rate limits. Any single device's failure
    # doesn't block the others.
    new_entries: dict[str, dict] = {}
    for cd in new:
        try:
            dps = cloud.get_device_dps(cd.device_id)
        except Exception as e:
            res.errors.append(f"DP fetch failed for {cd.device_id}: {e}")
            log.error("DP fetch failed for %s: %s", cd.device_id, e)
            continue
        try:
            entry_dict = build_localtuya_entry(cd, dps)
            new_entries[cd.device_id] = entry_dict
        except Exception as e:
            res.errors.append(f"entry build failed for {cd.device_id}: {e}")
            log.error("entry build failed for %s: %s", cd.device_id, e)

    if not new_entries:
        log.warning("no entries built despite new devices found (%s)", res.summary())
        return res

    # Atomic storage write + reload HA. If reload fails, restore backup.
    updated = append_devices(storage, new_entries)
    backup = write_storage_atomic(updated, storage_path)
    try:
        entry_id = ha.find_config_entry_id("localtuya")
        if entry_id is None:
            raise RuntimeError("LocalTuya entry not visible via HA REST API")
        log.info(
            "reloading LocalTuya integration (entry_id=%s) to pick up %d new device(s)",
            entry_id, len(new_entries),
        )
        ha.reload_config_entry(entry_id)
    except (requests.HTTPError, requests.ConnectionError, RuntimeError) as e:
        res.errors.append(f"HA reload failed: {e}; restoring backup")
        log.error("HA reload failed: %s. Restoring backup %s", e, backup)
        restore_backup(backup, storage_path)
        return res

    # Wait a beat for HA to finish reload + entity creation; then
    # verify each new entity is queryable. This is observability, not
    # mutation — we don't roll back if entity-check fails (the user
    # may need a moment, or the device's entity type may differ from
    # our category guess).
    time.sleep(3)
    for dev_id, entry_dict in new_entries.items():
        res.added.append(dev_id)
        try:
            _verify_entity_appeared(ha, entry_dict)
        except Exception as e:
            log.warning(
                "could not verify entity for %s (%s) — added but state-check failed: %s",
                dev_id, entry_dict.get("friendly_name"), e,
            )

    log.info("cycle done (%s)", res.summary())
    return res


def _verify_entity_appeared(ha: HaClient, entry: dict) -> None:
    """Best-effort: ask HA for the new entity's state. The exact
    entity_id LocalTuya picks is `<platform>.<slug(friendly_name)>`,
    matching HA's default slugification."""
    platform = entry.get("platform", "light")
    friendly = entry.get("friendly_name", "")
    if not friendly:
        return
    from .core import _slugify
    candidate = f"{platform}.{_slugify(friendly)}"
    state = ha.get_state(candidate)
    if state is None:
        log.warning("entity %s not found after reload", candidate)
    else:
        log.info(
            "entity %s present, state=%s", candidate, state.get("state"),
        )


# ─── env-driven entry point ────────────────────────────────────────


def main() -> int:
    """Reads creds from env, runs one cycle, exits with status.

    Required env (sourced from /etc/default/tuya-autoadd on Shannon):
      HA_BASE_URL          (e.g., http://localhost:8123)
      HA_TOKEN             (long-lived bearer token)
      TUYA_CLIENT_ID       (Tuya IoT Cloud Project access_id)
      TUYA_CLIENT_SECRET   (Tuya IoT Cloud Project access_secret)
      TUYA_REGION          (eu / us / cn / in; default: eu)
      TUYA_USER_ID         (optional — LocalTuya stores this; used
                             when listing devices by user)
      STORAGE_PATH         (override; default /config/.storage/core.config_entries)
      LAN_SCAN_TIMEOUT     (seconds; default 12)
      REQUIRE_LAN_IP       (1=yes [default], 0=add even without IP)

    Logging goes to stdout/stderr — systemd captures both to journal.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not os.environ.get("HA_BASE_URL") or not os.environ.get("HA_TOKEN"):
        log.error("missing required env: HA_BASE_URL and/or HA_TOKEN")
        return 2

    storage_path = Path(os.environ.get("STORAGE_PATH", str(DEFAULT_STORAGE_PATH)))

    # Tuya creds: explicit env > LocalTuya storage. Storage fallback
    # is the *Source from the SSoT* path — LocalTuya already has the
    # creds, so we don't duplicate them in a second config file. Falls
    # back to env if storage doesn't have them (e.g., LocalTuya in
    # no_cloud mode, or first-bootstrap).
    client_id = os.environ.get("TUYA_CLIENT_ID")
    client_secret = os.environ.get("TUYA_CLIENT_SECRET")
    region = os.environ.get("TUYA_REGION")
    user_id = os.environ.get("TUYA_USER_ID")
    if not client_id or not client_secret:
        try:
            storage = load_config_entries(storage_path)
            entry = find_localtuya_entry(storage)
            if entry is not None:
                data = entry.get("data", {})
                client_id = client_id or data.get("client_id")
                client_secret = client_secret or data.get("client_secret")
                region = region or data.get("region")
                user_id = user_id or data.get("user_id")
                log.info("Tuya creds loaded from LocalTuya storage")
        except FileNotFoundError:
            pass
    if not client_id or not client_secret:
        log.error(
            "Tuya creds unavailable: neither env (TUYA_CLIENT_ID + "
            "TUYA_CLIENT_SECRET) nor LocalTuya storage has them"
        )
        return 2

    # tinytuya's Cloud requires a `apiDeviceID` (device-id, NOT user-id)
    # to bootstrap user-scoped API calls. Pull the first existing
    # LocalTuya device_id from storage as the bootstrap anchor — any
    # already-configured device will do (its uid lets Tuya scope the
    # devices-list query). Without this, getdevices returns Code 1106
    # 'permission deny' since the cloud project can't determine which
    # user to list.
    bootstrap_device_id = ""
    try:
        storage_for_bootstrap = load_config_entries(storage_path)
        entry_for_bootstrap = find_localtuya_entry(storage_for_bootstrap)
        if entry_for_bootstrap:
            existing = entry_for_bootstrap.get("data", {}).get("devices", {}) or {}
            if existing:
                bootstrap_device_id = next(iter(existing.keys()))
                log.info(
                    "bootstrap Tuya Cloud client via existing device_id=%s...",
                    bootstrap_device_id[:8],
                )
    except FileNotFoundError:
        pass

    cloud = TuyaCloudClient(
        client_id=client_id,
        client_secret=client_secret,
        region=region or "eu",
        # tinytuya treats apiDeviceID as a known device handle, not a
        # user_id. user_id stays available on the client for later
        # endpoints that take ?uid=… explicitly.
        bootstrap_device_id=bootstrap_device_id,
        user_id=user_id or "",
    )
    ha = HaClient(
        base_url=os.environ["HA_BASE_URL"],
        token=os.environ["HA_TOKEN"],
    )
    lan_timeout = int(os.environ.get("LAN_SCAN_TIMEOUT", "12"))
    require_lan_ip = os.environ.get("REQUIRE_LAN_IP", "1") not in ("0", "false", "False", "")
    # Denylist — env extras stack on top of the hard-coded Shannon
    # life-support guard. Comma-separated.
    denylist_ids_extra = frozenset(
        s.strip() for s in os.environ.get("TUYA_AUTOADD_DENY_IDS", "").split(",") if s.strip()
    )
    denylist_names_extra = tuple(
        s.strip() for s in os.environ.get("TUYA_AUTOADD_DENY_NAMES", "").split(",") if s.strip()
    )
    name_denylist = SHANNON_LIFE_SUPPORT_NAME_DENYLIST + denylist_names_extra

    try:
        res = run_once(
            cloud=cloud,
            ha=ha,
            storage_path=storage_path,
            lan_scan_timeout=lan_timeout,
            require_lan_ip=require_lan_ip,
            denylist_ids=denylist_ids_extra,
            denylist_name_substrings=name_denylist,
        )
    except Exception as e:
        log.exception("cycle crashed: %s", e)
        return 1

    if res.errors:
        log.error("cycle had errors: %s", res.errors)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
