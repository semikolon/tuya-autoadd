"""Pure data shapes + diff/build functions.

No IO. No HTTP. No file reads. Purity makes Phase-1 testable in
milliseconds with no fixtures more elaborate than dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class CloudDevice:
    """One Tuya cloud-device row, as returned by Tuya OpenAPI's
    ``/v1.0/iot-03/devices`` family of endpoints (or tinytuya's
    ``Cloud.getdevices()``).

    Only the fields we need for the auto-add pipeline; the cloud
    payload has many more we ignore.
    """

    device_id: str  # Tuya's globally-unique device ID (e.g., "bf2285403741...")
    name: str  # friendly_name from SmartLife app
    category: str  # Tuya category code (e.g., "dj" = light, "kg" = switch)
    product_id: str
    product_name: str  # English product description; useful for ICON_* mapping later
    local_key: str  # the LAN protocol key — required for LocalTuya
    online: bool  # True if Tuya cloud thinks the device is reachable
    ip: str | None = None  # LAN IP (None if not LAN-discoverable yet)


@dataclass(frozen=True)
class LocalTuyaDevice:
    """One LocalTuya device entry as stored in
    ``/config/.storage/core.config_entries`` under the ``localtuya``
    integration's ``data.devices`` dict.

    The keys we read from HA are a strict superset; we record the
    identity-relevant fields here for diffing. Building NEW entries
    needs ``build_localtuya_entry`` which fills the storage-required
    fields.
    """

    device_id: str
    friendly_name: str
    has_local_key: bool


def diff_devices(
    cloud: Iterable[CloudDevice],
    local: Iterable[LocalTuyaDevice],
    *,
    denylist_ids: frozenset[str] = frozenset(),
    denylist_name_substrings: tuple[str, ...] = (),
    require_online: bool = False,
) -> list[CloudDevice]:
    """Return cloud devices that aren't yet in LocalTuya, with safety
    exclusions applied. Idempotent.

    **Safety — denylist (per *self-referential life-support* directive)**:
    Some Tuya devices MUST NEVER be auto-added to on-Shannon HA. The
    canonical example is the Tuya plug that powers Shannon itself —
    auto-adding it would let HA on Shannon cut Shannon's own power,
    reproducing the 2026-05-18 03:00 hard-hang class. Two filter axes:
    - `denylist_ids`: explicit device_id strings
    - `denylist_name_substrings`: case-insensitive friendly-name
       substrings (the default-deny pattern "shannon" must be in the
       caller's argument set for the Shannon-power-plug guard).

    `require_online=False` by default because Tuya cloud's `online`
    field is empirically unreliable — devices that are actively
    responding to HA calls have been observed reporting `online=false`.
    Set True only when you trust the cloud's online flag for the
    specific deployment.
    """
    local_ids = {d.device_id for d in local}
    name_substrings_lower = tuple(s.lower() for s in denylist_name_substrings)
    out: list[CloudDevice] = []
    for cd in cloud:
        if cd.device_id in local_ids:
            continue
        if cd.device_id in denylist_ids:
            continue
        name_lc = cd.name.lower()
        if any(sub in name_lc for sub in name_substrings_lower):
            continue
        if require_online and not cd.online:
            continue
        out.append(cd)
    return out


# Hard-coded denylist for self-referential life-support — the Tuya
# plug that powers Shannon must NEVER be auto-added (any "Shannon"-
# named Tuya device shipped by Fredrik is the powering plug per the
# directive cluster). Substring match is case-insensitive. Future
# expansion: add other-fleet-machine powering plugs here if they ever
# land in the Smart Life account.
SHANNON_LIFE_SUPPORT_NAME_DENYLIST: tuple[str, ...] = ("shannon",)


# Tuya category code → LocalTuya entity-type hints. Used by
# build_localtuya_entry to seed sensible default DP mappings. The full
# DP mapping for a specific product comes from Tuya Cloud's "device
# specification" endpoint (`/v1.0/devices/{id}/specifications`); these
# are just the entity-class hints for the most common Tuya categories.
#
# Reference: Tuya's category code list lives at
# https://developer.tuya.com/en/docs/iot/standarddescription
TUYA_CATEGORY_TO_PLATFORM: dict[str, str] = {
    "dj": "light",  # Light (RGB / dimmable / etc.)
    "xdd": "light",  # Decorative light
    "dd": "light",  # LED strip
    "fwd": "light",  # Ambient light
    "tgkg": "light",  # Tunable / RGB combo
    "kg": "switch",  # Switch (wall switch, smart plug w/o energy)
    "cz": "switch",  # Smart plug (czechia code; also used for energy plugs)
    "pc": "switch",  # Power strip
    "wk": "climate",  # Thermostat (out of scope today)
    "wsdcg": "sensor",  # Multi-sensor (temp + humidity)
    "ckmkzq": "cover",  # Garage door (out of scope)
}


def category_to_platform(category: str) -> str:
    """Map Tuya category to HA platform name. Falls back to ``light``
    for unknown light-like codes (most consumer bulbs) but logs a
    warning the caller should surface."""
    return TUYA_CATEGORY_TO_PLATFORM.get(category, "light")


@dataclass(frozen=True)
class DpHint:
    """One data point (DP) of a Tuya device — what state field it
    represents, what type it is, how LocalTuya should expose it."""

    dp_id: int
    code: str  # Tuya's symbolic name (e.g., "switch_led", "bright_value_v2")
    type: str  # "bool" / "value" / "string" / "enum"
    values: dict | None = None  # for enums + value-ranges, normalized JSON


def build_localtuya_entry(
    cloud: CloudDevice,
    dps: list[DpHint],
    *,
    protocol_version: str = "3.4",
) -> dict:
    """Build a LocalTuya-storage-compatible device dict from a cloud
    device + its DP specification.

    Returns a dict matching the structure under
    ``core.config_entries[entries[localtuya]][data][devices][<device_id>]``.

    The DP mapping (which DP is the main on/off, which is brightness,
    etc.) is what makes LocalTuya's auto-configure value-add — we
    reproduce it deterministically from the DP code names so the
    output matches what the HA UI would produce for the same input.

    `protocol_version` defaults to 3.4 (most 2024+ Tuya bulbs).
    Adjust if a future device reports 3.3 / 3.5.
    """
    platform = category_to_platform(cloud.category)
    # Slugify name → entity friendly-id portion (LocalTuya stores
    # friendly_name as-is, HA derives entity_id from it; the slug
    # here is purely for stable key generation, not for the
    # user-visible field).
    slug = _slugify(cloud.name)
    entry: dict = {
        # device_id is BOTH the dict-key in daemon.py:174 AND a required
        # field inside the entry — LocalTuya's DeviceConfig.__post_init__
        # reads `device_config[CONF_DEVICE_ID]` and KeyErrors at HA boot
        # without it. Omitting this caused a silent data-corruption class:
        # autoadd would write the entry, HA would load it the first time
        # (no validator at write-path), then any subsequent HA restart
        # would crash the entire LocalTuya integration with a one-line
        # traceback hiding 9 healthy lights behind a single missing key.
        # Anchoring incident: Closet light (2026-05-25), broke ALL HA
        # lights for an unknown duration before detection.
        "device_id": cloud.device_id,
        "friendly_name": cloud.name,
        "host": cloud.ip or "",  # filled by LAN discovery later if missing
        "local_key": cloud.local_key,
        "protocol_version": protocol_version,
        "product_key": cloud.product_id,
        "platform": platform,
        # The auto-config DP mapping. Built by inspecting DP codes; if
        # a code we recognize is present, it gets the platform-specific
        # role (id_<role> → dp_id).
        **_map_dps_to_platform_roles(platform, dps),
        # Keep raw DPS list around for sensor exposure + debugging.
        "entities": _build_entities_list(slug, cloud, platform, dps),
    }
    return entry


def _slugify(s: str) -> str:
    """Lowercase + ASCII-safe + underscore-joined. Mirrors HA's
    default entity-id slugification closely enough for our keying."""
    out = []
    prev_us = False
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_")


def _map_dps_to_platform_roles(platform: str, dps: list[DpHint]) -> dict:
    """For lights: scan DP codes for the standard set (switch_led,
    bright_value, temp_value, colour_data) and emit ``id_<role>`` keys
    pointing at the matching dp_id. LocalTuya's storage uses this
    shape for the platform-specific entity to know which DP is which.

    Empty dict for non-light platforms (switch / sensor) for now —
    those have simpler mappings filled in `_build_entities_list`.
    """
    if platform != "light":
        return {}
    role_codes = {
        "id_main_dp": ("switch_led", "switch_led_1", "switch"),
        "id_brightness_dp": ("bright_value", "bright_value_v2", "bright_value_1"),
        "id_color_temp_dp": ("temp_value", "temp_value_v2", "colour_temp"),
        "id_color_dp": ("colour_data", "colour_data_v2", "colour_data_hsv"),
        "id_color_mode_dp": ("work_mode", "led_mode"),
    }
    mapping: dict = {}
    for role, codes in role_codes.items():
        for dp in dps:
            if dp.code in codes:
                mapping[role] = dp.dp_id
                break
    return mapping


def _build_entities_list(
    slug: str,
    cloud: CloudDevice,
    platform: str,
    dps: list[DpHint],
) -> list[dict]:
    """Build the per-entity list LocalTuya stores. For most bulbs this
    is a single light entity; switches get a single switch entity. We
    intentionally don't auto-expose every DP as a sensor — the kiosk
    cares about toggle, not telemetry."""
    if platform == "light":
        return [
            {
                "friendly_name": cloud.name,
                "id": _find_dp(dps, ("switch_led", "switch_led_1", "switch")) or 20,
                "platform": "light",
                "brightness": _find_dp(
                    dps, ("bright_value", "bright_value_v2", "bright_value_1")
                ),
                "color_temp": _find_dp(dps, ("temp_value", "temp_value_v2")),
                "color": _find_dp(dps, ("colour_data", "colour_data_v2")),
                "color_mode": _find_dp(dps, ("work_mode", "led_mode")),
            }
        ]
    if platform == "switch":
        return [
            {
                "friendly_name": cloud.name,
                "id": _find_dp(dps, ("switch_1", "switch")) or 1,
                "platform": "switch",
            }
        ]
    # Fallback: empty list. Caller logs the unsupported category.
    return []


def _find_dp(dps: list[DpHint], codes: tuple[str, ...]) -> int | None:
    """First DP whose code matches any in ``codes``; None if not found."""
    for dp in dps:
        if dp.code in codes:
            return dp.dp_id
    return None
