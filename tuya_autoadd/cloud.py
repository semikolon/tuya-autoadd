"""Tuya Cloud API client.

Wraps tinytuya's `Cloud` class to return our `CloudDevice` shape. We
keep tinytuya at the edge so the rest of the codebase doesn't care
which library we're using — easy to swap if tinytuya goes stale.
"""

from __future__ import annotations

import logging
from typing import Iterable

import tinytuya

from .core import CloudDevice, DpHint

log = logging.getLogger(__name__)


class TuyaCloudClient:
    """Single-instance wrapper around tinytuya's cloud auth + queries.

    Args mirror what LocalTuya stores in its `core.config_entries`
    entry: `client_id`, `client_secret`, `region`, `user_id`. We can
    share creds with LocalTuya — same Tuya IoT cloud project."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        region: str = "eu",
        bootstrap_device_id: str = "",
        user_id: str = "",
    ) -> None:
        # tinytuya's region maps: eu = Europe Central (Frankfurt), us = US
        # West (Oregon), cn = China, in = India. xZetsubou's LocalTuya
        # stores the bare region code (e.g., "eu").
        #
        # `bootstrap_device_id` is a known device-id from LocalTuya's
        # storage. tinytuya's getdevices() needs a device anchor to
        # resolve which user's devices to enumerate (without it, the
        # /v1.0/users/<uid>/devices endpoint returns 1106 permission
        # deny). user_id alone isn't enough — Tuya's REST signing needs
        # an explicit device anchor at init time.
        self._cloud = tinytuya.Cloud(
            apiRegion=region,
            apiKey=client_id,
            apiSecret=client_secret,
            apiDeviceID=bootstrap_device_id or None,
        )
        self._user_id = user_id

    def list_devices(self) -> list[CloudDevice]:
        """Pull every device in the linked Smart Life account.

        Returns CloudDevice rows with name + category + local_key +
        product info. LAN IP is set to None here — IPs are filled in
        by `discover.discover_lan_ips` in a separate pass (cloud
        doesn't know LAN topology).
        """
        raw = self._cloud.getdevices()
        if not isinstance(raw, list):
            # tinytuya returns a dict on error
            raise RuntimeError(f"Tuya Cloud getdevices failed: {raw!r}")
        out: list[CloudDevice] = []
        for d in raw:
            try:
                out.append(self._row_to_cloud_device(d))
            except Exception as e:
                log.warning(
                    "skipping cloud device row (id=%s name=%s): %s",
                    d.get("id"),
                    d.get("name"),
                    e,
                )
        return out

    @staticmethod
    def _row_to_cloud_device(d: dict) -> CloudDevice:
        # tinytuya returns lowercase keys per tinytuya's normalization.
        # Defensive .get() with sane defaults so a sparse cloud row
        # still yields something diff-comparable.
        return CloudDevice(
            device_id=d["id"],
            name=d.get("name", "unnamed"),
            category=d.get("category", ""),
            product_id=d.get("product_id", ""),
            product_name=d.get("product_name", ""),
            local_key=d.get("key", ""),
            online=bool(d.get("online", False)),
            ip=d.get("ip") or None,
        )

    def get_device_dps(self, device_id: str) -> list[DpHint]:
        """Fetch the Tuya specification (DP list) for one device.

        Returns DpHint rows — one per data point. Used by
        `build_localtuya_entry` to map DPs to LocalTuya roles.

        Tuya's specification endpoint returns both `functions` (writable
        DPs) and `status` (readable DPs); we merge them, preferring
        functions when a DP appears in both (functions has the more
        precise type info)."""
        spec = self._cloud.getdps(device_id)
        if not isinstance(spec, dict):
            raise RuntimeError(f"getdps({device_id}) failed: {spec!r}")
        # tinytuya normalizes to {"result": {"functions": [...], "status": [...]}}
        result = spec.get("result", spec)
        functions = result.get("functions", []) or []
        status = result.get("status", []) or []
        seen: dict[int, DpHint] = {}
        for src in (status, functions):  # functions takes precedence
            for item in src:
                try:
                    dp_id = int(item["dp_id"])
                    seen[dp_id] = DpHint(
                        dp_id=dp_id,
                        code=item.get("code", ""),
                        type=item.get("type", ""),
                        values=_parse_values_field(item.get("values")),
                    )
                except (KeyError, ValueError) as e:
                    log.warning(
                        "skipping DP row for device=%s: %s (row=%s)",
                        device_id, e, item,
                    )
        return sorted(seen.values(), key=lambda d: d.dp_id)


def _parse_values_field(values: object) -> dict | None:
    """Tuya returns `values` as either a JSON string or a dict
    depending on endpoint. Normalize."""
    if values is None:
        return None
    if isinstance(values, dict):
        return values
    if isinstance(values, str):
        import json
        try:
            return json.loads(values)
        except json.JSONDecodeError:
            return {"raw": values}
    return {"raw": values}
