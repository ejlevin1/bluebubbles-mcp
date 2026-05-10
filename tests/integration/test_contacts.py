"""Integration tests: contact and handle endpoints (read-only)."""

from __future__ import annotations

import pytest

from bb_mcp.client import BlueBubblesClient

pytestmark = pytest.mark.integration


async def test_get_contacts(client: BlueBubblesClient) -> None:
    contacts = await client.get_contacts()
    assert isinstance(contacts, list)


async def test_check_imessage_availability(client: BlueBubblesClient) -> None:
    # Use Apple's own test address — won't cause any side effects
    result = await client.check_imessage_availability("apple@apple.com")
    assert isinstance(result, bool)


async def test_check_facetime_availability(client: BlueBubblesClient) -> None:
    result = await client.check_facetime_availability("apple@apple.com")
    assert isinstance(result, bool)
