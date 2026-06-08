"""Phase 1 tests — pure data shapes + diff/build.

No HTTP. No file IO. Pure functions exercised against hand-crafted
fixtures so failures point at logic, not infrastructure."""

from tuya_autoadd.core import (
    CloudDevice,
    LocalTuyaDevice,
    DpHint,
    SHANNON_LIFE_SUPPORT_NAME_DENYLIST,
    diff_devices,
    build_localtuya_entry,
    category_to_platform,
    _slugify,
)


# ─── diff_devices ───────────────────────────────────────────────────


def make_cloud(device_id: str, name: str = "Test", online: bool = True) -> CloudDevice:
    return CloudDevice(
        device_id=device_id,
        name=name,
        category="dj",
        product_id="abc123",
        product_name="Test bulb",
        local_key="0123456789abcdef",
        online=online,
        ip="192.168.4.99",
    )


def make_local(device_id: str, name: str = "Test") -> LocalTuyaDevice:
    return LocalTuyaDevice(device_id=device_id, friendly_name=name, has_local_key=True)


class TestDiffDevices:
    def test_no_cloud_no_local_returns_empty(self):
        assert diff_devices([], []) == []

    def test_cloud_already_local_returns_empty(self):
        cloud = [make_cloud("dev1"), make_cloud("dev2")]
        local = [make_local("dev1"), make_local("dev2")]
        assert diff_devices(cloud, local) == []

    def test_one_new_cloud_device(self):
        cloud = [make_cloud("dev1"), make_cloud("dev2", name="Closet")]
        local = [make_local("dev1")]
        new = diff_devices(cloud, local)
        assert len(new) == 1
        assert new[0].device_id == "dev2"
        assert new[0].name == "Closet"

    def test_offline_devices_skipped_when_require_online(self):
        cloud = [make_cloud("dev_offline", online=False)]
        local: list[LocalTuyaDevice] = []
        # New behavior: require_online defaults to False (cloud's
        # online flag is unreliable; LAN-scan is the real gate). The
        # legacy filter is opt-in via require_online=True.
        assert diff_devices(cloud, local) == [
            make_cloud("dev_offline", online=False),
        ]
        assert diff_devices(cloud, local, require_online=True) == []

    def test_denylist_by_id(self):
        cloud = [make_cloud("dev_ok"), make_cloud("dev_blocked")]
        local: list[LocalTuyaDevice] = []
        out = diff_devices(cloud, local, denylist_ids=frozenset({"dev_blocked"}))
        assert [d.device_id for d in out] == ["dev_ok"]

    def test_denylist_by_name_substring(self):
        # Test the substring mechanism with an explicit substring (the
        # default constant is currently () after the 2026-06-08 plug
        # repurpose — Shannon is no longer on Tuya power. The mechanism
        # itself must keep working for future fleet life-support plugs.)
        cloud = [
            make_cloud("dev_a", name="Bedroom Light"),
            make_cloud("dev_b", name="Shannon"),  # case-insensitive
            make_cloud("dev_c", name="shannon"),  # lowercase
            make_cloud("dev_d", name="Office Lamp"),
        ]
        local: list[LocalTuyaDevice] = []
        out = diff_devices(
            cloud, local,
            denylist_name_substrings=("shannon",),
        )
        ids = [d.device_id for d in out]
        assert "dev_a" in ids
        assert "dev_d" in ids
        assert "dev_b" not in ids, "Substring match must be case-insensitive"
        assert "dev_c" not in ids, "Case-insensitive substring match required"

    def test_default_name_denylist_is_empty_after_shannon_offload(self):
        # 2026-06-08: SHANNON_LIFE_SUPPORT_NAME_DENYLIST went from
        # ("shannon",) to () after the bedroom TV-plug swap. A device
        # named "Shannon" SHOULD now be auto-added (no life-support
        # concern); the device-id denylist remains the durable safety
        # pattern for future fleet plugs.
        assert SHANNON_LIFE_SUPPORT_NAME_DENYLIST == ()
        cloud = [make_cloud("dev_shannon_plug", name="Shannon")]
        local: list[LocalTuyaDevice] = []
        out = diff_devices(
            cloud, local,
            denylist_name_substrings=SHANNON_LIFE_SUPPORT_NAME_DENYLIST,
        )
        assert [d.device_id for d in out] == ["dev_shannon_plug"]

    def test_denylist_combines_with_existing_local(self):
        cloud = [
            make_cloud("dev1"),
            make_cloud("dev2", name="Shannon"),
            make_cloud("dev3"),
        ]
        local = [make_local("dev1")]
        # Explicit substring exercises the combined skip-paths (local + denylist).
        out = diff_devices(
            cloud, local,
            denylist_name_substrings=("shannon",),
        )
        # dev1 in local (skip), dev2 denylisted (skip), dev3 the only add
        assert [d.device_id for d in out] == ["dev3"]

    def test_idempotent(self):
        cloud = [make_cloud("dev1"), make_cloud("dev2"), make_cloud("dev3", online=False)]
        local = [make_local("dev1")]
        run1 = diff_devices(cloud, local, require_online=True)
        run2 = diff_devices(cloud, local, require_online=True)
        assert run1 == run2
        # And dev3 (offline) is skipped both runs (require_online=True):
        assert all(d.device_id != "dev3" for d in run1)


# ─── category_to_platform ──────────────────────────────────────────


class TestCategoryToPlatform:
    def test_dj_is_light(self):
        assert category_to_platform("dj") == "light"

    def test_kg_is_switch(self):
        assert category_to_platform("kg") == "switch"

    def test_unknown_falls_back_to_light(self):
        # Most consumer bulbs land in a Tuya light-like category; light
        # is the safe fallback for unknown codes.
        assert category_to_platform("zzz_unknown") == "light"


# ─── _slugify ──────────────────────────────────────────────────────


class TestSlugify:
    def test_lowercase_with_underscores(self):
        assert _slugify("Closet Bulb") == "closet_bulb"

    def test_collapses_runs(self):
        assert _slugify("Foo  Bar   Baz") == "foo_bar_baz"

    def test_strips_punctuation(self):
        assert _slugify("Gold Light #2 (RGB)") == "gold_light_2_rgb"

    def test_trims_edges(self):
        assert _slugify("  trim me  ") == "trim_me"


# ─── build_localtuya_entry ─────────────────────────────────────────


CLOSET_BULB_DPS = [
    DpHint(dp_id=20, code="switch_led", type="bool"),
    DpHint(dp_id=21, code="work_mode", type="enum",
           values={"range": ["white", "colour", "scene", "music"]}),
    DpHint(dp_id=22, code="bright_value_v2", type="value",
           values={"min": 10, "max": 1000}),
    DpHint(dp_id=23, code="temp_value_v2", type="value",
           values={"min": 0, "max": 1000}),
    DpHint(dp_id=24, code="colour_data_v2", type="string"),
]


class TestBuildLocalTuyaEntry:
    def test_closet_bulb_basic_shape(self):
        cloud = make_cloud("bfabcde12345", name="Closet")
        entry = build_localtuya_entry(cloud, CLOSET_BULB_DPS)
        assert entry["friendly_name"] == "Closet"
        assert entry["host"] == "192.168.4.99"
        assert entry["local_key"] == "0123456789abcdef"
        assert entry["protocol_version"] == "3.4"
        assert entry["product_key"] == "abc123"
        assert entry["platform"] == "light"

    def test_entry_includes_all_localtuya_required_fields(self):
        """Regression guard for the 2026-05-25 Closet incident.

        LocalTuya's `DeviceConfig.__post_init__` (in const.py) hard-reads
        five fields with `device_config[CONF_*]` (raises KeyError if
        missing). Omitting any of them silently writes a corrupt entry
        that HA loads once then crashes the entire integration on every
        subsequent restart, hiding all OTHER healthy devices behind one
        missing key.
        """
        cloud = make_cloud("bfabcde12345", name="Closet")
        entry = build_localtuya_entry(cloud, CLOSET_BULB_DPS)
        for required in ("device_id", "host", "local_key",
                         "protocol_version", "entities"):
            assert required in entry, (
                f"entry is missing required LocalTuya field: {required!r}; "
                f"this WILL crash the integration on next HA restart "
                f"(KeyError in DeviceConfig.__post_init__)"
            )
        # device_id MUST match the cloud-source's device_id — daemon.py
        # uses it as the dict KEY in core.config_entries; the inner field
        # must match the key or LocalTuya's storage migration drifts.
        assert entry["device_id"] == "bfabcde12345"

    def test_dp_role_mapping_light(self):
        cloud = make_cloud("bfabcde12345", name="Closet")
        entry = build_localtuya_entry(cloud, CLOSET_BULB_DPS)
        assert entry["id_main_dp"] == 20
        assert entry["id_brightness_dp"] == 22
        assert entry["id_color_temp_dp"] == 23
        assert entry["id_color_dp"] == 24
        assert entry["id_color_mode_dp"] == 21

    def test_entities_list_for_light(self):
        cloud = make_cloud("bfabcde12345", name="Closet")
        entry = build_localtuya_entry(cloud, CLOSET_BULB_DPS)
        assert len(entry["entities"]) == 1
        ent = entry["entities"][0]
        assert ent["platform"] == "light"
        assert ent["friendly_name"] == "Closet"
        assert ent["id"] == 20  # switch_led
        assert ent["brightness"] == 22
        assert ent["color_temp"] == 23
        assert ent["color"] == 24
        assert ent["color_mode"] == 21

    def test_simple_switch_no_brightness(self):
        cloud = CloudDevice(
            device_id="bf_switch_99",
            name="Closet Plug",
            category="kg",
            product_id="x",
            product_name="Smart Plug",
            local_key="0123456789abcdef",
            online=True,
            ip="192.168.4.100",
        )
        dps = [DpHint(dp_id=1, code="switch_1", type="bool")]
        entry = build_localtuya_entry(cloud, dps)
        assert entry["platform"] == "switch"
        assert entry["entities"][0]["platform"] == "switch"
        assert entry["entities"][0]["id"] == 1
        # Role-mapping shouldn't pollute switch entries with light-only keys:
        assert "id_brightness_dp" not in entry
        assert "id_color_dp" not in entry

    def test_protocol_version_override(self):
        cloud = make_cloud("dev1")
        entry = build_localtuya_entry(cloud, CLOSET_BULB_DPS, protocol_version="3.3")
        assert entry["protocol_version"] == "3.3"

    def test_missing_ip_leaves_host_empty(self):
        cloud = CloudDevice(
            device_id="bf_no_ip", name="Test", category="dj",
            product_id="x", product_name="bulb", local_key="0123456789abcdef",
            online=True, ip=None,
        )
        entry = build_localtuya_entry(cloud, CLOSET_BULB_DPS)
        assert entry["host"] == ""

    def test_missing_dp_codes_handled(self):
        """A bulb that only exposes switch_led + bright_value (no color)
        gets light platform but no color/temp/color_mode keys."""
        cloud = make_cloud("bf_dimmer", name="White-only bulb")
        dps = [
            DpHint(dp_id=20, code="switch_led", type="bool"),
            DpHint(dp_id=22, code="bright_value_v2", type="value"),
        ]
        entry = build_localtuya_entry(cloud, dps)
        assert entry["id_main_dp"] == 20
        assert entry["id_brightness_dp"] == 22
        # Optional roles unset:
        assert "id_color_temp_dp" not in entry
        assert "id_color_dp" not in entry
        ent = entry["entities"][0]
        assert ent["color_temp"] is None
        assert ent["color"] is None
