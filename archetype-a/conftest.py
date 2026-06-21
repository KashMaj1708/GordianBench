"""Pytest fixtures for Tier 1 regression tests."""

from __future__ import annotations

import pytest

from tests.helpers import (
    DEFAULT_DATABASE_URL,
    DEFAULT_GATEWAY_URL,
    clear_ledger,
    wait_for_gateway,
)


@pytest.fixture(scope="session")
def gateway_url() -> str:
    wait_for_gateway(DEFAULT_GATEWAY_URL)
    return DEFAULT_GATEWAY_URL


@pytest.fixture(scope="session")
def database_url() -> str:
    return DEFAULT_DATABASE_URL


@pytest.fixture
def clean_ledger(database_url: str):
    clear_ledger(database_url)
    yield


@pytest.fixture
def chaos_active():
    from tests.toxiproxy_chaos import clear_chaos, enable_chaos

    enable_chaos()
    yield
    clear_chaos()
