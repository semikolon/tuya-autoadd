"""LAN device discovery — find a Tuya device's IP on the local network.

The Tuya cloud knows a device's `device_id` + `local_key` but not its
LAN IP (cloud only sees the device through Tuya's WAN tunnel). To
control a device locally, LocalTuya needs the IP.

Tuya devices broadcast their presence on UDP port 6666 (legacy) and
6667 (encrypted, 3.3+ protocol). tinytuya's `deviceScan` listens to
both and returns a dict of discovered devices keyed by IP.

We use tinytuya's scanner with a short timeout; if a device isn't
discovered in one scan pass, the daemon retries next cycle.
"""

from __future__ import annotations

import logging
from typing import Iterable

import tinytuya

from .core import CloudDevice

log = logging.getLogger(__name__)


def discover_lan_ips(timeout_sec: int = 12) -> dict[str, str]:
    """Return a {device_id: ip} map of every Tuya device tinytuya
    finds via UDP broadcast scan. `timeout_sec` is how long we listen
    before returning — 10-15s catches a typical broadcast cycle.

    Devices that haven't broadcast in this window are missing from
    the result; caller decides whether to skip-add or use a placeholder
    IP (LocalTuya supports the empty-host case too, but local control
    only works once the IP is known)."""
    log.info("scanning LAN for Tuya devices (timeout=%ss)", timeout_sec)
    raw = tinytuya.deviceScan(verbose=False, maxretry=timeout_sec, color=False)
    if not isinstance(raw, dict):
        log.warning("tinytuya.deviceScan returned %r; treating as empty", type(raw))
        return {}
    out: dict[str, str] = {}
    for ip, info in raw.items():
        dev_id = info.get("gwId") or info.get("id")
        if not dev_id:
            continue
        out[dev_id] = ip
    log.info("LAN scan found %d device(s): %s", len(out), list(out.values()))
    return out


def attach_lan_ips(
    cloud_devices: Iterable[CloudDevice],
    lan_ips: dict[str, str],
) -> list[CloudDevice]:
    """Return new CloudDevice instances with `ip` filled in from the
    LAN scan map. Cloud devices not in the map keep ip=None (caller
    decides whether to skip-add or proceed with empty host).

    Pure transformation; no side effects."""
    from dataclasses import replace
    out = []
    for cd in cloud_devices:
        ip = lan_ips.get(cd.device_id)
        if ip:
            out.append(replace(cd, ip=ip))
        else:
            out.append(cd)
    return out
