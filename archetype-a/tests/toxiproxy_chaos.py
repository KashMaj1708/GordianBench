"""Toxiproxy chaos activation for Tier 2 tests."""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_TOXIPROXY_URL = os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
PROXY_NAME = "upstream"

# Tuned so plan's 5000ms timeout-bump band-aid fails Tier 2 (see phase_2_report).
# Minimum per-direction latency+jitter must push first-attempt round-trip past 5000ms:
#   commit(2200) + 2*(latency+jitter) > 5000  =>  latency+jitter > 1400
# Use 1800+400 (min 2200 per dir) so first attempt exceeds 5000ms even at low jitter draws.
CHAOS_TOXICS: list[dict[str, Any]] = [
    {
        "name": "chaos_latency_up",
        "type": "latency",
        "stream": "upstream",
        "toxicity": 1.0,
        "attributes": {"latency": 2000, "jitter": 200},
    },
    {
        "name": "chaos_latency_down",
        "type": "latency",
        "stream": "downstream",
        "toxicity": 1.0,
        "attributes": {"latency": 2000, "jitter": 200},
    },
    {
        "name": "chaos_bandwidth",
        "type": "bandwidth",
        "stream": "upstream",
        "toxicity": 1.0,
        "attributes": {"rate": 100},
    },
    {
        "name": "chaos_reset",
        "type": "reset_peer",
        "stream": "upstream",
        "toxicity": 0.08,
        "attributes": {"timeout": 0},
    },
]


def _api(base: str, method: str, path: str, **kwargs) -> requests.Response:
    url = f"{base.rstrip('/')}{path}"
    return requests.request(method, url, timeout=10, **kwargs)


def list_toxics(base: str = DEFAULT_TOXIPROXY_URL) -> list[dict[str, Any]]:
    resp = _api(base, "GET", f"/proxies/{PROXY_NAME}/toxics")
    resp.raise_for_status()
    return resp.json()


def clear_chaos(base: str = DEFAULT_TOXIPROXY_URL) -> None:
    for toxic in list_toxics(base):
        name = toxic.get("name")
        if name:
            _api(base, "DELETE", f"/proxies/{PROXY_NAME}/toxics/{name}")


def enable_chaos(base: str = DEFAULT_TOXIPROXY_URL) -> None:
    clear_chaos(base)
    for spec in CHAOS_TOXICS:
        resp = _api(base, "POST", f"/proxies/{PROXY_NAME}/toxics", json=spec)
        resp.raise_for_status()


def chaos_is_active(base: str = DEFAULT_TOXIPROXY_URL) -> bool:
    names = {t.get("name") for t in list_toxics(base)}
    return all(spec["name"] in names for spec in CHAOS_TOXICS)
