"""DiscordCustomAdapter admission integration.

Exercises the real adapter subclass against the real built-in
``_discord_message_admission``, so these tests cover the seam between the
ownership engine and the stock Discord gate rather than mocking it away.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from conftest import load_plugin_module

adapter_mod = load_plugin_module("adapter")
DiscordCustomAdapter = adapter_mod.DiscordCustomAdapter


SELF_ID = 1000
BOT_B = 2000
BOT_C = 3000
HUMAN = 9001
THREAD_ID = 77


def _user(uid, *, bot=False):
    u = MagicMock()
    u.id = uid
    u.bot = bot
    u.name = f"user{uid}"
    u.display_name = u.name
    return u


def _thread_channel():
    ch = MagicMock(spec=discord.Thread)
    ch.id = THREAD_ID
    ch.name = "a-thread"
    ch.parent_id = 55
    ch.parent = MagicMock()
    ch.parent.id = 55          # _get_parent_channel_id() reads channel.parent.id
    ch.parent.name = "parent-chan"
    return ch


def _message(
    *,
    author,
    content="",
    mentions=(),
    channel=None,
    message_id=1,
    msg_type=None,
):
    m = MagicMock()
    m.id = message_id
    m.author = author
    m.content = content
    m.mentions = list(mentions)
    m.attachments = []
    m.channel = channel if channel is not None else _thread_channel()
    m.guild = MagicMock()
    m.guild.id = 4242
    # A bare MagicMock would resolve *every* id to a truthy member whose .bot
    # is also truthy. Default to "not in cache" so tests must opt in.
    m.guild.get_member = lambda uid: None
    m.type = msg_type if msg_type is not None else discord.MessageType.default
    return m


def _make_adapter(tmp_path, extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, token="test-token")
    cfg.extra = dict(extra or {})
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        adapter = DiscordCustomAdapter(config=cfg)
    adapter._client = MagicMock()
    adapter._client.user = _user(SELF_ID, bot=True)
    adapter._client.get_user = lambda uid: None   # see _message()
    # Allow every human through; authorization is the built-in adapter's job
    # and is covered by its own tests.
    adapter._is_allowed_user = lambda *a, **k: True
    adapter._test_hermes_home = str(tmp_path)   # test-only; see _admit()
    return adapter


@pytest.fixture
def enabled(tmp_path):
    return _make_adapter(tmp_path, {"thread_ownership_transfer": True})


@pytest.fixture
def disabled(tmp_path):
    return _make_adapter(tmp_path)


def _admit(adapter, message, claim=True):
    # The ownership store is built lazily on first use, so HERMES_HOME must
    # point at the temp dir at admission time, not just at construction time.
    with patch.dict(os.environ, {"HERMES_HOME": adapter._test_hermes_home}):
        return adapter._discord_message_admission(message, claim=claim)[0]


# ── disabled: byte-for-byte the built-in behaviour ──────────────────────────


class TestDisabledIsUnchanged:
    def test_mentioned_human_message_is_admitted(self, disabled):
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{SELF_ID}> hi",
            mentions=[_user(SELF_ID, bot=True)],
        )
        assert _admit(disabled, msg) is True

    def test_no_state_file_is_written(self, disabled, tmp_path):
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{SELF_ID}> hi",
            mentions=[_user(SELF_ID, bot=True)],
        )
        _admit(disabled, msg)
        assert not (tmp_path / "discord_thread_ownership.json").exists()

    def test_other_bot_mention_is_dropped_exactly_as_before(self, disabled):
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> hi",
            mentions=[_user(BOT_C, bot=True)],
        )
        assert _admit(disabled, msg) is False

    def test_matches_the_builtin_verdict_on_a_range_of_messages(self, tmp_path):
        """Differential check against the stock adapter, same inputs."""
        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        cfg = PlatformConfig(enabled=True, token="test-token")
        cfg.extra = {}
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            builtin = DiscordAdapter(config=cfg)
        builtin._client = MagicMock()
        builtin._client.user = _user(SELF_ID, bot=True)
        builtin._is_allowed_user = lambda *a, **k: True
        custom = _make_adapter(tmp_path)

        cases = [
            dict(author=_user(HUMAN), content="plain text"),
            dict(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> hello",
                mentions=[_user(SELF_ID, bot=True)],
            ),
            dict(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> hello",
                mentions=[_user(BOT_C, bot=True)],
            ),
            dict(author=_user(BOT_B, bot=True), content=f"<@{SELF_ID}> yo",
                 mentions=[_user(SELF_ID, bot=True)]),
        ]
        for i, kw in enumerate(cases):
            a = builtin._discord_message_admission(
                _message(message_id=100 + i, **kw), claim=True
            )
            b = custom._discord_message_admission(
                _message(message_id=100 + i, **kw), claim=True
            )
            assert a == b, kw["content"]


# ── enabled: claim, silence, transfer ───────────────────────────────────────


class TestEnabledAdmission:
    def _claim(self, adapter, mid=1):
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{SELF_ID}> take this",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=mid,
        )
        return _admit(adapter, msg)

    def test_mention_claims_and_is_admitted(self, enabled):
        assert self._claim(enabled) is True
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(SELF_ID)

    def test_owner_answers_an_unmentioned_follow_up(self, enabled):
        self._claim(enabled)
        follow = _message(author=_user(HUMAN), content="and then?", message_id=2)
        assert _admit(enabled, follow) is True

    def test_non_owner_is_silenced_on_an_unmentioned_follow_up(self, tmp_path):
        """The regression this feature exists to fix."""
        other = _make_adapter(tmp_path, {"thread_ownership_transfer": True})
        claim = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> take this",
            mentions=[_user(BOT_C, bot=True)],
            message_id=1,
        )
        _admit(other, claim)
        # Without ownership this would be admitted: the bot has participated in
        # the thread and no mention is required for thread follow-ups.
        other._threads.mark(str(THREAD_ID))
        follow = _message(author=_user(HUMAN), content="and then?", message_id=2)
        assert _admit(other, follow) is False

    def test_transfer_moves_the_owner(self, enabled):
        self._claim(enabled)
        transfer = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> you take over",
            mentions=[_user(BOT_C, bot=True)],
            message_id=2,
        )
        assert _admit(enabled, transfer) is False
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_old_owner_stays_silent_after_the_transfer(self, enabled):
        self._claim(enabled)
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> you take over",
                mentions=[_user(BOT_C, bot=True)],
                message_id=2,
            ),
        )
        follow = _message(author=_user(HUMAN), content="still here?", message_id=3)
        assert _admit(enabled, follow) is False

    def test_mentioning_a_human_does_not_transfer(self, enabled):
        self._claim(enabled)
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{HUMAN}> thoughts?",
            mentions=[_user(HUMAN)],
            message_id=2,
        )
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(SELF_ID)

    def test_unauthorized_human_cannot_move_ownership(self, enabled):
        self._claim(enabled)
        enabled._is_allowed_user = lambda *a, **k: False
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> you take over",
            mentions=[_user(BOT_C, bot=True)],
            message_id=2,
        )
        assert _admit(enabled, msg) is False
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(SELF_ID)

    def test_reply_ping_alone_does_not_claim(self, enabled):
        """A Discord reply chip resolves into mentions with no inline token."""
        msg = _message(
            author=_user(HUMAN),
            content="thanks!",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=1,
        )
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) is None


# ── enabled: scope is threads only ──────────────────────────────────────────


class TestScope:
    def test_plain_channel_is_untouched(self, enabled, tmp_path):
        channel = MagicMock()
        channel.id = 555
        channel.name = "general"
        type(channel).__name__ = "TextChannel"
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{SELF_ID}> hi",
            mentions=[_user(SELF_ID, bot=True)],
            channel=channel,
        )
        assert _admit(enabled, msg) is True
        assert not (tmp_path / "discord_thread_ownership.json").exists()

    def test_dm_is_untouched(self, enabled, tmp_path):
        channel = MagicMock(spec=discord.DMChannel)
        channel.id = 666
        msg = _message(author=_user(HUMAN), content="hi", channel=channel)
        msg.guild = None
        assert _admit(enabled, msg) is True
        assert not (tmp_path / "discord_thread_ownership.json").exists()


# ── enabled: bot-to-bot handoff through the real gate ───────────────────────


class TestHandoffAdmission:
    def test_handoff_target_is_admitted_despite_allow_bots_none(self, enabled):
        """allow_bots defaults to "none"; a validated handoff must still land."""
        assert enabled._get_allow_bots() == "none"
        owner_claim = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> start",
            mentions=[_user(BOT_C, bot=True)],
            message_id=1,
        )
        _admit(enabled, owner_claim)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

        handoff = _message(
            author=_user(BOT_C, bot=True),
            content=f"[[handoff]] <@{SELF_ID}> your turn",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=2,
        )
        assert _admit(enabled, handoff) is True
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(SELF_ID)

    def test_allow_bots_gate_is_restored_after_the_call(self, enabled):
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> start",
                mentions=[_user(BOT_C, bot=True)],
                message_id=1,
            ),
        )
        _admit(
            enabled,
            _message(
                author=_user(BOT_C, bot=True),
                content=f"[[handoff]] <@{SELF_ID}> your turn",
                mentions=[_user(SELF_ID, bot=True)],
                message_id=2,
            ),
        )
        assert enabled._get_allow_bots() == "none"

    def test_unmarked_bot_mention_is_still_dropped(self, enabled):
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> start",
                mentions=[_user(BOT_C, bot=True)],
                message_id=1,
            ),
        )
        chatter = _message(
            author=_user(BOT_C, bot=True),
            content=f"maybe <@{SELF_ID}> knows",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=2,
        )
        assert _admit(enabled, chatter) is False
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_non_owner_bot_marker_is_ignored(self, enabled):
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> start",
                mentions=[_user(BOT_C, bot=True)],
                message_id=1,
            ),
        )
        forged = _message(
            author=_user(BOT_B, bot=True),
            content=f"[[handoff]] <@{SELF_ID}>",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=2,
        )
        assert _admit(enabled, forged) is False
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_own_outbound_handoff_is_observed(self, enabled):
        """The owner sees its own message and records that it handed off."""
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> start",
                mentions=[_user(SELF_ID, bot=True)],
                message_id=1,
            ),
        )
        mine = _message(
            author=enabled._client.user,
            content=f"[[handoff]] <@{BOT_C}> over to you",
            mentions=[_user(BOT_C, bot=True)],
            message_id=2,
        )
        assert _admit(enabled, mine) is False  # never reply to ourselves
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_after_handing_off_we_ignore_follow_ups(self, enabled):
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> start",
                mentions=[_user(SELF_ID, bot=True)],
                message_id=1,
            ),
        )
        _admit(
            enabled,
            _message(
                author=enabled._client.user,
                content=f"[[handoff]] <@{BOT_C}>",
                mentions=[_user(BOT_C, bot=True)],
                message_id=2,
            ),
        )
        enabled._threads.mark(str(THREAD_ID))
        follow = _message(author=_user(HUMAN), content="thanks", message_id=3)
        assert _admit(enabled, follow) is False


# ── mention resolution: all bots must reach the same winner ─────────────────


class TestMentionResolution:
    """`message.mentions` is not always populated (mobile / edited / relayed).

    The adapter must resolve inline ids from the guild/user caches instead of
    falling back to "only I am a bot", which would let two bots each conclude
    they own the thread — the exact double-answer bug this feature removes.
    """

    def _with_guild_members(self, adapter, members):
        guild = MagicMock()
        guild.get_member = lambda uid: members.get(uid)
        return guild

    def test_resolves_via_the_guild_cache_when_mentions_is_empty(self, enabled):
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> take it",
            mentions=[],
        )
        msg.guild = self._with_guild_members(
            enabled, {BOT_C: _user(BOT_C, bot=True)}
        )
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_resolves_via_the_client_user_cache(self, enabled):
        enabled._client.get_user = lambda uid: (
            _user(BOT_C, bot=True) if uid == BOT_C else None
        )
        msg = _message(
            author=_user(HUMAN), content=f"<@{BOT_C}> take it", mentions=[]
        )
        msg.guild.get_member = lambda uid: None
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_unresolvable_mention_makes_no_ownership_change(self, enabled):
        enabled._client.get_user = lambda uid: None
        msg = _message(author=_user(HUMAN), content="<@777777> hi", mentions=[])
        msg.guild.get_member = lambda uid: None
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) is None

    def test_a_resolvable_bot_alongside_an_unresolvable_id_is_refused(self, enabled):
        """Ambiguity: the unknown id might be a bot that should have won."""
        enabled._client.get_user = lambda uid: None
        msg = _message(
            author=_user(HUMAN),
            content=f"<@777777> <@{SELF_ID}>",
            mentions=[_user(SELF_ID, bot=True)],
        )
        msg.guild.get_member = lambda uid: None
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) is None

    def test_a_mentioned_human_is_resolved_and_ignored(self, enabled):
        msg = _message(
            author=_user(HUMAN), content=f"<@{HUMAN}> hi", mentions=[]
        )
        msg.guild = self._with_guild_members(enabled, {HUMAN: _user(HUMAN)})
        _admit(enabled, msg)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) is None


class TestThreeAdapterConvergence:
    """Three real adapters, three profiles, one raw payload.

    Deliberately does NOT hand each bot a pre-built mentioned_bot_ids tuple:
    each adapter derives its own view from the same message object, which is
    the only way this can catch a divergence in resolution.
    """

    def _fleet(self, tmp_path, *, guild_members):
        adapters = []
        for i, uid in enumerate((SELF_ID, BOT_B, BOT_C)):
            home = tmp_path / f"profile{i}"
            home.mkdir()
            a = _make_adapter(home, {"thread_ownership_transfer": True})
            a._client.user = _user(uid, bot=True)
            a._client.get_user = lambda x: None
            adapters.append(a)
        return adapters

    def _payload(self, content, mentions, members, message_id=1):
        msg = _message(
            author=_user(HUMAN),
            content=content,
            mentions=mentions,
            message_id=message_id,
        )
        msg.guild = MagicMock()
        msg.guild.get_member = lambda uid: members.get(uid)
        return msg

    def test_all_three_agree_with_a_populated_mentions_list(self, tmp_path):
        members = {b: _user(b, bot=True) for b in (SELF_ID, BOT_B, BOT_C)}
        fleet = self._fleet(tmp_path, guild_members=members)
        owners = []
        for a in fleet:
            _admit(
                a,
                self._payload(
                    f"<@{BOT_B}> and <@{BOT_C}>",
                    [_user(BOT_B, bot=True), _user(BOT_C, bot=True)],
                    members,
                ),
            )
            owners.append(a._ownership_store.owner_of(str(THREAD_ID)))
        assert owners == [str(BOT_B)] * 3

    def test_all_three_agree_with_an_empty_mentions_list(self, tmp_path):
        """The regression: previously each bot resolved only itself."""
        members = {b: _user(b, bot=True) for b in (SELF_ID, BOT_B, BOT_C)}
        fleet = self._fleet(tmp_path, guild_members=members)
        owners = []
        for a in fleet:
            _admit(a, self._payload(f"<@{BOT_C}> <@{SELF_ID}>", [], members))
            owners.append(a._ownership_store.owner_of(str(THREAD_ID)))
        assert owners == [str(BOT_C)] * 3

    def test_exactly_one_of_the_three_answers(self, tmp_path):
        members = {b: _user(b, bot=True) for b in (SELF_ID, BOT_B, BOT_C)}
        fleet = self._fleet(tmp_path, guild_members=members)

        def claim():
            return self._payload(f"<@{BOT_C}> hi", [], members)

        for a in fleet:
            _admit(a, claim())
        # Every bot has participated in the thread, so without ownership all
        # three would answer the unmentioned follow-up.
        answered = []
        for a in fleet:
            a._threads.mark(str(THREAD_ID))
            follow = _message(
                author=_user(HUMAN), content="and then?", message_id=2
            )
            follow.guild = MagicMock()
            follow.guild.get_member = lambda uid: members.get(uid)
            if _admit(a, follow):
                answered.append(str(a._client.user.id))
        assert answered == [str(BOT_C)]

    def test_all_three_refuse_together_on_an_unresolvable_mention(self, tmp_path):
        fleet = self._fleet(tmp_path, guild_members={})
        for a in fleet:
            _admit(a, self._payload("<@424242> hi", [], {}))
        assert [a._ownership_store.owner_of(str(THREAD_ID)) for a in fleet] == [
            None
        ] * 3


# ── documented constraint: profiles must share one allowlist ────────────────


class TestAllowlistConvergence:
    """Ownership is only a pure function of the message *given equal trust*.

    Observation is gated on each profile's own DISCORD_ALLOWED_USERS, so two
    profiles with different allowlists can disagree. These tests pin the real
    behaviour so the README prerequisite is enforced, not just asserted.
    """

    def _pair(self, tmp_path, *, second_trusts_human):
        first = _make_adapter(tmp_path / "a", {"thread_ownership_transfer": True})
        second = _make_adapter(tmp_path / "b", {"thread_ownership_transfer": True})
        second._client.user = _user(BOT_C, bot=True)
        second._is_allowed_user = lambda *a, **k: second_trusts_human
        return first, second

    @pytest.fixture(autouse=True)
    def _homes(self, tmp_path):
        (tmp_path / "a").mkdir(exist_ok=True)
        (tmp_path / "b").mkdir(exist_ok=True)

    def test_shared_allowlist_converges(self, tmp_path):
        first, second = self._pair(tmp_path, second_trusts_human=True)

        def msg():
            return _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> take over",
                mentions=[_user(BOT_C, bot=True)],
            )

        _admit(first, msg())
        _admit(second, msg())
        assert first._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)
        assert second._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_divergent_allowlists_diverge_as_documented(self, tmp_path):
        """Known constraint, not a silent one: see README prerequisites."""
        first, second = self._pair(tmp_path, second_trusts_human=False)

        def msg():
            return _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> take over",
                mentions=[_user(BOT_C, bot=True)],
            )

        _admit(first, msg())
        _admit(second, msg())
        assert first._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)
        # second never observed the message, so it records no owner.
        assert second._ownership_store.owner_of(str(THREAD_ID)) is None

    def test_the_untrusting_profile_falls_back_to_builtin_behaviour(self, tmp_path):
        """Divergence degrades to the stock adapter, it does not deadlock."""
        first, second = self._pair(tmp_path, second_trusts_human=False)
        msg = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> take over",
            mentions=[_user(BOT_C, bot=True)],
        )
        _admit(first, msg)
        second._is_allowed_user = lambda *a, **k: True
        second._threads.mark(str(THREAD_ID))
        follow = _message(author=_user(HUMAN), content="and then?", message_id=2)
        # No owner recorded → no ownership opinion → built-in rules decide.
        assert _admit(second, follow) is True
        assert _admit(first, follow) is False


# ── replay / redelivery at the adapter seam ─────────────────────────────────


class TestRedelivery:
    def test_core_dedup_stops_ownership_moving_on_redelivery(self, enabled):
        """`_dispatch_recovered_message` re-runs seen messages through here."""
        first = _message(
            author=_user(HUMAN),
            content=f"<@{SELF_ID}> mine",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=1001,
        )
        _admit(enabled, first)
        transfer = _message(
            author=_user(HUMAN),
            content=f"<@{BOT_C}> yours",
            mentions=[_user(BOT_C, bot=True)],
            message_id=1002,
        )
        _admit(enabled, transfer)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

        # Backfill re-runs m1001 with claim=False, exactly as the adapter does.
        _admit(enabled, first, claim=False)
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_an_older_unseen_message_does_not_roll_back(self, enabled):
        """Beyond the dedup TTL, snowflake ordering is the guard."""
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{BOT_C}> yours",
                mentions=[_user(BOT_C, bot=True)],
                message_id=5000,
            ),
        )
        enabled._dedup._seen.clear()          # simulate TTL expiry
        _admit(
            enabled,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> mine",
                mentions=[_user(SELF_ID, bot=True)],
                message_id=4999,
            ),
        )
        assert enabled._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)


# ── channel scope ───────────────────────────────────────────────────────────


class TestChannelScope:
    def test_ignored_channel_records_no_ownership(self, tmp_path):
        a = _make_adapter(
            tmp_path,
            {
                "thread_ownership_transfer": True,
                "ignored_channels": str(THREAD_ID),
            },
        )
        _admit(
            a,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> hi",
                mentions=[_user(SELF_ID, bot=True)],
            ),
        )
        assert a._ownership_store.owner_of(str(THREAD_ID)) is None

    def test_ignored_parent_channel_records_no_ownership(self, tmp_path):
        a = _make_adapter(
            tmp_path,
            {"thread_ownership_transfer": True, "ignored_channels": "55"},
        )
        _admit(
            a,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> hi",
                mentions=[_user(SELF_ID, bot=True)],
            ),
        )
        assert a._ownership_store.owner_of(str(THREAD_ID)) is None

    def test_channel_outside_the_allowlist_records_no_ownership(self, tmp_path):
        a = _make_adapter(
            tmp_path,
            {"thread_ownership_transfer": True, "allowed_channels": "999"},
        )
        _admit(
            a,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> hi",
                mentions=[_user(SELF_ID, bot=True)],
            ),
        )
        assert a._ownership_store.owner_of(str(THREAD_ID)) is None

    def test_allowlisted_channel_still_records(self, tmp_path):
        a = _make_adapter(
            tmp_path,
            {
                "thread_ownership_transfer": True,
                "allowed_channels": str(THREAD_ID),
            },
        )
        _admit(
            a,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> hi",
                mentions=[_user(SELF_ID, bot=True)],
            ),
        )
        assert a._ownership_store.owner_of(str(THREAD_ID)) == str(SELF_ID)


# ── shutdown drains queued ownership writes ─────────────────────────────────


class TestDisconnectFlush:
    """State writes are dispatched off the event loop; a restart must not race
    them."""

    def _claim(self, adapter):
        return _admit(
            adapter,
            _message(
                author=_user(HUMAN),
                content=f"<@{SELF_ID}> mine",
                mentions=[_user(SELF_ID, bot=True)],
            ),
        )

    def test_disconnect_flushes_pending_writes(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"thread_ownership_transfer": True})

        async def scenario():
            self._claim(adapter)
            with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
                await adapter.disconnect()

        with patch.object(
            DiscordCustomAdapter.__mro__[1], "disconnect", new=AsyncMock()
        ):
            asyncio.run(scenario())

        saved = json.loads(
            (tmp_path / "discord_thread_ownership.json").read_text()
        )
        assert saved["threads"][str(THREAD_ID)]["owner_id"] == str(SELF_ID)

    def test_disconnect_still_tears_down_when_the_flush_fails(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"thread_ownership_transfer": True})
        self._claim(adapter)
        adapter._ownership_store_cache.close = MagicMock(
            side_effect=RuntimeError("boom")
        )
        base_disconnect = AsyncMock()

        with patch.object(
            DiscordCustomAdapter.__mro__[1], "disconnect", new=base_disconnect
        ):
            asyncio.run(adapter.disconnect())

        base_disconnect.assert_awaited_once()

    def test_disconnect_is_safe_when_ownership_was_never_used(self, tmp_path):
        adapter = _make_adapter(tmp_path)          # feature disabled
        base_disconnect = AsyncMock()
        with patch.object(
            DiscordCustomAdapter.__mro__[1], "disconnect", new=base_disconnect
        ):
            asyncio.run(adapter.disconnect())
        assert adapter._ownership_store_cache is None
        base_disconnect.assert_awaited_once()


# ── config resolution on the adapter ────────────────────────────────────────


class TestAdapterConfig:
    def test_extra_mapping_is_honoured(self, tmp_path):
        a = _make_adapter(
            tmp_path,
            {"thread_ownership_transfer": {"bot_handoff": "off", "max_handoffs": 3}},
        )
        cfg = a._ownership_config()
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"
        assert cfg.max_handoffs == 3

    def test_env_bridge_enables_the_feature(self, tmp_path):
        a = _make_adapter(tmp_path)
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            assert a._ownership_config().enabled is True

    def test_env_false_is_a_kill_switch_over_yaml(self, tmp_path):
        """Env-first, matching every other Discord gate via _gate_raw."""
        a = _make_adapter(tmp_path, {"thread_ownership_transfer": True})
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "false"}):
            assert a._ownership_config().enabled is False

    def test_yaml_applies_when_no_env_var_is_set(self, tmp_path):
        a = _make_adapter(tmp_path, {"thread_ownership_transfer": True})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_THREAD_OWNERSHIP_TRANSFER", None)
            assert a._ownership_config().enabled is True

    def test_default_is_off(self, tmp_path):
        assert _make_adapter(tmp_path)._ownership_config().enabled is False


class TestEnvFlagPreservesMappingGuards:
    """M1: a truthy env flag must not silently discard the mapping form.

    ``config.extra`` is the profile-scoped carrier the YAML bridge writes, and
    the env var is the operator kill switch.  Composing them by "env wins
    wholesale" meant ``DISCORD_THREAD_OWNERSHIP_TRANSFER=true`` reset
    ``bot_handoff: off`` to ``marker`` — re-opening the bot ingress path an
    operator had deliberately closed.
    """

    GUARDED = {
        "thread_ownership_transfer": {
            "enabled": False,
            "bot_handoff": "off",
            "max_handoffs": 2,
            "handoff_cooldown_seconds": 600,
        }
    }

    def test_truthy_env_enables_without_touching_the_guards(self, tmp_path):
        a = _make_adapter(tmp_path, self.GUARDED)
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            cfg = a._ownership_config()
        assert cfg.enabled is True
        assert cfg.bot_handoff == "off"
        assert cfg.max_handoffs == 2
        assert cfg.handoff_cooldown_seconds == 600

    def test_falsey_env_remains_a_kill_switch(self, tmp_path):
        a = _make_adapter(
            tmp_path, {"thread_ownership_transfer": {"enabled": True}}
        )
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "false"}):
            assert a._ownership_config().enabled is False

    def test_a_bot_handoff_off_thread_refuses_a_marker_under_a_truthy_env(
        self, tmp_path
    ):
        """End to end: the guard actually holds at admission, not just in config."""
        a = _make_adapter(tmp_path, self.GUARDED)
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            _admit(
                a,
                _message(
                    author=_user(HUMAN),
                    content=f"<@{BOT_C}> start",
                    mentions=[_user(BOT_C, bot=True)],
                    message_id=1,
                ),
            )
            handoff = _message(
                author=_user(BOT_C, bot=True),
                content=f"[[handoff]] <@{SELF_ID}> your turn",
                mentions=[_user(SELF_ID, bot=True)],
                message_id=2,
            )
            # bot_handoff="off" survived, so no bot-authored message is
            # force-admitted past DISCORD_ALLOW_BOTS=none.
            assert _admit(a, handoff) is False
        assert a._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_env_only_still_enables_with_defaults(self, tmp_path):
        a = _make_adapter(tmp_path)
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            cfg = a._ownership_config()
        assert cfg.enabled is True
        assert cfg.bot_handoff == "marker"



class TestBotHandoffOffPreservesTheStockBoundary:
    """The README's security note, asserted rather than promised.

    ``bot_handoff: off`` must never force-admit a bot-authored message, so
    ``DISCORD_ALLOW_BOTS`` keeps meaning exactly what it means without this
    plugin.  Ownership still silences non-owners on human messages.
    """

    OFF = {"thread_ownership_transfer": {"enabled": True, "bot_handoff": "off"}}

    def _own_the_thread(self, adapter, owner=None):
        owner = owner or BOT_C
        _admit(
            adapter,
            _message(
                author=_user(HUMAN),
                content=f"<@{owner}> start",
                mentions=[_user(owner, bot=True)],
                message_id=1,
            ),
        )

    def test_an_owner_marker_handoff_is_not_admitted(self, tmp_path):
        a = _make_adapter(tmp_path, self.OFF)
        self._own_the_thread(a)
        handoff = _message(
            author=_user(BOT_C, bot=True),
            content=f"[[handoff]] <@{SELF_ID}> your turn",
            mentions=[_user(SELF_ID, bot=True)],
            message_id=2,
        )
        assert _admit(a, handoff) is False

    def test_ownership_does_not_move_to_us(self, tmp_path):
        a = _make_adapter(tmp_path, self.OFF)
        self._own_the_thread(a)
        _admit(
            a,
            _message(
                author=_user(BOT_C, bot=True),
                content=f"[[handoff]] <@{SELF_ID}> your turn",
                mentions=[_user(SELF_ID, bot=True)],
                message_id=2,
            ),
        )
        assert a._ownership_store.owner_of(str(THREAD_ID)) == str(BOT_C)

    def test_the_allow_bots_gate_is_never_widened(self, tmp_path):
        """No env write and no forced mode, for any bot message in the thread."""
        a = _make_adapter(tmp_path, self.OFF)
        self._own_the_thread(a)
        seen = []
        real = adapter_mod.compat.forced_allow_bots
        with patch.object(
            adapter_mod.compat,
            "forced_allow_bots",
            side_effect=lambda active: seen.append(active) or real(active),
        ):
            _admit(
                a,
                _message(
                    author=_user(BOT_C, bot=True),
                    content=f"[[handoff]] <@{SELF_ID}> your turn",
                    mentions=[_user(SELF_ID, bot=True)],
                    message_id=2,
                ),
            )
        assert True not in seen
        assert a._get_allow_bots() == "none"

    def test_human_side_ownership_still_silences_non_owners(self, tmp_path):
        """The half of the feature that needs no widening at all."""
        a = _make_adapter(tmp_path, self.OFF)
        self._own_the_thread(a)
        follow_up = _message(
            author=_user(HUMAN), content="and then?", message_id=3
        )
        assert _admit(a, follow_up) is False

    def test_the_owner_still_answers_its_own_thread(self, tmp_path):
        a = _make_adapter(tmp_path, self.OFF)
        self._own_the_thread(a, owner=SELF_ID)
        follow_up = _message(
            author=_user(HUMAN), content="and then?", message_id=3
        )
        assert _admit(a, follow_up) is True

# ── the gate-env snapshot, on cores that have one ───────────────────────────

_compat = adapter_mod.compat.probe()

pytestmark_snapshot = pytest.mark.skipif(
    not _compat.has_gate_env_snapshot,
    reason=(
        "this Hermes build has no _snapshot_gate_env/_scoped_gate_env; the "
        "flag is read live instead (see TestLegacyGateFallback)"
    ),
)


@pytestmark_snapshot
class TestGateSnapshot:
    """Multiplex safety on a core that snapshots gate env per adapter."""

    def test_the_flag_is_captured_into_the_real_gate_snapshot(self, tmp_path):
        """The built-in _GATE_ENV_KEYS cannot know a plugin's key.

        Without the _snapshot_gate_env override the flag would never enter the
        snapshot, so the gate read would always take the live "pre-connect
        fallback" instead of the path the core documents as authoritative
        under multiplex.
        """
        a = _make_adapter(tmp_path)
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            a._snapshot_gate_env()
        assert "DISCORD_THREAD_OWNERSHIP_TRANSFER" in a._gate_env_snapshot

    def test_config_is_read_through_the_scoped_gate(self, tmp_path):
        """The per-adapter snapshot must win over a later os.environ write.

        Uses the production snapshot call, not a hand-injected dict: the
        snapshot is taken inside the owning profile's scope at connect(), then
        another profile mutates the shared process env.
        """
        a = _make_adapter(tmp_path)
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            a._snapshot_gate_env()          # our profile's connect()
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "false"}):
            assert a._ownership_config().enabled is True

    def test_a_profile_that_never_opted_in_stays_off(self, tmp_path):
        """One profile's env write must not enable another's."""
        opted_out = _make_adapter(tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_THREAD_OWNERSHIP_TRANSFER", None)
            opted_out._snapshot_gate_env()  # connect() with nothing configured
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "true"}):
            assert opted_out._ownership_config().enabled is False

    def test_the_builtin_gate_keys_are_still_snapshotted(self, tmp_path):
        """The override must extend the built-in snapshot, not replace it."""
        a = _make_adapter(tmp_path)
        with patch.dict(os.environ, {"DISCORD_ALLOW_BOTS": "mentions"}):
            a._snapshot_gate_env()
        assert a._gate_env_snapshot["DISCORD_ALLOW_BOTS"] == "mentions"
        assert "DISCORD_ALLOWED_USERS" in a._gate_env_snapshot


# ── the same guarantees on a core without those helpers ─────────────────────


class TestLegacyGateFallback:
    """A build without the gate helpers keeps the feature, on a live read.

    The snapshot cannot be extended where it does not exist, so the flag is
    read from the environment — exactly what every other Discord gate does on
    such a build.  What must not change is the *precedence*, because that is
    what makes the env kill switch work.
    """

    @pytest.fixture(autouse=True)
    def legacy_gate_helpers(self, monkeypatch):
        from plugins.platforms.discord.adapter import DiscordAdapter

        original_init = DiscordAdapter.__init__

        def legacy_init(adapter, *args, **kwargs):
            original_init(adapter, *args, **kwargs)
            adapter.__dict__.pop("_gate_env_snapshot", None)

        monkeypatch.setattr(DiscordAdapter, "__init__", legacy_init)
        for name in ("_snapshot_gate_env", "_gate_env", "_gate_raw", "_get_allow_bots"):
            monkeypatch.setattr(DiscordAdapter, name, None, raising=False)

    def test_calling_the_snapshot_override_is_inert(self, tmp_path):
        a = _make_adapter(tmp_path)
        a._snapshot_gate_env()              # must not raise
        assert not hasattr(a, "_gate_env_snapshot")

    def test_the_env_kill_switch_still_wins_over_yaml(self, tmp_path):
        a = _make_adapter(tmp_path, {"thread_ownership_transfer": True})
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "false"}):
            assert a._ownership_config().enabled is False

    def test_per_adapter_yaml_still_opts_in(self, tmp_path):
        a = _make_adapter(tmp_path, {"thread_ownership_transfer": True})
        b = _make_adapter(tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_THREAD_OWNERSHIP_TRANSFER", None)
            assert a._ownership_config().enabled is True
            assert b._ownership_config().enabled is False

    def test_the_bot_gate_is_widened_through_the_environment(self, tmp_path):
        """The legacy admission gate reads DISCORD_ALLOW_BOTS inline."""
        a = _make_adapter(tmp_path)
        assert a._compat.has_allow_bots_hook is False
        with patch.dict(os.environ, {"DISCORD_ALLOW_BOTS": "none"}):
            with adapter_mod.compat.forced_allow_bots(True):
                assert os.environ["DISCORD_ALLOW_BOTS"] == "mentions"
            assert os.environ["DISCORD_ALLOW_BOTS"] == "none"
