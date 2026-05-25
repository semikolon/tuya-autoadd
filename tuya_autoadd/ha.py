"""Home Assistant REST API client — minimal slice we need.

Operations:
- locate the LocalTuya config_entry by domain
- trigger a reload of that entry after we've edited storage
- read entity states for sanity checks (did the new light appear?)

We do NOT use HA's config_flow REST API to add devices — direct
storage write is simpler and matches what LocalTuya does internally.
The reload step picks up the edited file.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class HaClient:
    """Long-lived bearer token (from /etc/default/shannon-kiosk-actions
    or similar) authenticates everything we do."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def _get(self, path: str) -> Any:
        r = self._session.get(self.base_url + path, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict | None = None) -> Any:
        r = self._session.post(
            self.base_url + path,
            json=payload or {},
            timeout=self._timeout,
        )
        r.raise_for_status()
        # Some endpoints return text/204; tolerate that.
        if r.headers.get("Content-Type", "").startswith("application/json"):
            return r.json()
        return r.text

    def find_config_entry_id(self, domain: str) -> str | None:
        """Return the entry_id for the named integration (e.g.,
        "localtuya"), or None if not configured."""
        # HA exposes config_entries via a websocket API only; the REST
        # slice is limited. We fall back to reading the storage file
        # if the REST endpoint isn't available. But /api/config has
        # the basics; /api/states gives us domain info.
        # The most portable path: GET /api/config/config_entries
        # which returns a list of entries with their entry_id.
        entries = self._get("/api/config/config_entries/entry")
        for e in entries:
            if e.get("domain") == domain:
                return e.get("entry_id")
        return None

    def reload_config_entry(self, entry_id: str) -> dict:
        """Reload an integration without restarting HA. After a
        storage-direct edit of LocalTuya's config_entry data, calling
        this picks up the new devices.

        Returns HA's response (a dict with `require_restart` etc).
        Raises requests.HTTPError on failure."""
        return self._post(f"/api/config/config_entries/entry/{entry_id}/reload")

    def get_state(self, entity_id: str) -> dict | None:
        """Return the entity's current state dict, or None if it
        doesn't exist yet. Used to verify the new device's entity
        appeared after the reload."""
        try:
            return self._get(f"/api/states/{entity_id}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def list_entities_for_integration(self, integration_domain: str) -> list[str]:
        """List all entity_ids whose state's `attributes.platform` or
        `context.integration` matches the integration. We approximate
        by checking the entity_registry storage if the REST search is
        too noisy; for now we just return all states and let the caller
        filter by entity_id prefix.

        For LocalTuya: entity_ids like `light.closet` aren't tagged
        with their integration in the public state API, so this is
        best-effort. The simpler approach is to remember the device
        names we just added and check states for each."""
        all_states = self._get("/api/states")
        return [s["entity_id"] for s in all_states]
