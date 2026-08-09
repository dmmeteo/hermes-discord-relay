"""Thread-ownership engine.

Covers the pure decision engine and its persistent store.  The engine is
deliberately free of any ``discord`` import so these tests exercise the real
logic (no mocks of the thing under test).

The plugin is loaded the same way ``PluginManager._load_directory_module``
loads it at runtime — see ``conftest.py``.
"""

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest
from conftest import load_plugin_module

ownership = load_plugin_module("ownership")


OwnershipConfig = ownership.OwnershipConfig
MessageFacts = ownership.MessageFacts
ThreadOwnershipStore = ownership.ThreadOwnershipStore

BOT_A = "1000"
BOT_B = "2000"
BOT_C = "3000"
HUMAN = "9001"
HUMAN_2 = "9002"
THREAD = "77"


def _cfg(**over):
    base = dict(enabled=True)
    base.update(over)
    return OwnershipConfig(**base)


def _facts(
    *,
    self_id=BOT_A,
    author_id=HUMAN,
    author_is_bot=False,
    mentioned_bot_ids=(),
    message_id="101",
    content="",
    thread_id=THREAD,
    has_unresolved_mentions=False,
):
    return MessageFacts(
        thread_id=thread_id,
        message_id=message_id,
        author_id=author_id,
        author_is_bot=author_is_bot,
        self_id=self_id,
        mentioned_bot_ids=tuple(mentioned_bot_ids),
        content=content,
        has_unresolved_mentions=has_unresolved_mentions,
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return ThreadOwnershipStore()


# ── Slice 1: claim + unmentioned follow-up ──────────────────────────────────


class TestClaim:
    def test_human_mentioning_one_bot_claims_the_thread(self, store):
        d = store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        assert d.owner_id == BOT_A
        assert d.verdict == "claim"
        assert store.owner_of(THREAD) == BOT_A

    def test_claiming_bot_defers_to_base_admission(self, store):
        d = store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        # None == "no ownership opinion, let the built-in gate decide".
        assert d.admit_self is None

    def test_unmentioned_bot_is_vetoed_by_the_claim(self, store):
        d = store.apply(
            _facts(self_id=BOT_B, mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0
        )
        assert d.owner_id == BOT_A
        assert d.admit_self is False

    def test_unmentioned_human_follow_up_reaches_only_the_owner(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)

        owner = store.apply(
            _facts(self_id=BOT_A, message_id="102", content="and then?"),
            _cfg(),
            now=1.0,
        )
        other = store.apply(
            _facts(self_id=BOT_B, message_id="102", content="and then?"),
            _cfg(),
            now=1.0,
        )
        assert owner.admit_self is None      # owner: base rules apply → responds
        assert other.admit_self is False     # non-owner: silenced
        assert store.owner_of(THREAD) == BOT_A

    def test_no_owner_yet_defers_for_everyone(self, store):
        d = store.apply(_facts(content="hello thread"), _cfg(), now=0.0)
        assert d.owner_id is None
        assert d.admit_self is None


# ── Slice 2: human-driven transfer ──────────────────────────────────────────


class TestHumanTransfer:
    def test_mentioning_another_bot_transfers_ownership(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        d = store.apply(
            _facts(self_id=BOT_C, message_id="102", mentioned_bot_ids=[BOT_C]),
            _cfg(),
            now=1.0,
        )
        assert d.verdict == "transfer"
        assert d.owner_id == BOT_C
        assert store.owner_of(THREAD) == BOT_C

    def test_old_owner_stays_silent_on_the_transfer_message(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        old = store.apply(
            _facts(self_id=BOT_A, message_id="102", mentioned_bot_ids=[BOT_C]),
            _cfg(),
            now=1.0,
        )
        assert old.admit_self is False

    def test_old_owner_stays_silent_on_later_follow_ups(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        store.apply(
            _facts(self_id=BOT_A, message_id="102", mentioned_bot_ids=[BOT_C]),
            _cfg(),
            now=1.0,
        )
        later = store.apply(
            _facts(self_id=BOT_A, message_id="103", content="still there?"),
            _cfg(),
            now=2.0,
        )
        assert later.admit_self is False

    def test_re_mentioning_the_current_owner_is_a_no_op(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        d = store.apply(
            _facts(message_id="102", mentioned_bot_ids=[BOT_A]), _cfg(), now=1.0
        )
        assert d.verdict == "reaffirm"
        assert d.owner_id == BOT_A

    def test_mentioning_a_human_does_not_transfer(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        d = store.apply(
            _facts(
                self_id=BOT_A,
                message_id="102",
                content="<@9002> thoughts?",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.verdict == "none"
        assert store.owner_of(THREAD) == BOT_A

    def test_mentioning_a_human_and_a_bot_transfers_to_the_bot(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        d = store.apply(
            _facts(
                message_id="102",
                mentioned_bot_ids=[BOT_C],
                content="<@9002> <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.owner_id == BOT_C


# ── Slice 3: multi-bot determinism, bots cannot steal ───────────────────────


class TestDeterminismAndTheft:
    def test_first_mentioned_bot_wins(self, store):
        d = store.apply(
            _facts(mentioned_bot_ids=[BOT_C, BOT_A, BOT_B]), _cfg(), now=0.0
        )
        assert d.owner_id == BOT_C

    def test_every_bot_computes_the_same_winner(self, tmp_path, monkeypatch):
        """Each profile keeps its own store; they converge without shared state."""
        winners = []
        for i, me in enumerate((BOT_A, BOT_B, BOT_C)):
            home = tmp_path / f"profile{i}"
            home.mkdir()
            monkeypatch.setenv("HERMES_HOME", str(home))
            s = ThreadOwnershipStore()
            d = s.apply(
                _facts(self_id=me, mentioned_bot_ids=[BOT_B, BOT_A]), _cfg(), now=0.0
            )
            winners.append(d.owner_id)
        assert winners == [BOT_B, BOT_B, BOT_B]

    def test_only_the_winner_is_not_vetoed(self, store):
        losing = store.apply(
            _facts(self_id=BOT_A, mentioned_bot_ids=[BOT_B, BOT_A]), _cfg(), now=0.0
        )
        assert losing.admit_self is False

    def test_bot_message_cannot_claim_an_unowned_thread(self, store):
        d = store.apply(
            _facts(
                self_id=BOT_B,
                author_id=BOT_B,
                author_is_bot=True,
                mentioned_bot_ids=[BOT_B],
            ),
            _cfg(),
            now=0.0,
        )
        assert d.owner_id is None
        assert store.owner_of(THREAD) is None

    def test_non_owner_bot_cannot_steal_from_the_owner(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        d = store.apply(
            _facts(
                self_id=BOT_A,
                author_id=BOT_B,
                author_is_bot=True,
                message_id="102",
                mentioned_bot_ids=[BOT_B],
                content="[[handoff]] <@2000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert store.owner_of(THREAD) == BOT_A
        assert d.verdict == "none"

    def test_non_owner_bot_cannot_hand_the_thread_to_a_third_bot(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        store.apply(
            _facts(
                author_id=BOT_B,
                author_is_bot=True,
                message_id="102",
                mentioned_bot_ids=[BOT_C],
                content="[[handoff]] <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert store.owner_of(THREAD) == BOT_A


# ── Slice 4: owner-initiated bot-to-bot handoff ─────────────────────────────


class TestBotHandoff:
    def _own(self, store, owner=BOT_A):
        store.apply(
            _facts(mentioned_bot_ids=[owner], message_id="100"), _cfg(), now=0.0
        )

    def test_owner_hands_off_with_marker(self, store):
        self._own(store)
        d = store.apply(
            _facts(
                self_id=BOT_C,
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="done here [[handoff]] <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.verdict == "handoff"
        assert d.owner_id == BOT_C
        # The receiving bot must be admitted even though allow_bots is "none".
        assert d.admit_self is True

    def test_handing_off_owner_goes_silent(self, store):
        self._own(store)
        d = store.apply(
            _facts(
                self_id=BOT_A,
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="[[handoff]] <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.owner_id == BOT_C
        assert d.admit_self is False

    def test_uninvolved_bot_is_not_admitted_by_a_handoff(self, store):
        self._own(store)
        d = store.apply(
            _facts(
                self_id=BOT_B,
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="[[handoff]] <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.admit_self is False

    def test_ordinary_owner_mention_without_marker_is_not_a_handoff(self, store):
        """The distinguishing signal: chatter that merely names another bot."""
        self._own(store)
        d = store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="you could ask <@3000> about that",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.verdict == "none"
        assert store.owner_of(THREAD) == BOT_A

    def test_mention_mode_accepts_a_bare_owner_mention(self, store):
        self._own(store)
        d = store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="over to <@3000>",
            ),
            _cfg(bot_handoff="mention"),
            now=1.0,
        )
        assert d.owner_id == BOT_C

    def test_off_mode_disables_bot_handoff_entirely(self, store):
        self._own(store)
        store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="[[handoff]] <@3000>",
            ),
            _cfg(bot_handoff="off"),
            now=1.0,
        )
        assert store.owner_of(THREAD) == BOT_A

    def test_self_handoff_is_ignored(self, store):
        self._own(store)
        d = store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_A],
                content="[[handoff]] <@1000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.verdict == "none"
        assert store.owner_of(THREAD) == BOT_A

    def test_ambiguous_multi_bot_handoff_is_refused(self, store):
        """Humans get first-mention-wins; autonomous handoff refuses instead."""
        self._own(store)
        d = store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_B, BOT_C],
                content="[[handoff]] <@2000> <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.verdict == "none"
        assert store.owner_of(THREAD) == BOT_A

    def test_marker_is_case_insensitive(self, store):
        self._own(store)
        d = store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="101",
                mentioned_bot_ids=[BOT_C],
                content="[[HANDOFF]] <@3000>",
            ),
            _cfg(),
            now=1.0,
        )
        assert d.owner_id == BOT_C


# ── Slice 4b: loop / abuse protection ───────────────────────────────────────


class TestHandoffGuards:
    def _handoff(self, store, frm, to, mid, now, cfg=None):
        return store.apply(
            _facts(
                self_id=to,
                author_id=frm,
                author_is_bot=True,
                message_id=mid,
                mentioned_bot_ids=[to],
                content=f"[[handoff]] <@{to}>",
            ),
            cfg or _cfg(),
            now=now,
        )

    def test_immediate_bounce_back_is_blocked(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), _cfg(), now=0.0)
        self._handoff(store, BOT_A, BOT_C, "101", 1.0)
        d = self._handoff(store, BOT_C, BOT_A, "102", 2.0)
        assert d.verdict == "none"
        assert d.reason == "bounce_blocked"
        assert store.owner_of(THREAD) == BOT_C

    def test_bounce_back_is_allowed_after_the_cooldown(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), _cfg(), now=0.0)
        self._handoff(store, BOT_A, BOT_C, "101", 1.0)
        d = self._handoff(store, BOT_C, BOT_A, "102", 1.0 + 31.0)
        assert d.owner_id == BOT_A

    def test_hop_budget_freezes_runaway_handoffs(self, store):
        cfg = _cfg(max_handoffs=3, handoff_cooldown_seconds=0.0)
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        chain = [BOT_C, BOT_A, BOT_C, BOT_A, BOT_C]
        owner = BOT_A
        results = []
        for i, nxt in enumerate(chain):
            results.append(self._handoff(store, owner, nxt, str(101 + i), i + 1.0, cfg))
            if results[-1].verdict == "handoff":
                owner = nxt
        assert [r.verdict for r in results[:3]] == ["handoff"] * 3
        assert results[3].verdict == "none"
        assert results[3].reason == "budget_exhausted"
        assert results[4].verdict == "none"

    def test_budget_recovers_after_the_window(self, store):
        cfg = _cfg(max_handoffs=2, handoff_cooldown_seconds=0.0, handoff_window_seconds=100.0)
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        self._handoff(store, BOT_A, BOT_C, "101", 1.0, cfg)
        self._handoff(store, BOT_C, BOT_A, "102", 2.0, cfg)
        assert self._handoff(store, BOT_A, BOT_C, "103", 3.0, cfg).verdict == "none"
        assert self._handoff(store, BOT_A, BOT_C, "104", 200.0, cfg).verdict == "handoff"

    def test_a_human_can_always_break_a_frozen_thread(self, store):
        """Budget exhaustion must never lock humans out of re-assigning."""
        cfg = _cfg(max_handoffs=1, handoff_cooldown_seconds=0.0)
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        self._handoff(store, BOT_A, BOT_C, "101", 1.0, cfg)
        assert self._handoff(store, BOT_C, BOT_A, "102", 2.0, cfg).verdict == "none"
        d = store.apply(
            _facts(message_id="103", mentioned_bot_ids=[BOT_B]), cfg, now=3.0
        )
        assert d.owner_id == BOT_B

    def test_human_transfer_resets_the_hop_budget(self, store):
        cfg = _cfg(max_handoffs=1, handoff_cooldown_seconds=0.0)
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        self._handoff(store, BOT_A, BOT_C, "101", 1.0, cfg)
        store.apply(_facts(message_id="102", mentioned_bot_ids=[BOT_A]), cfg, now=2.0)
        assert self._handoff(store, BOT_A, BOT_C, "103", 3.0, cfg).verdict == "handoff"


# ── Slice 5: persistence and idempotency ────────────────────────────────────


class TestPersistence:
    def test_ownership_survives_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        ThreadOwnershipStore().apply(_facts(mentioned_bot_ids=[BOT_C]), _cfg(), now=0.0)
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_C

    def test_state_file_lands_in_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        ThreadOwnershipStore().apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        path = tmp_path / "discord_thread_ownership.json"
        assert path.exists()
        assert json.loads(path.read_text())["threads"][THREAD]["owner_id"] == BOT_A

    def test_profiles_keep_independent_state(self, tmp_path, monkeypatch):
        one, two = tmp_path / "one", tmp_path / "two"
        one.mkdir()
        two.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(one))
        ThreadOwnershipStore().apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        monkeypatch.setenv("HERMES_HOME", str(two))
        assert ThreadOwnershipStore().owner_of(THREAD) is None

    def test_replaying_the_same_message_does_not_double_count(self, store):
        cfg = _cfg(max_handoffs=2, handoff_cooldown_seconds=0.0)
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        facts = _facts(
            author_id=BOT_A,
            author_is_bot=True,
            message_id="101",
            mentioned_bot_ids=[BOT_C],
            content="[[handoff]] <@3000>",
        )
        first = store.apply(facts, cfg, now=1.0)
        second = store.apply(facts, cfg, now=1.0)
        assert first.owner_id == second.owner_id == BOT_C
        assert second.verdict == "replay"
        # The replay consumed no hop, so the second of two budgeted handoffs
        # still fits.  (If it had double-counted, this would be refused.)
        assert store.apply(
            _facts(
                author_id=BOT_C,
                author_is_bot=True,
                message_id="102",
                mentioned_bot_ids=[BOT_B],
                content="[[handoff]] <@2000>",
            ),
            cfg,
            now=2.0,
        ).verdict == "handoff"

    def test_tracked_threads_are_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        s = ThreadOwnershipStore(max_threads=5)
        for i in range(20):
            s.apply(
                _facts(thread_id=str(i), message_id=str(100 + i), mentioned_bot_ids=[BOT_A]),
                _cfg(),
                now=float(i),
            )
        saved = json.loads((tmp_path / "discord_thread_ownership.json").read_text())
        assert len(saved["threads"]) == 5
        assert s.owner_of("19") == BOT_A
        assert s.owner_of("0") is None

    def test_corrupt_state_file_is_survivable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "discord_thread_ownership.json").write_text("{not json")
        s = ThreadOwnershipStore()
        assert s.owner_of(THREAD) is None
        assert s.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0).owner_id == BOT_A


# ── Slice 5c: replay / out-of-order redelivery must not roll ownership back ─


class TestReplayAndOutOfOrder:
    """Discord resurfaces messages two ways; both must be inert.

    Gateway RESUME redelivers an exact message, and missed-message backfill can
    surface an *older* message never seen before.  Either one re-applied would
    snap ownership back to a previous owner and resurrect the double-answer
    bug this feature exists to remove.
    """

    def _claim(self, store, bot, mid, now):
        return store.apply(
            _facts(mentioned_bot_ids=[bot], message_id=mid), _cfg(), now=now
        )

    def test_exact_redelivery_does_not_roll_ownership_back(self, store):
        self._claim(store, BOT_A, "1001", 0.0)
        self._claim(store, BOT_C, "1002", 1.0)
        replay = store.apply(
            _facts(mentioned_bot_ids=[BOT_A], message_id="1001"), _cfg(), now=2.0
        )
        assert replay.verdict == "replay"
        assert store.owner_of(THREAD) == BOT_C

    def test_an_older_unseen_message_does_not_roll_ownership_back(self, store):
        """Backfill can surface a message the applied-id set has never seen."""
        self._claim(store, BOT_C, "2000", 0.0)
        late = store.apply(
            _facts(mentioned_bot_ids=[BOT_A], message_id="1999"), _cfg(), now=1.0
        )
        assert late.verdict == "replay"
        assert late.reason == "older_than_current_owner_event"
        assert store.owner_of(THREAD) == BOT_C

    def test_a_newer_message_still_transfers(self, store):
        self._claim(store, BOT_C, "2000", 0.0)
        assert self._claim(store, BOT_A, "2001", 1.0).verdict == "transfer"
        assert store.owner_of(THREAD) == BOT_A

    def test_replay_of_an_intermediate_transfer_is_inert(self, store):
        self._claim(store, BOT_A, "1001", 0.0)
        self._claim(store, BOT_B, "1002", 1.0)
        self._claim(store, BOT_C, "1003", 2.0)
        for mid in ("1001", "1002"):
            store.apply(
                _facts(mentioned_bot_ids=[BOT_A], message_id=mid), _cfg(), now=3.0
            )
        assert store.owner_of(THREAD) == BOT_C

    def test_replay_still_reports_the_current_owner_for_admission(self, store):
        self._claim(store, BOT_C, "1002", 0.0)
        replay = store.apply(
            _facts(self_id=BOT_A, mentioned_bot_ids=[BOT_C], message_id="1002"),
            _cfg(),
            now=1.0,
        )
        assert replay.admit_self is False      # non-owner still silenced
        owner_view = store.apply(
            _facts(self_id=BOT_C, mentioned_bot_ids=[BOT_C], message_id="1002"),
            _cfg(),
            now=1.0,
        )
        assert owner_view.admit_self is None   # owner still deferred to base

    def test_applied_ids_survive_a_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        first = ThreadOwnershipStore()
        first.apply(
            _facts(mentioned_bot_ids=[BOT_C], message_id="5000"), _cfg(), now=0.0
        )
        reloaded = ThreadOwnershipStore()
        replay = reloaded.apply(
            _facts(mentioned_bot_ids=[BOT_A], message_id="4999"), _cfg(), now=1.0
        )
        assert replay.verdict == "replay"
        assert reloaded.owner_of(THREAD) == BOT_C

    def test_applied_id_memory_is_bounded(self, store):
        cfg = _cfg()
        for i in range(200):
            store.apply(
                _facts(
                    mentioned_bot_ids=[BOT_A if i % 2 else BOT_C],
                    message_id=str(9000 + i),
                ),
                cfg,
                now=float(i),
            )
        state = store._threads[THREAD]
        assert len(state.applied_message_ids) <= ownership._APPLIED_ID_MEMORY

    def test_legacy_state_format_still_loads(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "discord_thread_ownership.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "threads": {
                        THREAD: {
                            "owner_id": BOT_C,
                            "last_event_message_id": "3000",
                            "handoffs": [],
                        }
                    },
                }
            )
        )
        store = ThreadOwnershipStore()
        assert store.owner_of(THREAD) == BOT_C
        replay = store.apply(
            _facts(mentioned_bot_ids=[BOT_A], message_id="3000"), _cfg(), now=1.0
        )
        assert replay.verdict == "replay"


class TestReplayedAccessor:
    """``replayed()`` is what the adapter calls when core dedup already fired."""

    def test_it_does_not_move_ownership(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_C]), _cfg(), now=0.0)
        d = store.replayed(
            _facts(self_id=BOT_A, mentioned_bot_ids=[BOT_A], message_id="999"),
            _cfg(),
        )
        assert d.verdict == "replay"
        assert store.owner_of(THREAD) == BOT_C

    def test_it_still_vetoes_non_owners(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_C]), _cfg(), now=0.0)
        d = store.replayed(_facts(self_id=BOT_A, message_id="999"), _cfg())
        assert d.admit_self is False

    def test_it_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        ThreadOwnershipStore().replayed(_facts(), _cfg())
        assert not (tmp_path / "discord_thread_ownership.json").exists()


# ── Slice 5d: unresolved mentions must not diverge the bots ─────────────────


class TestUnresolvedMentions:
    def test_an_unresolved_mention_refuses_a_claim(self, store):
        d = store.apply(
            _facts(mentioned_bot_ids=[BOT_A], has_unresolved_mentions=True),
            _cfg(),
            now=0.0,
        )
        assert d.verdict == "none"
        assert d.reason == "unresolved_mention"
        assert store.owner_of(THREAD) is None

    def test_an_unresolved_mention_refuses_a_transfer(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        store.apply(
            _facts(
                message_id="102",
                mentioned_bot_ids=[BOT_C],
                has_unresolved_mentions=True,
            ),
            _cfg(),
            now=1.0,
        )
        assert store.owner_of(THREAD) == BOT_A

    def test_an_unresolved_mention_refuses_a_bot_handoff(self, store):
        store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(), now=0.0)
        d = store.apply(
            _facts(
                author_id=BOT_A,
                author_is_bot=True,
                message_id="102",
                mentioned_bot_ids=[BOT_C],
                content="[[handoff]] <@3000> <@4000>",
                has_unresolved_mentions=True,
            ),
            _cfg(),
            now=1.0,
        )
        assert d.reason == "unresolved_mention"
        assert store.owner_of(THREAD) == BOT_A

    def test_existing_ownership_still_governs_admission(self, store):
        """Refusing the change must not also drop the veto."""
        store.apply(_facts(mentioned_bot_ids=[BOT_C]), _cfg(), now=0.0)
        d = store.apply(
            _facts(
                self_id=BOT_A,
                message_id="102",
                mentioned_bot_ids=[BOT_A],
                has_unresolved_mentions=True,
            ),
            _cfg(),
            now=1.0,
        )
        assert d.admit_self is False


# ── Slice 5b: disabled behaviour is unchanged ───────────────────────────────


class TestDisabled:
    def test_disabled_engine_never_expresses_an_opinion(self, store):
        d = store.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(enabled=False), now=0.0)
        assert d.admit_self is None
        assert d.verdict == "disabled"

    def test_disabled_engine_writes_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        s = ThreadOwnershipStore()
        s.apply(_facts(mentioned_bot_ids=[BOT_A]), _cfg(enabled=False), now=0.0)
        assert not (tmp_path / "discord_thread_ownership.json").exists()
        assert s.owner_of(THREAD) is None


# ── Config parsing ──────────────────────────────────────────────────────────


class TestConfigParsing:
    def test_absent_config_is_disabled(self):
        assert OwnershipConfig.from_raw(None).enabled is False

    def test_bare_true_enables_with_defaults(self):
        cfg = OwnershipConfig.from_raw(True)
        assert cfg.enabled is True
        assert cfg.bot_handoff == "marker"

    def test_string_true_enables(self):
        assert OwnershipConfig.from_raw("yes").enabled is True

    def test_mapping_overrides_defaults(self):
        cfg = OwnershipConfig.from_raw(
            {"enabled": True, "bot_handoff": "mention", "max_handoffs": 2}
        )
        assert (cfg.enabled, cfg.bot_handoff, cfg.max_handoffs) == (True, "mention", 2)

    def test_mapping_without_enabled_defaults_to_on(self):
        """Writing the block at all is the opt-in signal."""
        assert OwnershipConfig.from_raw({"bot_handoff": "off"}).enabled is True

    def test_explicit_false_in_mapping_wins(self):
        assert OwnershipConfig.from_raw({"enabled": False}).enabled is False

    def test_unknown_handoff_mode_falls_back_to_marker(self):
        assert OwnershipConfig.from_raw({"bot_handoff": "bogus"}).bot_handoff == "marker"

    def test_garbage_numeric_fields_fall_back_to_defaults(self):
        cfg = OwnershipConfig.from_raw({"max_handoffs": "lots"})
        assert cfg.max_handoffs == OwnershipConfig().max_handoffs

    def test_negative_budget_is_clamped_to_zero(self):
        assert OwnershipConfig.from_raw({"max_handoffs": -5}).max_handoffs == 0

    def test_zero_window_is_clamped_to_a_positive_floor(self):
        """Both budget knobs must read the same direction: smaller is stricter.

        An unclamped window of 0 makes the "recent handoffs" filter match
        nothing, which silently *disables* the hop budget instead of
        tightening it — the opposite of max_handoffs: 0.
        """
        cfg = OwnershipConfig.from_raw({"handoff_window_seconds": 0})
        assert cfg.handoff_window_seconds > 0

    def test_zero_window_still_enforces_the_hop_budget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = ThreadOwnershipStore()
        cfg = OwnershipConfig.from_raw(
            {"handoff_window_seconds": 0, "max_handoffs": 1,
             "handoff_cooldown_seconds": 0}
        )
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        first = store.apply(
            _facts(author_id=BOT_A, author_is_bot=True, message_id="101",
                   mentioned_bot_ids=[BOT_C], content="[[handoff]] <@3000>"),
            cfg, now=0.1,
        )
        second = store.apply(
            _facts(author_id=BOT_C, author_is_bot=True, message_id="102",
                   mentioned_bot_ids=[BOT_B], content="[[handoff]] <@2000>"),
            cfg, now=0.2,
        )
        assert first.verdict == "handoff"
        assert second.verdict == "none"
        assert second.reason == "budget_exhausted"

    def test_zero_max_handoffs_freezes_bot_handoff(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = ThreadOwnershipStore()
        cfg = OwnershipConfig.from_raw({"max_handoffs": 0})
        store.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg, now=0.0)
        d = store.apply(
            _facts(author_id=BOT_A, author_is_bot=True, message_id="101",
                   mentioned_bot_ids=[BOT_C], content="[[handoff]] <@3000>"),
            cfg, now=1.0,
        )
        assert d.reason == "budget_exhausted"


class TestEnvOverridesMappingGuards:
    """``resolve`` — how the env bridge composes with the mapping form.

    The env var exists for parity with the other ``DISCORD_*`` gates, i.e. as
    an operator kill switch.  Treating a truthy value as a *whole config*
    replaced ``bot_handoff``, ``max_handoffs`` and the cooldown/window budget
    with defaults — silently re-opening bot-authored force-admission on a
    deployment that had set ``bot_handoff: off``.  A truthy env value may only
    move ``enabled``.
    """

    MAPPING = {
        "enabled": False,
        "bot_handoff": "off",
        "handoff_marker": "[[go]]",
        "max_handoffs": 2,
        "handoff_window_seconds": 60,
        "handoff_cooldown_seconds": 600,
    }

    def test_no_env_value_leaves_the_mapping_untouched(self):
        cfg = OwnershipConfig.resolve(None, dict(self.MAPPING, enabled=True))
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"
        assert cfg.max_handoffs == 2
        assert cfg.handoff_cooldown_seconds == 600

    def test_a_truthy_env_value_only_flips_enabled(self):
        cfg = OwnershipConfig.resolve("true", self.MAPPING)
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"           # the guard survives
        assert cfg.handoff_marker == "[[go]]"
        assert cfg.max_handoffs == 2
        assert cfg.handoff_window_seconds == 60
        assert cfg.handoff_cooldown_seconds == 600

    def test_a_truthy_env_value_cannot_reopen_bot_handoff(self):
        """The regression this test exists for."""
        cfg = OwnershipConfig.resolve("1", {"bot_handoff": "off"})
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"

    def test_a_falsey_env_value_is_still_a_kill_switch(self):
        cfg = OwnershipConfig.resolve("false", dict(self.MAPPING, enabled=True))
        assert cfg.enabled is False

    def test_the_kill_switch_beats_a_scalar_true_in_yaml(self):
        assert OwnershipConfig.resolve("false", True).enabled is False

    def test_a_truthy_env_value_with_no_yaml_uses_defaults(self):
        cfg = OwnershipConfig.resolve("yes", None)
        assert cfg.enabled is True
        assert cfg.bot_handoff == "marker"
        assert cfg.max_handoffs == 8

    def test_a_truthy_env_value_over_scalar_yaml_uses_defaults(self):
        cfg = OwnershipConfig.resolve("true", False)
        assert cfg.enabled is True
        assert cfg.bot_handoff == "marker"

    def test_an_unparseable_env_value_defers_to_yaml(self):
        """Neither truthy nor falsey: the operator did not express intent."""
        cfg = OwnershipConfig.resolve("maybe", dict(self.MAPPING, enabled=True))
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"

    def test_an_empty_env_value_defers_to_yaml(self):
        cfg = OwnershipConfig.resolve("", dict(self.MAPPING, enabled=True))
        assert cfg.bot_handoff == "off"

    def test_a_mapping_disabled_explicitly_stays_off_without_env(self):
        assert OwnershipConfig.resolve(None, self.MAPPING).enabled is False

    def test_nothing_anywhere_is_off(self):
        assert OwnershipConfig.resolve(None, None).enabled is False

    def test_a_real_bool_env_value_is_accepted(self):
        """``_gate_env`` returns strings, but ``config.extra`` may hold a bool."""
        cfg = OwnershipConfig.resolve(True, {"bot_handoff": "off"})
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"


class TestWriteOffTheEventLoop:
    """The durable write must never stall the Discord event loop.

    ``atomic_json_write`` is temp-file + fsync + os.replace, and ``apply()`` is
    reached from ``on_message``.  Inside a running loop the write is handed to
    a background thread; with no loop (CLI, cron, tests) it stays inline so
    "returned means persisted" still holds.
    """

    def _claim(self, store, cfg=None):
        return store.apply(
            _facts(mentioned_bot_ids=[BOT_A], message_id="100"), cfg or _cfg(), now=0.0
        )

    def test_without_a_loop_the_write_is_synchronous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = ThreadOwnershipStore()
        self._claim(store)
        # No flush(): the file must already be on disk.
        assert (tmp_path / "discord_thread_ownership.json").exists()

    def test_inside_a_loop_apply_does_not_wait_for_the_fsync(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import utils

        real_write = utils.atomic_json_write
        # Signal >> scheduler noise: the runner is busy (12 parallel workers),
        # so keep a wide margin between the injected stall and the assertion.
        slow = 1.5

        def slow_write(*a, **kw):
            time.sleep(slow)
            return real_write(*a, **kw)

        monkeypatch.setattr(utils, "atomic_json_write", slow_write)

        async def scenario():
            store = ThreadOwnershipStore()
            t0 = time.perf_counter()
            self._claim(store)
            elapsed = time.perf_counter() - t0
            return store, elapsed

        store, elapsed = asyncio.run(scenario())
        assert elapsed < 0.5, f"apply() blocked for {elapsed:.3f}s"
        assert store.flush(timeout=10.0)
        assert (tmp_path / "discord_thread_ownership.json").exists()
        store.close()

    def test_the_loop_keeps_running_while_the_write_is_in_flight(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import utils

        real_write = utils.atomic_json_write
        monkeypatch.setattr(
            utils,
            "atomic_json_write",
            lambda *a, **kw: (time.sleep(1.5), real_write(*a, **kw))[1],
        )

        async def scenario():
            store = ThreadOwnershipStore()
            gaps = []
            last = time.perf_counter()
            for i in range(20):
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now
                if i == 5:
                    # Trigger the write mid-loop. A synchronous fsync here is
                    # charged to the *next* gap, so do not reset the clock.
                    self._claim(store)
            return store, gaps

        store, gaps = asyncio.run(scenario())
        assert max(gaps) < 0.5, f"event loop stalled for {max(gaps):.3f}s"
        assert store.flush(timeout=10.0)
        store.close()

    def test_queued_writes_land_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        async def scenario():
            store = ThreadOwnershipStore()
            for i, bot in enumerate((BOT_A, BOT_B, BOT_C)):
                store.apply(
                    _facts(mentioned_bot_ids=[bot], message_id=str(200 + i)),
                    _cfg(),
                    now=float(i),
                )
            assert store.flush(timeout=10.0)
            store.close()

        asyncio.run(scenario())
        saved = json.loads((tmp_path / "discord_thread_ownership.json").read_text())
        assert saved["threads"][THREAD]["owner_id"] == BOT_C

    def test_flush_is_a_noop_when_nothing_was_queued(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert ThreadOwnershipStore().flush(timeout=0.1) is True

    def test_state_survives_a_close_after_async_writes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        async def scenario():
            store = ThreadOwnershipStore()
            self._claim(store)
            assert store.close(timeout=10.0)

        asyncio.run(scenario())
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_A

    def test_after_close_writes_fall_back_to_synchronous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        async def scenario():
            store = ThreadOwnershipStore()
            self._claim(store)
            store.close(timeout=10.0)
            # A late event must still be durable, not silently dropped.
            store.apply(
                _facts(mentioned_bot_ids=[BOT_C], message_id="300"), _cfg(), now=1.0
            )
            return store

        asyncio.run(scenario())
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_C

    def test_a_full_queue_falls_back_to_synchronous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = ThreadOwnershipStore()
        monkeypatch.setattr(store._writer, "submit", lambda path, snapshot: False)

        async def scenario():
            self._claim(store)

        asyncio.run(scenario())
        assert (tmp_path / "discord_thread_ownership.json").exists()
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_A

    def test_a_writer_thread_that_cannot_start_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        store = ThreadOwnershipStore()
        monkeypatch.setattr(
            ownership.threading, "Thread", MagicMock(side_effect=RuntimeError("nope"))
        )

        async def scenario():
            self._claim(store)

        asyncio.run(scenario())
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_A

    def test_a_failing_write_does_not_kill_the_writer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import utils

        real_write = utils.atomic_json_write
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk gone")
            return real_write(*a, **kw)

        monkeypatch.setattr(utils, "atomic_json_write", flaky)

        async def scenario():
            store = ThreadOwnershipStore()
            self._claim(store)
            store.flush(timeout=10.0)
            store.apply(
                _facts(mentioned_bot_ids=[BOT_C], message_id="300"), _cfg(), now=1.0
            )
            assert store.flush(timeout=10.0)
            store.close()

        asyncio.run(scenario())
        assert calls["n"] == 2
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_C

    def test_the_writer_thread_is_a_daemon(self, tmp_path, monkeypatch):
        """A stuck write must never keep the interpreter alive."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        async def scenario():
            store = ThreadOwnershipStore()
            self._claim(store)
            store.flush(timeout=10.0)
            return store._writer._thread

        thread = asyncio.run(scenario())
        assert thread is not None and thread.daemon

    def test_the_writer_never_mutates_in_memory_state(self, tmp_path, monkeypatch):
        """The event loop stays the sole mutator of _threads."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        other = ThreadOwnershipStore()
        other.apply(
            _facts(thread_id="999", mentioned_bot_ids=[BOT_B], message_id="400"),
            _cfg(),
            now=0.0,
        )

        async def scenario():
            store = ThreadOwnershipStore()
            store._threads.pop("999", None)      # pretend we never saw it
            self._claim(store)
            store.flush(timeout=10.0)
            store.close()
            return dict(store._threads)

        in_memory = asyncio.run(scenario())
        assert "999" not in in_memory            # writer did not write back
        # ...but the merge still preserved it on disk.
        saved = json.loads((tmp_path / "discord_thread_ownership.json").read_text())
        assert set(saved["threads"]) == {"999", THREAD}

    def test_async_writes_still_merge_a_concurrent_writer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        async def scenario():
            one = ThreadOwnershipStore()
            one.apply(
                _facts(thread_id="111", mentioned_bot_ids=[BOT_A], message_id="500"),
                _cfg(),
                now=0.0,
            )
            assert one.flush(timeout=10.0)
            two = ThreadOwnershipStore()
            two.apply(
                _facts(thread_id="222", mentioned_bot_ids=[BOT_C], message_id="600"),
                _cfg(),
                now=1.0,
            )
            assert two.flush(timeout=10.0)
            one.close()
            two.close()

        asyncio.run(scenario())
        saved = json.loads((tmp_path / "discord_thread_ownership.json").read_text())
        assert set(saved["threads"]) == {"111", "222"}

    def test_lru_bound_still_applies_through_the_async_path(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        async def scenario():
            store = ThreadOwnershipStore(max_threads=5)
            for i in range(20):
                store.apply(
                    _facts(
                        thread_id=str(i),
                        mentioned_bot_ids=[BOT_A],
                        message_id=str(700 + i),
                    ),
                    _cfg(),
                    now=float(i),
                )
            assert store.flush(timeout=10.0)
            store.close()

        asyncio.run(scenario())
        saved = json.loads((tmp_path / "discord_thread_ownership.json").read_text())
        assert len(saved["threads"]) == 5
        assert "19" in saved["threads"]


class TestMultiplexContextVarHome:
    """State must land in the profile that owns the adapter, not the process.

    Under ``gateway.multiplex_profiles`` the profile home is installed with
    ``set_hermes_home_override``, a ContextVar that deliberately does not touch
    ``os.environ``.  A ``threading.Thread`` starts with an empty context, so a
    background writer that resolves the path itself would silently target the
    process-global home — a secondary profile writing into the primary's file.
    Every other persistence test sets the env var, which is visible from any
    thread and therefore cannot catch this.
    """

    @pytest.fixture
    def homes(self, tmp_path, monkeypatch):
        primary, secondary = tmp_path / "primary", tmp_path / "secondary"
        primary.mkdir()
        secondary.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(primary))
        return primary, secondary

    @staticmethod
    def _scoped(secondary, fn):
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(secondary)
        try:
            return fn()
        finally:
            reset_hermes_home_override(token)

    def _claim(self, store):
        return store.apply(
            _facts(mentioned_bot_ids=[BOT_C], message_id="100"), _cfg(), now=0.0
        )

    def test_async_write_lands_in_the_overridden_profile(self, homes):
        primary, secondary = homes

        async def scenario():
            def body():
                store = ThreadOwnershipStore()
                self._claim(store)
                assert store.flush(timeout=10.0)
                store.close()

            self._scoped(secondary, body)

        asyncio.run(scenario())
        assert (secondary / "discord_thread_ownership.json").exists()
        assert not (primary / "discord_thread_ownership.json").exists()

    def test_sync_write_lands_in_the_overridden_profile(self, homes):
        primary, secondary = homes

        def body():
            self._claim(ThreadOwnershipStore())

        self._scoped(secondary, body)
        assert (secondary / "discord_thread_ownership.json").exists()
        assert not (primary / "discord_thread_ownership.json").exists()

    def test_the_merge_read_uses_the_same_profile_as_the_write(self, homes):
        """A split between read and write would silently drop other threads."""
        primary, secondary = homes

        def seed():
            store = ThreadOwnershipStore()
            store.apply(
                _facts(thread_id="111", mentioned_bot_ids=[BOT_A], message_id="90"),
                _cfg(),
                now=0.0,
            )
            store.close()

        self._scoped(secondary, seed)

        async def scenario():
            def body():
                store = ThreadOwnershipStore()
                self._claim(store)
                assert store.flush(timeout=10.0)
                store.close()

            self._scoped(secondary, body)

        asyncio.run(scenario())
        saved = json.loads(
            (secondary / "discord_thread_ownership.json").read_text()
        )
        # Both threads present → the merge read hit the secondary profile too.
        assert set(saved["threads"]) == {"111", THREAD}
        assert not (primary / "discord_thread_ownership.json").exists()

    def test_two_profiles_in_one_process_stay_separate(self, homes):
        """The multiplex shape: two scoped stores, one process, one loop."""
        primary, secondary = homes

        async def scenario():
            # Primary profile: no override installed, uses HERMES_HOME.
            a = ThreadOwnershipStore()
            a.apply(
                _facts(mentioned_bot_ids=[BOT_A], message_id="100"), _cfg(), now=0.0
            )

            def body():
                b = ThreadOwnershipStore()
                b.apply(
                    _facts(mentioned_bot_ids=[BOT_B], message_id="100"),
                    _cfg(),
                    now=0.0,
                )
                assert b.flush(timeout=10.0)
                b.close()

            self._scoped(secondary, body)
            assert a.flush(timeout=10.0)
            a.close()

        asyncio.run(scenario())
        primary_state = json.loads(
            (primary / "discord_thread_ownership.json").read_text()
        )
        secondary_state = json.loads(
            (secondary / "discord_thread_ownership.json").read_text()
        )
        assert primary_state["threads"][THREAD]["owner_id"] == BOT_A
        assert secondary_state["threads"][THREAD]["owner_id"] == BOT_B

    def test_atexit_drain_targets_the_right_profile(self, homes):
        primary, secondary = homes

        async def scenario():
            def body():
                store = ThreadOwnershipStore()
                self._claim(store)
                return store

            return self._scoped(secondary, body)

        store = asyncio.run(scenario())
        ownership._flush_all_stores()
        assert (secondary / "discord_thread_ownership.json").exists()
        assert not (primary / "discord_thread_ownership.json").exists()
        assert store.owner_of(THREAD) == BOT_C


class TestConcurrentWriters:
    """Two stores on one profile must not silently delete each other's threads.

    The class holds a long-lived in-memory snapshot, so a plain full-file
    rewrite would drop whatever another writer added after we loaded.
    """

    def test_a_second_writer_does_not_lose_the_first_ones_threads(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        one = ThreadOwnershipStore()
        two = ThreadOwnershipStore()

        one.apply(
            _facts(thread_id="111", mentioned_bot_ids=[BOT_A], message_id="100"),
            _cfg(), now=0.0,
        )
        two.apply(
            _facts(thread_id="222", mentioned_bot_ids=[BOT_C], message_id="200"),
            _cfg(), now=1.0,
        )

        saved = json.loads((tmp_path / "discord_thread_ownership.json").read_text())
        assert set(saved["threads"]) == {"111", "222"}
        fresh = ThreadOwnershipStore()
        assert fresh.owner_of("111") == BOT_A
        assert fresh.owner_of("222") == BOT_C

    def test_our_own_thread_wins_over_a_stale_on_disk_copy(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        one = ThreadOwnershipStore()
        two = ThreadOwnershipStore()
        one.apply(_facts(mentioned_bot_ids=[BOT_A], message_id="100"), _cfg(), now=0.0)
        # `two` loaded before that write and now decides on the same thread.
        two.apply(_facts(mentioned_bot_ids=[BOT_C], message_id="200"), _cfg(), now=1.0)
        assert ThreadOwnershipStore().owner_of(THREAD) == BOT_C
