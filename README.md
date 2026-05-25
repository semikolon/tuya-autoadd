# tuya-autoadd

**Purpose**: Make "add a Tuya bulb in SmartLife app" → "appears + works in HA" require ZERO HA UI clicks.

Polls the Tuya Cloud API on a schedule, diffs against LocalTuya's currently configured devices, and adds any new ones to LocalTuya's `core.config_entries` storage. Triggers an HA reload to pick them up. Idempotent — running it when nothing's new is a no-op.

Built for Shannon (the IoT-hub Rock Pi 4B SE) but architecture-portable.

**Architectural origin**: Fredrik's 2026-05-21 directive — *"Tuya should always auto-update if I add devices in the SmartLife app... I want you/the system to fully self-update and just have the right settings automagically to the extent possible."* Honors *Forged steel* + *Track everything automatable* + *Deterministic infrastructure* + *Fix the system, never work around it* directive cluster.

**Test bed**: the Closet bulb added to SmartLife on 2026-05-21 evening — first deployment proves the chain end-to-end.

## Architecture

```
Tuya Cloud (Smart Life account)
      │ (poll every 5 min)
      ▼
tuya_autoadd.poll
      │
      ├─ list_cloud_devices() → cloud_devices: list[CloudDevice]
      │
      ├─ load_localtuya_devices() → local_devices: dict[id, LocalTuyaDevice]
      │
      ├─ diff() → new_devices: list[CloudDevice]
      │
      ▼ (for each new device)
build_localtuya_entry()  (cloud meta + local_key + DP probe → LocalTuya schema)
      │
      ▼
append_to_storage()  + HA reload
      │
      ▼
HA state: light.<closet> / switch.<...> appears
```

## TDD layers

- Phase 1 (unit, mocked): discovery + diff + entry-build
- Phase 2 (integration, real HA REST): storage I/O + reload trigger
- Phase 3 (end-to-end on Shannon): Closet bulb is the first test device
- Phase 4 (scheduling): systemd timer at 5min interval

## Status

In development 2026-05-21. Not yet deployed.
