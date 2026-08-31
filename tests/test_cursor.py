"""Unit tests for the pure cursor arithmetic.

Every test here calls the shipped functions rather than re-deriving their logic — a
sibling implementation shipped with 16 of 27 deliberate mutations surviving precisely
because its tests recomputed the answer inline. The mutation list in the PR description
is the check on that.
"""

from __future__ import annotations

import pytest

from bb_mcp.cursor import (
    APPLE_EPOCH_MS,
    FUTURE_SKEW_MS,
    MAX_PAGE,
    NS_PER_MS,
    UNKNOWN_REACTION,
    Cursor,
    advance_changed,
    advance_created,
    change_times,
    changed_bind_ns,
    clamp_to_now,
    coerce_ms,
    exclusive_apple_ns,
    from_apple_ns,
    is_future,
    merge_monotonic,
    message_ms,
    order_by_created,
    reaction_label,
    reaction_target_guid,
    split_overfetch,
    summarize_reactions,
    to_apple_ns,
)

NOW = 1_800_000_000_000  # a fixed "now" in Unix ms; nothing here reads the clock


def msg(guid: str, created: object, **extra: object) -> dict[str, object]:
    return {"guid": guid, "dateCreated": created, **extra}


# ===========================================================================
# Apple epoch
# ===========================================================================


class TestAppleEpoch:
    def test_epoch_start_is_zero(self) -> None:
        assert to_apple_ns(APPLE_EPOCH_MS) == 0

    def test_round_trip_through_ns(self) -> None:
        assert from_apple_ns(to_apple_ns(NOW)) == NOW

    def test_exclusive_covers_the_whole_millisecond(self) -> None:
        # The last nanosecond of the millisecond, so strict `>` excludes all of it.
        assert exclusive_apple_ns(NOW) == to_apple_ns(NOW) + NS_PER_MS - 1

    def test_exclusive_is_below_the_next_millisecond(self) -> None:
        assert exclusive_apple_ns(NOW) < to_apple_ns(NOW + 1)


class TestChangedBind:
    def test_floors_at_zero_for_pre_2001_watermark(self) -> None:
        # Never-edited rows store 0. A negative bind makes `0 > bind` true and the
        # changed query returns the entire message table (verified live).
        assert exclusive_apple_ns(0) < 0
        assert changed_bind_ns(0) == 0

    def test_normal_watermark_passes_through(self) -> None:
        assert changed_bind_ns(NOW) == exclusive_apple_ns(NOW)


# ===========================================================================
# Defensive coercion
# ===========================================================================


class TestCoerceMs:
    @pytest.mark.parametrize("value", [123, 123.9, "123", " 123 ", "123.0"])
    def test_numeric_forms_coerce(self, value: object) -> None:
        assert coerce_ms(value) == 123

    @pytest.mark.parametrize(
        "value",
        [
            None,
            0,
            "0",
            "",
            "   ",
            "abc",
            True,
            False,
            [],
            {},
            float("nan"),
            float("inf"),
        ],
    )
    def test_unusable_forms_return_none(self, value: object) -> None:
        assert coerce_ms(value) is None

    def test_string_timestamp_does_not_raise_in_max(self) -> None:
        # The original crash: a str where an int was assumed blew up inside max().
        rows = [msg("a", "1700000000000"), msg("b", 1700000000001)]
        assert (
            advance_created(rows, prev_ms=0, limit=5, now_ms=NOW).next_ms
            == 1700000000001
        )

    def test_message_ms_reads_date_created(self) -> None:
        assert message_ms(msg("a", 7)) == 7

    def test_change_times_collects_both_columns(self) -> None:
        row = {"dateEdited": 5, "dateRetracted": 9}
        assert sorted(change_times(row)) == [5, 9]

    def test_change_times_skips_zero_sentinels(self) -> None:
        assert change_times({"dateEdited": 0, "dateRetracted": None}) == []


class TestIsFuture:
    def test_within_skew_allowance_is_not_future(self) -> None:
        # A server clock that leads ours must not make every real message "future",
        # which would leave no watermark candidate and stall the axis forever.
        assert not is_future(NOW + FUTURE_SKEW_MS - 1, NOW)

    def test_beyond_skew_allowance_is_future(self) -> None:
        assert is_future(NOW + FUTURE_SKEW_MS + 1, NOW)

    def test_clamp_to_now_caps(self) -> None:
        assert clamp_to_now(NOW + 1000, NOW) == NOW
        assert clamp_to_now(NOW - 1000, NOW) == NOW - 1000


# ===========================================================================
# Cursor encoding
# ===========================================================================


class TestCursorRoundTrip:
    def test_encode_parse_identity(self) -> None:
        original = Cursor(created_ms=NOW - 5, changed_ms=NOW - 9)
        assert Cursor.parse(original.encode(), now_ms=NOW) == original

    def test_encoded_form_survives_two_round_trips(self) -> None:
        token = Cursor(created_ms=NOW - 5, changed_ms=NOW - 9).encode()
        assert (
            Cursor.parse(Cursor.parse(token, now_ms=NOW).encode(), now_ms=NOW).encode()
            == token
        )

    def test_axes_stay_independent_through_encoding(self) -> None:
        parsed = Cursor.parse(
            Cursor(created_ms=111, changed_ms=222).encode(), now_ms=NOW
        )
        assert (parsed.created_ms, parsed.changed_ms) == (111, 222)

    def test_seed_sets_both_axes(self) -> None:
        assert Cursor.seed(500) == Cursor(created_ms=500, changed_ms=500)

    def test_epoch_ms_string_seeds_both(self) -> None:
        assert Cursor.parse(str(NOW - 1000), now_ms=NOW) == Cursor.seed(NOW - 1000)

    def test_epoch_seconds_are_promoted_to_ms(self) -> None:
        assert Cursor.parse("1700000000", now_ms=NOW) == Cursor.seed(1_700_000_000_000)

    def test_iso8601_seeds_both(self) -> None:
        parsed = Cursor.parse("2024-01-01T00:00:00+00:00", now_ms=NOW)
        assert parsed == Cursor.seed(1_704_067_200_000)

    def test_iso8601_z_suffix_accepted(self) -> None:
        assert Cursor.parse("2024-01-01T00:00:00Z", now_ms=NOW) == Cursor.seed(
            1_704_067_200_000
        )

    def test_future_seed_clamped_to_now(self) -> None:
        assert Cursor.parse(NOW + 10_000_000, now_ms=NOW) == Cursor.seed(NOW)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "not-a-cursor",
            "v1|only-two",
            "v1|a|b",
            "v9|1|2",
            "v1|1|2|3",
            "v2|1|2",
            "v2|1|2|3|4",
            # An empty scope normalises to None, which is what a GLOBAL poll expects —
            # so without an explicit refusal this seeds a global walk from a token we
            # never minted.
            "v2|1|2|",
            "v2|1|2|   ",
        ],
    )
    def test_malformed_raises_rather_than_seeding(self, bad: str) -> None:
        # Never fall back to "now" or "the beginning of time": either would hide the
        # mistake behind a plausible-looking empty or enormous result.
        with pytest.raises(ValueError, match="verbatim"):
            Cursor.parse(bad, now_ms=NOW)


CHAT = "iMessage;-;+15551234567"
OTHER_CHAT = "iMessage;-;+15559998888"


class TestCursorScope:
    """Scope lives in the token, never on the dataclass.

    Putting it on :class:`Cursor` would have `merge_monotonic` drop it field-wise and
    would make `next_cursor != cursor` permanently true, destroying the stall signal.
    """

    def test_unscoped_encode_is_unchanged_v1(self) -> None:
        assert Cursor(created_ms=111, changed_ms=222).encode() == "v1|111|222"

    def test_scoped_encode_is_v2_with_the_normalized_chat(self) -> None:
        token = Cursor(created_ms=111, changed_ms=222).encode(CHAT)
        assert token == "v2|111|222|any;-;+15551234567"

    def test_scoped_round_trip(self) -> None:
        original = Cursor(created_ms=NOW - 5, changed_ms=NOW - 9)
        parsed = Cursor.parse(original.encode(CHAT), now_ms=NOW, expect_scope=CHAT)
        assert parsed == original

    def test_scoped_encode_clamps_and_floors_like_the_global_path(self) -> None:
        parsed = Cursor.parse(
            f"v2|{NOW + 10_000}|-5|{CHAT}", now_ms=NOW, expect_scope=CHAT
        )
        assert (parsed.created_ms, parsed.changed_ms) == (NOW, 0)

    def test_service_prefix_does_not_change_the_scope(self) -> None:
        # Polling as `iMessage;-;X` and then `any;-;X` is the same query; a spurious
        # mismatch there would break an agent that did nothing wrong.
        assert Cursor(1, 2).encode(CHAT) == Cursor(1, 2).encode("any;-;+15551234567")
        Cursor.parse(
            Cursor(1, 2).encode(CHAT), now_ms=NOW, expect_scope="any;-;+15551234567"
        )

    def test_scoped_cursor_in_a_global_poll_raises(self) -> None:
        with pytest.raises(ValueError, match="one cursor per scope"):
            Cursor.parse(Cursor(1, 2).encode(CHAT), now_ms=NOW)

    def test_global_cursor_in_a_scoped_poll_raises(self) -> None:
        # Safe in isolation, but `advance_created` would move the global watermark to
        # the last row in this chat and strand every other chat's messages behind it.
        with pytest.raises(ValueError, match="one cursor per scope"):
            Cursor.parse(Cursor(1, 2).encode(), now_ms=NOW, expect_scope=CHAT)

    def test_another_chats_cursor_raises(self) -> None:
        with pytest.raises(ValueError, match="one cursor per scope"):
            Cursor.parse(Cursor(1, 2).encode(OTHER_CHAT), now_ms=NOW, expect_scope=CHAT)

    def test_mismatch_does_not_tell_the_caller_to_echo_it_verbatim(self) -> None:
        # `_BAD_CURSOR`'s advice is wrong here: the agent did echo it verbatim.
        with pytest.raises(ValueError) as excinfo:
            Cursor.parse(Cursor(1, 2).encode(CHAT), now_ms=NOW)
        assert "verbatim" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "seed", [NOW - 60_000, "1700000000", str(NOW - 1000), "2024-01-01T00:00:00Z"]
    )
    def test_seeds_are_scope_neutral(self, seed: str | int) -> None:
        # Only encoded tokens carry scope. If a seed had to match, every FIRST scoped
        # poll would fail.
        assert Cursor.parse(seed, now_ms=NOW, expect_scope=CHAT) == Cursor.parse(
            seed, now_ms=NOW
        )


class TestMergeMonotonic:
    def test_backwards_created_is_held_and_reported(self) -> None:
        merged, notes = merge_monotonic(Cursor(100, 100), Cursor(50, 100))
        assert merged.created_ms == 100
        assert any("backwards" in n for n in notes)

    def test_backwards_changed_is_held_and_reported(self) -> None:
        merged, notes = merge_monotonic(Cursor(100, 100), Cursor(100, 50))
        assert merged.changed_ms == 100
        assert any("backwards" in n for n in notes)

    def test_forward_movement_is_untouched(self) -> None:
        merged, notes = merge_monotonic(Cursor(100, 100), Cursor(200, 300))
        assert (merged.created_ms, merged.changed_ms, notes) == (200, 300, [])


# ===========================================================================
# Paging
# ===========================================================================


class TestSplitOverfetch:
    def test_overfetched_page_is_truncated(self) -> None:
        rows, truncated = split_overfetch([msg(str(i), i) for i in range(6)], 5)
        assert len(rows) == 5 and truncated

    def test_exactly_limit_counts_as_truncated(self) -> None:
        # Belt-and-braces: if the server ever caps the page at `limit` and ignores our
        # +1 overfetch, a strict `>` could never fire and we would advance past rows
        # we never saw. One extra empty poll is the cost.
        rows, truncated = split_overfetch([msg(str(i), i) for i in range(5)], 5)
        assert len(rows) == 5 and truncated

    def test_short_page_is_not_truncated(self) -> None:
        rows, truncated = split_overfetch([msg("a", 1)], 5)
        assert len(rows) == 1 and not truncated


class TestOrderByCreated:
    def test_sorts_ascending_and_reports_server_order(self) -> None:
        rows, stamps, ascending = order_by_created([msg("b", 2), msg("a", 1)])
        assert [r["guid"] for r in rows] == ["a", "b"]
        assert stamps == [1, 2]
        assert not ascending

    def test_detects_already_ascending(self) -> None:
        assert order_by_created([msg("a", 1), msg("b", 2)])[2] is True

    def test_equal_stamps_keep_server_order(self) -> None:
        rows, _, _ = order_by_created([msg("a", 5), msg("b", 5), msg("c", 5)])
        assert [r["guid"] for r in rows] == ["a", "b", "c"]

    def test_unusable_date_sorts_to_front(self) -> None:
        rows, stamps, _ = order_by_created([msg("good", 9), msg("junk", "abc")])
        assert [r["guid"] for r in rows] == ["junk", "good"]
        assert stamps == [0, 9]


# ===========================================================================
# The created watermark
# ===========================================================================


class TestAdvanceCreated:
    def test_untruncated_page_takes_the_max(self) -> None:
        result = advance_created(
            [msg("a", 100), msg("b", 200)], prev_ms=0, limit=5, now_ms=NOW
        )
        assert result.next_ms == 200
        assert not result.truncated
        assert len(result.rows) == 2

    def test_empty_page_holds_the_watermark(self) -> None:
        result = advance_created([], prev_ms=42, limit=5, now_ms=NOW)
        assert result.next_ms == 42 and result.rows == []

    def test_truncation_between_rows_sharing_a_millisecond_skips_nothing(self) -> None:
        # The cut lands inside millisecond 200, which holds b and c. Excluding 200
        # would lose c forever, so the whole 200-block is dropped and re-fetched.
        rows = [msg("a", 100), msg("b", 200), msg("c", 200)]
        result = advance_created(rows, prev_ms=0, limit=3, now_ms=NOW)
        assert result.truncated
        assert [r["guid"] for r in result.rows] == ["a"]
        assert result.next_ms == 100
        assert exclusive_apple_ns(result.next_ms) < to_apple_ns(200)

    def test_frontier_block_longer_than_one_row_is_dropped_whole(self) -> None:
        rows = [msg("a", 100), msg("b", 200), msg("c", 200), msg("d", 200)]
        result = advance_created(rows, prev_ms=0, limit=4, now_ms=NOW)
        assert [r["guid"] for r in result.rows] == ["a"]
        assert result.next_ms == 100

    def test_whole_batch_inside_one_millisecond_steps_back(self) -> None:
        # Excluding the millisecond here would permanently drop everything past the
        # cut, so the cursor steps to frontier-1 and the millisecond is re-fetched.
        rows = [msg("a", 500), msg("b", 500), msg("c", 500)]
        result = advance_created(rows, prev_ms=0, limit=3, now_ms=NOW)
        assert result.next_ms == 499
        assert result.stalled_ms == 500
        assert len(result.rows) == 3, "the batch must not be emptied"

    def test_stepped_back_cursor_refetches_that_millisecond(self) -> None:
        result = advance_created(
            [msg("a", 500), msg("b", 500)], prev_ms=0, limit=2, now_ms=NOW
        )
        assert exclusive_apple_ns(result.next_ms) < to_apple_ns(500)

    def test_never_moves_backwards(self) -> None:
        result = advance_created([msg("a", 10)], prev_ms=999, limit=5, now_ms=NOW)
        assert result.next_ms == 999

    def test_future_row_is_returned_but_never_becomes_the_watermark(self) -> None:
        far = NOW + 50 * 365 * 24 * 3600 * 1000
        result = advance_created(
            [msg("real", NOW - 1000), msg("corrupt", far)],
            prev_ms=0,
            limit=5,
            now_ms=NOW,
        )
        assert result.next_ms == NOW - 1000
        assert {r["guid"] for r in result.rows} == {"real", "corrupt"}

    def test_row_inside_the_skew_window_is_clamped_to_now(self) -> None:
        result = advance_created(
            [msg("a", NOW + FUTURE_SKEW_MS - 1)], prev_ms=0, limit=5, now_ms=NOW
        )
        assert result.next_ms == NOW

    def test_only_future_rows_holds_the_watermark(self) -> None:
        far = NOW + 10_000_000
        result = advance_created([msg("corrupt", far)], prev_ms=7, limit=5, now_ms=NOW)
        assert result.next_ms == 7

    def test_unusable_dates_are_returned_but_not_watermark_candidates(self) -> None:
        result = advance_created(
            [msg("junk", None), msg("good", 300)], prev_ms=0, limit=5, now_ms=NOW
        )
        assert result.next_ms == 300
        assert len(result.rows) == 2

    def test_descending_page_that_is_truncated_refuses_to_advance(self) -> None:
        rows = [msg("c", 300), msg("b", 200), msg("a", 100)]
        result = advance_created(rows, prev_ms=50, limit=3, now_ms=NOW)
        assert result.next_ms == 50
        assert any("ascending" in n for n in result.notes)

    def test_descending_page_that_is_complete_still_advances(self) -> None:
        rows = [msg("b", 200), msg("a", 100)]
        result = advance_created(rows, prev_ms=0, limit=5, now_ms=NOW)
        assert result.next_ms == 200


# ===========================================================================
# The changed watermark
# ===========================================================================


class TestAdvanceChanged:
    def test_untruncated_takes_the_max_change_time(self) -> None:
        rows = [{"dateEdited": 100}, {"dateRetracted": 300}, {"dateEdited": 200}]
        assert advance_changed(rows, prev_ms=0, truncated=False, now_ms=NOW) == 300

    def test_retraction_alone_advances_the_watermark(self) -> None:
        rows = [{"dateRetracted": 400}]
        assert advance_changed(rows, prev_ms=0, truncated=False, now_ms=NOW) == 400

    def test_truncated_page_holds_the_watermark(self) -> None:
        # This axis is matched on the change columns but sorted by message.date, so
        # max(change_time) over a truncated subset is not a frontier. Holding costs
        # duplicates; advancing loses edits.
        rows = [{"dateEdited": 100}, {"dateEdited": 300}]
        assert advance_changed(rows, prev_ms=50, truncated=True, now_ms=NOW) == 50

    def test_future_edit_is_ignored_not_adopted(self) -> None:
        rows = [{"dateEdited": NOW - 10}, {"dateEdited": NOW + 10_000_000_000}]
        assert advance_changed(rows, prev_ms=0, truncated=False, now_ms=NOW) == NOW - 10

    def test_edit_inside_the_skew_window_is_clamped_to_now(self) -> None:
        # is_future() tolerates a server clock up to FUTURE_SKEW_MS ahead, so such a
        # value is a legitimate watermark candidate — but adopting it verbatim parks
        # the cursor in the future and skips everything written in that gap.
        rows = [{"dateEdited": NOW + FUTURE_SKEW_MS - 1}]
        assert advance_changed(rows, prev_ms=0, truncated=False, now_ms=NOW) == NOW

    def test_all_future_edits_hold_the_watermark(self) -> None:
        rows = [{"dateEdited": NOW + 10_000_000_000}]
        assert advance_changed(rows, prev_ms=5, truncated=False, now_ms=NOW) == 5

    def test_never_moves_backwards(self) -> None:
        assert (
            advance_changed(
                [{"dateEdited": 10}], prev_ms=999, truncated=False, now_ms=NOW
            )
            == 999
        )

    def test_empty_page_holds(self) -> None:
        assert advance_changed([], prev_ms=77, truncated=False, now_ms=NOW) == 77


# ===========================================================================
# Reactions
# ===========================================================================


class TestReactionTargets:
    @pytest.mark.parametrize("prefix", ["p:0/", "p:1/", "p:7/", "p:15/", ""])
    def test_every_part_prefix_is_stripped(self, prefix: str) -> None:
        assert reaction_target_guid(f"{prefix}ABC-123") == "ABC-123"

    def test_prefix_is_only_stripped_at_the_start(self) -> None:
        assert reaction_target_guid("ABC/p:0/DEF") == "ABC/p:0/DEF"

    def test_none_is_tolerated(self) -> None:
        assert reaction_target_guid(None) == ""


class TestReactionLabels:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (2000, "love"),
            (2001, "like"),
            (2002, "dislike"),
            (2003, "laugh"),
            (2004, "emphasize"),
            (2005, "question"),
        ],
    )
    def test_numeric_reaction_codes(self, code: int, expected: str) -> None:
        assert reaction_label(code) == (expected, False)

    @pytest.mark.parametrize("code", [3000, 3001, 3002, 3003, 3004, 3005])
    def test_numeric_removal_codes(self, code: int) -> None:
        label, removed = reaction_label(code)
        assert removed and label.startswith("-")

    def test_resolved_name_from_the_wire(self) -> None:
        # BlueBubbles 1.9.9 applies its own mapping before serializing (verified live).
        assert reaction_label("love") == ("love", False)

    def test_resolved_removal_name_from_the_wire(self) -> None:
        assert reaction_label("-like") == ("-like", True)

    @pytest.mark.parametrize("code", ["2006", 2006, "3", "sticker", None, "", 3006])
    def test_unrecognised_codes_render_neutrally(self, code: object) -> None:
        # Never invent a tapback name for a code the server did not map.
        assert reaction_label(code) == (UNKNOWN_REACTION, False)


class TestSummarizeReactions:
    def test_ignores_ordinary_messages(self) -> None:
        assert summarize_reactions([msg("a", 1)]) == []

    def test_describes_a_reaction_row(self) -> None:
        rows = [
            msg(
                "r1",
                123,
                associatedMessageGuid="p:3/TARGET",
                associatedMessageType="love",
                isFromMe=True,
            )
        ]
        (summary,) = summarize_reactions(rows)
        assert summary["target_guid"] == "TARGET"
        assert summary["type"] == "love"
        assert summary["removed"] is False
        assert summary["is_from_me"] is True
        assert summary["date_ms"] == 123

    def test_reports_the_raw_code_even_when_unrecognised(self) -> None:
        rows = [msg("r1", 1, associatedMessageGuid="T", associatedMessageType="2006")]
        (summary,) = summarize_reactions(rows)
        assert summary["type"] == UNKNOWN_REACTION
        assert summary["type_code"] == "2006"


class TestPageBounds:
    def test_max_page_leaves_room_for_the_overfetch(self) -> None:
        # We always ask for limit+1, and the server 400s anything above 1000.
        assert MAX_PAGE + 1 <= 1000
