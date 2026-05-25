"""tuya-autoadd — automate Tuya device addition to HA LocalTuya.

Public exports are the data classes + pure functions used in the auto-add
pipeline. The IO-shaped functions live in submodules (cloud.py, ha.py,
storage.py).
"""

from .core import (
    CloudDevice,
    LocalTuyaDevice,
    diff_devices,
    build_localtuya_entry,
)

__all__ = [
    "CloudDevice",
    "LocalTuyaDevice",
    "diff_devices",
    "build_localtuya_entry",
]
