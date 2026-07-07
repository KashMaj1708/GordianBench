"""Toxiproxy control for the primary->replica replication stream.

Partitioning the "replication" proxy freezes the standby at its current LSN, so a
client that writes the primary and then reads the replica observes a stale value
once the read-after-write pin expires. This is the deterministic, controllable
cause of the stale-read invariant violation.
"""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_TOXIPROXY_URL = os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
PROXY_NAME = "replication"


def _api(base: str, method: str, path: str, **kwargs) -> requests.Response:
    url = f"{base.rstrip('/')}{path}"
    return requests.request(method, url, timeout=10, **kwargs)


def _set_enabled(enabled: bool, base: str = DEFAULT_TOXIPROXY_URL) -> None:
    resp = _api(base, "POST", f"/proxies/{PROXY_NAME}", json={"enabled": enabled})
    resp.raise_for_status()


def partition_replication(base: str = DEFAULT_TOXIPROXY_URL) -> None:
    """Cut the replication stream (standby stops applying WAL)."""
    _set_enabled(False, base)


def heal_replication(base: str = DEFAULT_TOXIPROXY_URL) -> None:
    """Restore the replication stream (standby reconnects and catches up)."""
    _set_enabled(True, base)


def list_toxics(base: str = DEFAULT_TOXIPROXY_URL) -> list[dict[str, Any]]:
    resp = _api(base, "GET", f"/proxies/{PROXY_NAME}/toxics")
    resp.raise_for_status()
    return resp.json()


def clear_lag(base: str = DEFAULT_TOXIPROXY_URL) -> None:
    for toxic in list_toxics(base):
        name = toxic.get("name")
        if name:
            _api(base, "DELETE", f"/proxies/{PROXY_NAME}/toxics/{name}")


def add_replication_lag(latency_ms: int = 3000, base: str = DEFAULT_TOXIPROXY_URL) -> None:
    """Add steady latency to the replication stream (lag without full partition).

    The toxic MUST be on the *downstream* stream: PostgreSQL streams WAL from the
    primary (upstream server) back to the standby (the proxy's client), so the WAL
    bytes travel downstream. An upstream toxic only delays the standby's small
    feedback messages and leaves WAL delivery unthrottled — the replica stays
    current and no stale read manifests.
    """
    clear_lag(base)
    resp = _api(
        base,
        "POST",
        f"/proxies/{PROXY_NAME}/toxics",
        json={
            "name": "repl_lag",
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": latency_ms, "jitter": 0},
        },
    )
    resp.raise_for_status()


def reset_replication(base: str = DEFAULT_TOXIPROXY_URL) -> None:
    """Clear toxics and re-enable the proxy (clean baseline)."""
    try:
        clear_lag(base)
    except Exception:
        pass
    heal_replication(base)
