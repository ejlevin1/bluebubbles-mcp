"""Integration tests: contact and handle endpoints (read-only)."""

from __future__ import annotations

import pytest

from bb_mcp.client import BlueBubblesClient

pytestmark = pytest.mark.integration


async def test_get_contacts(client: BlueBubblesClient) -> None:
    contacts = await client.get_contacts()
    assert isinstance(contacts, list)


async def test_check_imessage_availability(
    client: BlueBubblesClient, private_api: bool
) -> None:
    if not private_api:
        pytest.skip("handle availability requires the Private API")
    # A registration lookup — reaches Apple, but sends nothing to the address.
    result = await client.check_imessage_availability("apple@apple.com")
    assert isinstance(result, dict)
    assert isinstance(result["available"], bool)


async def test_check_facetime_availability(
    client: BlueBubblesClient, private_api: bool
) -> None:
    if not private_api:
        pytest.skip("handle availability requires the Private API")
    result = await client.check_facetime_availability("apple@apple.com")
    assert isinstance(result, dict)
    assert isinstance(result["available"], bool)


async def test_check_imessage_availability_detects_registered_address(
    client: BlueBubblesClient, private_api: bool
) -> None:
    """A False for every input would satisfy the shape check above.

    Pin the positive case to the server's own iMessage address, which is
    registered by definition — otherwise a lookup that silently answered
    "unavailable" for everything would still pass.
    """
    if not private_api:
        pytest.skip("handle availability requires the Private API")
    info = await client.server_info()
    me = info.get("detected_imessage") or info.get("detected_icloud")
    if not me:
        pytest.skip("Server did not report a detected iMessage address")
    result = await client.check_imessage_availability(me)
    assert result["available"] is True
