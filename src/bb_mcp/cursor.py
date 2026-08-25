"""Pure cursor arithmetic for incremental message polling.

No I/O, no imports from :mod:`bb_mcp.client` or :mod:`bb_mcp.server`. Everything here
is a plain function over plain data so it can be unit-tested directly — which matters,
because every subtle bug in incremental polling lives in this file.

Two facts about the BlueBubbles/chat.db wire format drive the whole design:

* Raw ``where`` bind arguments are **Apple-epoch nanoseconds**, while JSON responses
  carry **Unix milliseconds**. The server's date transformer does not fire for raw
  ``andWhere`` params, so callers must convert by hand.
* ``message.date`` carries sub-millisecond precision, but ``dateCreated`` in the
  response is ``floor(true_ns / 1e6)``. A cursor built from ``dateCreated`` therefore
  cannot address a position *inside* a millisecond.

Priority order everywhere below: **never skip a message; duplicates are acceptable.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Sequence

APPLE_EPOCH_MS: Final = 978_307_200_000
NS_PER_MS: Final = 1_000_000

#: Tolerance for a server clock that leads ours. Without it, a fast server clock makes
#: every real message look "future", leaving no watermark candidate and stalling the
#: axis permanently.
FUTURE_SKEW_MS: Final = 300_000

#: The server caps ``limit`` at 1000 and 400s anything above it. We always request
#: ``limit + 1`` to detect truncation, so the largest limit we may ask for is 999.
MAX_PAGE: Final = 999

#: Bumped when the encoded cursor layout changes, so an old cursor fails loudly in
#: :meth:`Cursor.parse` instead of being silently mis-read as the new layout.
CURSOR_VERSION: Final = "v1"

#: Below this, an all-digit ``since`` is read as epoch *seconds* rather than epoch ms.
#: 1e11 ms is 1973; 1e11 s is the year 5138. Nothing real is ambiguous.
_EPOCH_MS_FLOOR: Final = 100_000_000_000


# ---------------------------------------------------------------------------
# Apple epoch
# ---------------------------------------------------------------------------


def to_apple_ns(unix_ms: int) -> int:
    """Unix ms -> Apple-epoch ns at the START of that millisecond."""
    return (unix_ms - APPLE_EPOCH_MS) * NS_PER_MS


def from_apple_ns(apple_ns: int) -> int:
    """Apple-epoch ns -> Unix ms, floored — matching how the server serializes dates."""
    return apple_ns // NS_PER_MS + APPLE_EPOCH_MS


def exclusive_apple_ns(unix_ms: int) -> int:
    """Bind value for ``col > :t`` that excludes the ENTIRE millisecond ``unix_ms``.

    Only correct when every row inside ``unix_ms`` has already been delivered.
    :func:`advance_created` is what guarantees that.
    """
    return to_apple_ns(unix_ms) + (NS_PER_MS - 1)


def changed_bind_ns(unix_ms: int) -> int:
    """Bind value for the ``date_edited`` / ``date_retracted`` axis. Floored at 0.

    Never-edited rows store ``0`` in those columns. A pre-2001 watermark yields a
    NEGATIVE Apple-ns bind, and ``0 > negative`` is TRUE — so the changed query would
    match every message ever sent. Verified against a live server: a negative bind
    returned the whole table (1000, capped) where the floored bind returned the 40
    real edits.
    """
    return max(0, exclusive_apple_ns(unix_ms))


# ---------------------------------------------------------------------------
# Defensive coercion
# ---------------------------------------------------------------------------


def coerce_ms(value: Any) -> int | None:
    """Read a timestamp field as Unix ms, or ``None`` if it is not usable.

    ``None`` is returned for null, ``0`` (BlueBubbles' "never edited"/"never
    retracted" sentinel), booleans, empty strings, NaN/inf, and anything non-numeric.

    This exists because a string where an int was assumed once took down a whole poll
    with a ``TypeError`` inside ``max()``. Coercion failures must degrade to "not a
    watermark candidate", never to an exception.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value or None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return int(value) or None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text) or None
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return int(number) or None
    return None


def message_ms(row: dict[str, Any]) -> int | None:
    """``dateCreated`` of a message row, as Unix ms, or ``None``."""
    return coerce_ms(row.get("dateCreated"))


def change_times(row: dict[str, Any]) -> list[int]:
    """Usable ``dateEdited`` / ``dateRetracted`` values on a row. May be empty."""
    return [
        ms
        for ms in (
            coerce_ms(row.get("dateEdited")),
            coerce_ms(row.get("dateRetracted")),
        )
        if ms is not None
    ]


def is_future(unix_ms: int, now_ms: int) -> bool:
    """Is this timestamp implausibly ahead of our clock?

    One corrupt ``date_edited`` centuries in the future would otherwise become the
    watermark and skip every real message until then.
    """
    return unix_ms > now_ms + FUTURE_SKEW_MS


def clamp_to_now(unix_ms: int, now_ms: int) -> int:
    """Never let a watermark sit in the future."""
    return min(unix_ms, now_ms)


# ---------------------------------------------------------------------------
# The cursor value
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cursor:
    """Two independent monotonic watermarks, both Unix ms.

    ``created_ms`` tracks the ``message.date`` axis and ``changed_ms`` the
    ``date_edited``/``date_retracted`` axis. They cannot share one value: a message
    edited a moment ago may be years old, so it sorts near the FRONT by
    ``message.date``. A combined cursor derived from such a batch lands *behind* where
    the poll started, and the next poll returns the identical batch forever.
    """

    created_ms: int
    changed_ms: int

    def encode(self) -> str:
        """Render as the opaque token callers echo back."""
        return f"{CURSOR_VERSION}|{self.created_ms}|{self.changed_ms}"

    @classmethod
    def seed(cls, unix_ms: int) -> Cursor:
        """Start both axes from one timestamp."""
        ms = max(0, int(unix_ms))
        return cls(created_ms=ms, changed_ms=ms)

    @classmethod
    def parse(cls, raw: str | int, *, now_ms: int) -> Cursor:
        """Inverse of :meth:`encode`, plus the human-friendly seed forms.

        Accepted: an encoded cursor, epoch ms, epoch seconds, or an ISO-8601
        date/datetime (a trailing ``Z`` is tolerated, naive values are read as UTC).

        Anything else raises :class:`ValueError`. It deliberately never falls back to
        "now" or "the beginning of time" — both would hide the mistake behind a
        plausible-looking empty or enormous result.
        """
        if isinstance(raw, bool):
            raise ValueError(_BAD_CURSOR.format(value=raw))
        if isinstance(raw, int):
            return cls.seed(clamp_to_now(raw, now_ms))

        text = str(raw).strip()
        if not text:
            raise ValueError(_BAD_CURSOR.format(value=raw))

        if text.startswith(f"{CURSOR_VERSION}|"):
            parts = text.split("|")
            if len(parts) != 3:
                raise ValueError(_BAD_CURSOR.format(value=raw))
            try:
                created, changed = int(parts[1]), int(parts[2])
            except ValueError:
                raise ValueError(_BAD_CURSOR.format(value=raw)) from None
            return cls(
                created_ms=clamp_to_now(max(0, created), now_ms),
                changed_ms=clamp_to_now(max(0, changed), now_ms),
            )

        # A token that looks versioned but isn't ours: fail rather than seed from it.
        if re.match(r"^v\d+\|", text):
            raise ValueError(_BAD_CURSOR.format(value=raw))

        if text.isdigit():
            value = int(text)
            if value < _EPOCH_MS_FLOOR:
                value *= 1000
            return cls.seed(clamp_to_now(value, now_ms))

        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            raise ValueError(_BAD_CURSOR.format(value=raw)) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return cls.seed(clamp_to_now(int(parsed.timestamp() * 1000), now_ms))


_BAD_CURSOR: Final = (
    "Could not read {value!r} as a cursor. Pass the `cursor` value from the previous "
    "response verbatim — do not construct or edit it. To start from a specific point "
    "instead, pass an ISO-8601 timestamp or epoch milliseconds."
)


def merge_monotonic(previous: Cursor, candidate: Cursor) -> tuple[Cursor, list[str]]:
    """Field-wise guarantee that neither watermark ever moves backwards.

    A decreasing watermark means the next poll re-reads a window it already returned,
    which for a truncated page loops forever. Any attempted decrease is reverted and
    reported rather than silently accepted.
    """
    notes: list[str] = []
    created, changed = candidate.created_ms, candidate.changed_ms
    if created < previous.created_ms:
        notes.append(
            "created watermark tried to move backwards; held at previous value"
        )
        created = previous.created_ms
    if changed < previous.changed_ms:
        notes.append(
            "changed watermark tried to move backwards; held at previous value"
        )
        changed = previous.changed_ms
    return Cursor(created_ms=created, changed_ms=changed), notes


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


def split_overfetch(
    raw: Sequence[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Trim an over-fetched page down to ``limit`` and report whether more remain.

    Callers request ``limit + 1``, so ``len(raw) > limit`` is the true signal. We test
    ``>=`` deliberately: if the server ever caps a page at ``limit`` and ignores the
    overfetch, ``> limit`` could never fire and we would advance past rows we never
    saw. The cost of ``>=`` is one extra empty poll when the row count lands exactly
    on ``limit``.
    """
    return list(raw[:limit]), len(raw) >= limit


def order_by_created(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int], bool]:
    """Stable-sort rows ascending by ``dateCreated``.

    Returns ``(rows, their ms, server_order_was_already_ascending)``. Rows with a
    missing or unusable ``dateCreated`` get ``0`` and sort to the front, so they are
    always emitted but never become the frontier.

    Sorting here is what makes the frontier trim sound: ``floor()`` is monotone over a
    true-nanosecond ascending order, so rows sharing a floor-millisecond are provably
    contiguous.
    """
    stamped = [(message_ms(row) or 0, index, row) for index, row in enumerate(rows)]
    ascending = all(a[0] <= b[0] for a, b in zip(stamped, stamped[1:]))
    stamped.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in stamped], [item[0] for item in stamped], ascending


@dataclass(frozen=True, slots=True)
class CreatedAdvance:
    """Outcome of advancing the ``message.date`` watermark over one page."""

    rows: list[dict[str, Any]]
    next_ms: int
    truncated: bool
    stalled_ms: int | None = None
    notes: list[str] = field(default_factory=list)


def advance_created(
    rows: Sequence[dict[str, Any]],
    *,
    prev_ms: int,
    limit: int,
    now_ms: int,
) -> CreatedAdvance:
    """Advance the created watermark over one page without ever skipping a row.

    ``rows`` is the raw response, which may hold ``limit + 1`` entries.

    The millisecond boundary is the whole problem. :func:`exclusive_apple_ns` excludes
    an entire millisecond under strict ``>``, which is correct *only* when we hold
    every row in that millisecond:

    * **Not truncated** — we have them all, so the max seen is safe.
    * **Truncated, spanning several milliseconds** — the cut may have landed
      mid-millisecond, so drop the trailing rows sharing the frontier millisecond.
      Every remaining row's millisecond is then provably complete.
    * **Truncated, everything usable inside one millisecond** — do NOT exclude it, or
      we drop everything past the cut permanently. Step to ``frontier - 1`` so the
      millisecond is re-fetched intact: bounded duplicates, never a skip.
    """
    notes: list[str] = []
    kept, truncated = split_overfetch(rows, limit)
    ordered, stamps, ascending = order_by_created(kept)

    if truncated and not ascending:
        # The cut happened at an end we did not expect, so the frontier trim below
        # would be reasoning about the wrong rows. Refuse to advance and say so.
        notes.append(
            "server returned this page out of ascending order; watermark held. "
            "Results are complete but the cursor did not advance."
        )
        return CreatedAdvance(ordered, prev_ms, True, None, notes)

    candidates = [ms for ms in stamps if ms > 0 and not is_future(ms, now_ms)]
    if not ordered or not candidates:
        return CreatedAdvance(ordered, prev_ms, truncated, None, notes)

    if not truncated:
        next_ms = max(prev_ms, clamp_to_now(candidates[-1], now_ms))
        return CreatedAdvance(ordered, next_ms, False, None, notes)

    frontier = candidates[-1]
    keep = next(index for index, ms in enumerate(stamps) if ms == frontier)
    before_frontier = [
        ms for ms in stamps[:keep] if ms > 0 and not is_future(ms, now_ms)
    ]

    if not before_frontier:
        # Everything usable sits in the frontier millisecond. Excluding it would drop
        # every row past the cut forever, so re-fetch the millisecond intact instead.
        return CreatedAdvance(
            ordered, max(prev_ms, frontier - 1), True, frontier, notes
        )

    next_ms = max(prev_ms, clamp_to_now(before_frontier[-1], now_ms))
    return CreatedAdvance(ordered[:keep], next_ms, True, None, notes)


def advance_changed(
    rows: Sequence[dict[str, Any]],
    *,
    prev_ms: int,
    truncated: bool,
    now_ms: int,
) -> int:
    """New ``date_edited`` / ``date_retracted`` watermark for one page.

    Returns ``prev_ms`` unchanged when the page was truncated. This axis is matched on
    the change columns but *sorted* by ``message.date`` — the only sortable column —
    so ``max(change_time)`` over an arbitrary truncated subset is not a frontier and
    advancing to it would lose edits. Holding costs duplicates; advancing loses data.

    Future-dated values are excluded from candidacy before the max, not clamped into
    it, so a single corrupt timestamp cannot drag the watermark forward.
    """
    if truncated:
        return prev_ms
    best = prev_ms
    for row in rows:
        for ms in change_times(row):
            if is_future(ms, now_ms):
                continue
            best = max(best, clamp_to_now(ms, now_ms))
    return best


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

_PART_PREFIX: Final = re.compile(r"^p:\d+/")

_REACTION_NAMES: Final = ("love", "like", "dislike", "laugh", "emphasize", "question")

#: What to call an ``associatedMessageType`` we do not recognise. Anything outside
#: 2000-2005 / 3000-3005 falls through to ``dbValue.toString()`` on the server, which
#: is why raw values like '2006', '3', and 'sticker' show up. Render those neutrally
#: rather than inventing a tapback name.
UNKNOWN_REACTION: Final = "responded to"


def reaction_target_guid(associated_message_guid: str | None) -> str:
    """Strip the part prefix from a reaction's target GUID.

    The prefix is not always ``p:0/``: across ~31,000 reaction rows it was ``p:0/``
    only 92.94% of the time, with ``p:1/`` through ``p:15/`` and no prefix at all
    making up the rest. Stripping just ``p:0/`` fails on 7% of reactions.
    """
    return _PART_PREFIX.sub("", associated_message_guid or "")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def reaction_label(associated_message_type: Any) -> tuple[str, bool]:
    """Map an ``associatedMessageType`` to ``(label, is_removal)``.

    Both wire forms are handled, because which one arrives depends on the server:

    * **Already-resolved names.** BlueBubbles 1.9.9 applies its own mapping before
      serializing, so the field arrives as ``'love'``, ``'-like'``, ``'emphasize'``…
      (verified live).
    * **Numeric codes.** 2000-2005 are love/like/dislike/laugh/emphasize/question and
      3000-3005 are the same with a leading ``-`` meaning the reaction was removed.

    Everything else falls through to ``dbValue.toString()`` on the server, which is
    why raw values like ``'2006'``, ``'3'`` and ``'sticker'`` really do appear. Those
    render neutrally — never invent a tapback name for a code you do not recognise.
    The unrecognised value is still reported verbatim as ``type_code``.
    """
    code = _as_int(associated_message_type)
    if code is not None:
        if 2000 <= code <= 2005:
            return _REACTION_NAMES[code - 2000], False
        if 3000 <= code <= 3005:
            return f"-{_REACTION_NAMES[code - 3000]}", True
        return UNKNOWN_REACTION, False

    if isinstance(associated_message_type, str):
        name = associated_message_type.strip()
        removed = name.startswith("-")
        if name.lstrip("-") in _REACTION_NAMES:
            return name, removed

    return UNKNOWN_REACTION, False


def summarize_reactions(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe the reaction rows in a page. Pure — no extra requests."""
    summary: list[dict[str, Any]] = []
    for row in rows:
        target = row.get("associatedMessageGuid")
        if not target:
            continue
        label, removed = reaction_label(row.get("associatedMessageType"))
        summary.append(
            {
                "guid": row.get("guid"),
                "target_guid": reaction_target_guid(target),
                "type": label,
                "type_code": row.get("associatedMessageType"),
                "removed": removed,
                "is_from_me": bool(row.get("isFromMe")),
                "date_ms": message_ms(row),
            }
        )
    return summary
