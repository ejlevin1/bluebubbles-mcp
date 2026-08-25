"""Async client for the BlueBubbles REST API."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any, Final

import httpx

from bb_mcp.cursor import changed_bind_ns, exclusive_apple_ns


#: GUID batch size for :meth:`BlueBubblesClient.resolve_message_chats`. Well under
#: SQLite's 999-variable ceiling once the ``IN (:...guids)`` spread expands.
_GUID_CHUNK: Final = 400


def escape_like(term: str) -> str:
    r"""Escape ``\``, ``%`` and ``_`` for a LIKE pattern used with ``ESCAPE '\'``.

    Without this a query of ``%`` matches the entire table. Measured on a live server:
    ``text LIKE '%_%'`` returned every row (capped at 1000) where the escaped form
    returned 942.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_clause(query: str) -> dict[str, Any]:
    # Careful: `statement` is concatenated into SQL verbatim and only `args` are bound,
    # so the search term must never be interpolated here.
    #
    # Caveat for macOS 13+ servers with the Private API enabled: MessageRouter.query
    # DELETES the where-clause containing `message.text` and reroutes to
    # searchMessagesPrivateApi, parsing the bind variable out as
    # `statement.split(" ")[2]` and stripping the outer `%`. Under that path Spotlight
    # does the matching and this escaping silently becomes a no-op. The trailing
    # ESCAPE clause does not disturb `split(" ")[2]`, so it is safe to send either way.
    return {
        "statement": "message.text LIKE :query ESCAPE '\\'",
        "args": {"query": f"%{escape_like(query)}%"},
    }


def _sender_clauses(handle_address: str | None, from_me: bool) -> list[dict[str, Any]]:
    """Build the sender filter.

    ``handle`` carries the *other* party, never the account owner, so
    ``handle.id = <my own address>`` matches nothing at all — verified live: 0 rows
    against 85 outgoing messages in the same window. Filtering to the user's own
    messages has to go through ``message.is_from_me``.
    """
    if from_me:
        return [{"statement": "message.is_from_me = :fromMe", "args": {"fromMe": 1}}]
    if handle_address:
        return [
            {"statement": "handle.id = :address", "args": {"address": handle_address}}
        ]
    return []


class BlueBubblesClient:
    """Thin async wrapper around the BlueBubbles v1 REST API.

    Every request authenticates via the ``password`` query parameter.
    """

    def __init__(self, base_url: str, password: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._password = password
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    # -- internal helpers -----------------------------------------------------

    @staticmethod
    def _normalize_guid(chat_guid: str) -> str:
        """Normalize chat GUIDs: replace iMessage;-; prefix with any;-; for API compatibility."""
        return chat_guid.replace("iMessage;-;", "any;-;", 1)

    def _url(self, path: str) -> str:
        return f"{self._base_url}/api/v1{path}"

    def _auth_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"password": self._password}
        if extra:
            params.update(extra)
        return params

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.get(self._url(path), params=self._auth_params(params))
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    async def _post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self._http.post(
            self._url(path), json=json, params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    async def _delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.delete(
            self._url(path), params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    async def _put(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self._http.put(
            self._url(path), json=json, params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    # -- server ---------------------------------------------------------------

    async def ping(self) -> Any:
        return await self._get("/ping")

    async def server_info(self) -> Any:
        return await self._get("/server/info")

    # -- chats ----------------------------------------------------------------

    async def list_chats(
        self,
        limit: int = 25,
        offset: int = 0,
        sort: str = "lastmessage",
        with_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
        }
        if with_fields:
            body["with"] = with_fields
        return await self._post("/chat/query", json=body)

    async def get_chat(
        self, chat_guid: str, with_fields: list[str] | None = None
    ) -> dict[str, Any]:
        chat_guid = self._normalize_guid(chat_guid)
        params: dict[str, Any] = {}
        if with_fields:
            params["with"] = ",".join(with_fields)
        return await self._get(f"/chat/{chat_guid}", params=params)

    async def get_chat_messages(
        self,
        chat_guid: str,
        limit: int = 25,
        offset: int = 0,
        sort: str = "DESC",
        after: int | None = None,
        before: int | None = None,
        handle_address: str | None = None,
        from_me: bool = False,
    ) -> list[dict[str, Any]]:
        chat_guid = self._normalize_guid(chat_guid)
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "with": "attachment",
        }
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        messages = await self._get(f"/chat/{chat_guid}/message", params=params)
        if from_me:
            # `handle` names the OTHER party even on outgoing messages, so matching it
            # against the account owner's own address never hits. Use the flag instead.
            return [m for m in messages if m.get("isFromMe")]
        if handle_address:
            messages = [
                m
                for m in messages
                if (m.get("handle") or {}).get("address") == handle_address
            ]
        return messages

    async def mark_chat_read(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{self._normalize_guid(chat_guid)}/read")

    async def mark_chat_unread(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{self._normalize_guid(chat_guid)}/unread")

    async def start_typing(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{self._normalize_guid(chat_guid)}/typing")

    async def stop_typing(self, chat_guid: str) -> Any:
        return await self._delete(f"/chat/{self._normalize_guid(chat_guid)}/typing")

    async def delete_chat(self, chat_guid: str) -> Any:
        return await self._delete(f"/chat/{self._normalize_guid(chat_guid)}")

    async def rename_group(self, chat_guid: str, display_name: str) -> Any:
        chat_guid = self._normalize_guid(chat_guid)
        return await self._put(f"/chat/{chat_guid}", json={"displayName": display_name})

    async def add_participant(self, chat_guid: str, address: str) -> Any:
        chat_guid = self._normalize_guid(chat_guid)
        return await self._post(
            f"/chat/{chat_guid}/participant/add", json={"address": address}
        )

    async def remove_participant(self, chat_guid: str, address: str) -> Any:
        chat_guid = self._normalize_guid(chat_guid)
        return await self._post(
            f"/chat/{chat_guid}/participant/remove", json={"address": address}
        )

    async def leave_chat(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{self._normalize_guid(chat_guid)}/leave")

    # -- messages -------------------------------------------------------------

    async def send_message(
        self,
        chat_guid: str,
        message: str,
        method: str = "apple-script",
        subject: str | None = None,
        reply_to_guid: str | None = None,
    ) -> dict[str, Any]:
        chat_guid = self._normalize_guid(chat_guid)
        body: dict[str, Any] = {
            "chatGuid": chat_guid,
            "tempGuid": f"temp-{uuid.uuid4().hex}",
            "message": message,
            "method": method,
        }
        if subject:
            body["subject"] = subject
        if reply_to_guid and method != "apple-script":
            body["selectedMessageGuid"] = reply_to_guid
        return await self._post("/message/text", json=body)

    async def send_message_to_address(
        self,
        address: str,
        message: str,
        service: str = "iMessage",
        method: str = "apple-script",
    ) -> dict[str, Any]:
        if method == "apple-script":
            # /chat/new requires Private API; use /message/text with the
            # canonical any;-;<address> GUID for 1:1 chats instead.
            return await self.send_message(f"any;-;{address}", message, method=method)
        body: dict[str, Any] = {
            "addresses": [address],
            "message": message,
            "method": method,
            "service": service,
            "tempGuid": f"temp-{uuid.uuid4().hex}",
        }
        return await self._post("/chat/new", json=body)

    async def send_reaction(
        self,
        chat_guid: str,
        message_guid: str,
        reaction: str,
        part_index: int = 0,
    ) -> Any:
        body: dict[str, Any] = {
            "chatGuid": self._normalize_guid(chat_guid),
            "selectedMessageGuid": message_guid,
            "reaction": reaction,
            "partIndex": part_index,
        }
        return await self._post("/message/react", json=body)

    async def edit_message(
        self,
        message_guid: str,
        new_text: str,
        backwards_compat: str | None = None,
        part_index: int = 0,
    ) -> Any:
        body: dict[str, Any] = {
            "editedMessage": new_text,
            "backwardsCompatibilityMessage": backwards_compat
            or f"Edited to: {new_text}",
            "partIndex": part_index,
        }
        return await self._post(f"/message/{message_guid}/edit", json=body)

    async def unsend_message(self, message_guid: str, part_index: int = 0) -> Any:
        return await self._post(
            f"/message/{message_guid}/unsend", json={"partIndex": part_index}
        )

    async def search_messages(
        self,
        query: str | None = None,
        chat_guid: str | None = None,
        limit: int = 25,
        offset: int = 0,
        sort: str = "DESC",
        after: int | None = None,
        before: int | None = None,
        handle_address: str | None = None,
        from_me: bool = False,
    ) -> list[dict[str, Any]]:
        """Search messages via ``POST /message/query``.

        ``with`` deliberately omits ``chats``: that join is an INNER join and silently
        drops messages belonging to no chat row — measured at 4.70% over a 30-day
        window on a live server, and the dropped rows are real (SMS shortcodes, 2FA
        senders, marketing). Resolve chat labels separately with
        :meth:`resolve_message_chats`.

        Note that ``message.text`` is the *stored* column. Messages whose body lives
        only in ``attributedBody`` are invisible to a ``LIKE`` filter even though the
        server backfills ``text`` via ``universalText()`` when serializing the
        response, so an empty result is not proof of absence.
        """
        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "with": ["attachment"],
        }
        if chat_guid:
            body["chatGuid"] = self._normalize_guid(chat_guid)
        if after is not None:
            body["after"] = after
        if before is not None:
            body["before"] = before
        where: list[dict[str, Any]] = []
        if query:
            where.append(_like_clause(query))
        where.extend(_sender_clauses(handle_address, from_me))
        if where:
            body["where"] = where
        return await self._post("/message/query", json=body)

    async def query_created_since(
        self,
        *,
        since_ms: int,
        limit: int,
        chat_guid: str | None = None,
        handle_address: str | None = None,
        from_me: bool = False,
    ) -> list[dict[str, Any]]:
        """Messages created strictly after ``since_ms`` (Unix ms), oldest first.

        Matches and sorts on the *same* column, which is what makes a truncated page
        resumable: the caller can always continue from the frontier without rewinding.

        Pass ``limit + 1`` and let :func:`bb_mcp.cursor.advance_created` trim — that is
        how truncation is detected.
        """
        body: dict[str, Any] = {
            "limit": limit,
            "offset": 0,
            "sort": "ASC",
            "with": ["attachment"],
            "where": [
                {
                    "statement": "message.date > :createdAfter",
                    "args": {"createdAfter": exclusive_apple_ns(since_ms)},
                },
                *_sender_clauses(handle_address, from_me),
            ],
        }
        if chat_guid:
            body["chatGuid"] = self._normalize_guid(chat_guid)
        return await self._post("/message/query", json=body)

    async def query_changed_since(
        self,
        *,
        since_ms: int,
        limit: int,
        chat_guid: str | None = None,
        handle_address: str | None = None,
        from_me: bool = False,
    ) -> list[dict[str, Any]]:
        """Messages edited or unsent strictly after ``since_ms`` (Unix ms).

        There is no route that returns *updated* messages — ``after``/``before`` filter
        ``message.date`` only, and ``applyMessageUpdateDateQuery`` is reachable solely
        from ``GET /message/count/updated``, which returns a count. A raw ``where`` is
        the only way to see edits and unsends.

        The bind parameter is repeated across both references, which the server binds
        correctly. It goes through :func:`changed_bind_ns` rather than
        :func:`exclusive_apple_ns` because never-edited rows store ``0`` and a negative
        bind would match every one of them.
        """
        body: dict[str, Any] = {
            "limit": limit,
            "offset": 0,
            "sort": "ASC",
            "with": ["attachment"],
            "where": [
                {
                    "statement": (
                        "(message.date_edited > :changedAfter"
                        " OR message.date_retracted > :changedAfter)"
                    ),
                    "args": {"changedAfter": changed_bind_ns(since_ms)},
                },
                *_sender_clauses(handle_address, from_me),
            ],
        }
        if chat_guid:
            body["chatGuid"] = self._normalize_guid(chat_guid)
        return await self._post("/message/query", json=body)

    async def resolve_message_chats(
        self, message_guids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Map message GUID -> its chats, for rows already fetched.

        This is the second pass that replaces ``with: ["chats"]`` on the primary query.
        GUIDs absent from the result have no chat row — the INNER join drops them — and
        belong in the caller's explicit "no chat" bucket rather than vanishing.

        Uses the TypeORM spread form ``IN (:...guids)``; a plain ``IN (:guids)`` is a
        500. Returns ``{}`` without issuing a request for an empty input, since
        ``limit=0`` is a 400 and ``IN ()`` is a syntax error.
        """
        unique = list(dict.fromkeys(g for g in message_guids if g))
        if not unique:
            return {}
        chunks = [
            unique[i : i + _GUID_CHUNK] for i in range(0, len(unique), _GUID_CHUNK)
        ]
        responses = await asyncio.gather(
            *(self._resolve_chat_chunk(chunk) for chunk in chunks)
        )
        resolved: dict[str, list[dict[str, Any]]] = {}
        for response in responses:
            resolved.update(response)
        return resolved

    async def _resolve_chat_chunk(
        self, guids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        rows = await self._post(
            "/message/query",
            json={
                "limit": _GUID_CHUNK,
                "offset": 0,
                "sort": "ASC",
                "with": ["chat"],
                "where": [
                    {
                        "statement": "message.guid IN (:...guids)",
                        "args": {"guids": guids},
                    }
                ],
            },
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            guid = row.get("guid")
            if not guid:
                continue
            chats = row.get("chats")
            if chats is None:
                single = row.get("chat")
                chats = [single] if isinstance(single, dict) else []
            out[guid] = chats
        return out

    async def get_message(self, message_guid: str) -> dict[str, Any]:
        return await self._get(
            f"/message/{message_guid}",
            params={"with": "chats,attachments"},
        )

    # -- contacts -------------------------------------------------------------

    async def get_contacts(self) -> list[dict[str, Any]]:
        return await self._get("/contact")

    async def query_contacts(self, addresses: list[str]) -> list[dict[str, Any]]:
        return await self._post("/contact/query", json={"addresses": addresses})

    # -- handles --------------------------------------------------------------

    async def check_imessage_availability(self, address: str) -> Any:
        return await self._get(
            "/handle/availability/imessage", params={"address": address}
        )

    async def check_facetime_availability(self, address: str) -> Any:
        return await self._get(
            "/handle/availability/facetime", params={"address": address}
        )

    # -- attachments ----------------------------------------------------------

    async def get_attachment(self, attachment_guid: str) -> dict[str, Any]:
        return await self._get(f"/attachment/{attachment_guid}")

    async def download_attachment(self, attachment_guid: str) -> bytes:
        resp = await self._http.get(
            self._url(f"/attachment/{attachment_guid}/download"),
            params=self._auth_params({"original": "true"}),
        )
        resp.raise_for_status()
        return resp.content

    async def send_attachment(
        self,
        chat_guid: str,
        file_data: bytes,
        filename: str,
        mime_type: str = "application/octet-stream",
        method: str = "apple-script",
    ) -> dict[str, Any]:
        resp = await self._http.post(
            self._url("/message/attachment"),
            params=self._auth_params(),
            data={
                "chatGuid": self._normalize_guid(chat_guid),
                "tempGuid": f"temp-{uuid.uuid4().hex}",
                "method": method,
                "name": filename,
            },
            files={"attachment": (filename, file_data, mime_type)},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    # -- scheduled messages ---------------------------------------------------

    async def list_scheduled_messages(self) -> list[dict[str, Any]]:
        return await self._get("/message/schedule")

    async def create_scheduled_message(
        self,
        chat_guid: str,
        message: str,
        scheduled_for: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chatGuid": self._normalize_guid(chat_guid),
            "message": message,
            "scheduledFor": scheduled_for,
            "tempGuid": f"temp-{uuid.uuid4().hex}",
        }
        return await self._post("/message/schedule", json=body)

    async def delete_scheduled_message(self, schedule_id: int) -> Any:
        return await self._delete(f"/message/schedule/{schedule_id}")


class BlueBubblesError(Exception):
    def __init__(
        self, message: str, response_body: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.response_body = response_body
