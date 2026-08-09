"""``DiscordCustomAdapter`` — the built-in Discord adapter plus thread ownership.

Only two behaviours are overridden:

* :meth:`DiscordCustomAdapter._discord_message_admission` — observes every
  trusted message in a guild thread to keep ownership current, then narrows
  (or, for a validated handoff, widens) the built-in admission verdict.
* :meth:`DiscordCustomAdapter._get_allow_bots` — lets exactly one class of
  bot-authored message through: an owner-initiated handoff addressed to us.

Everything else is inherited.  When the feature is off, both overrides are a
straight delegation to the built-in adapter and nothing is read or written.

``discord`` is imported through :mod:`compat`, never at module scope: it is a
lazily-installed Hermes extra, so on a fresh host it can legitimately be absent
at the moment this module loads.  Without the guard the *plugin* would be the
thing that fails there, and the feature would silently vanish for a run.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from plugins.platforms.discord.adapter import DiscordAdapter

from . import compat
from .ownership import MessageFacts, OwnershipConfig, ThreadOwnershipStore

logger = logging.getLogger(__name__)

#: Env bridge for ``discord.thread_ownership_transfer`` (see config_bridge.py).
ENV_FLAG = "DISCORD_THREAD_OWNERSHIP_TRANSFER"
CONFIG_KEY = "thread_ownership_transfer"

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _ownership_message_types(discord_mod: Any) -> set:
    """Message kinds that can carry ownership intent.  Matches the built-in gate."""
    return {discord_mod.MessageType.default, discord_mod.MessageType.reply}


class DiscordCustomAdapter(DiscordAdapter):
    """Discord adapter with single-owner thread admission."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ownership_store_cache: ThreadOwnershipStore | None = None
        self._ownership_force_admit = False
        # Probed per adapter, not per message: the answer cannot change inside
        # a process, and the construction site is where a surprising build
        # should be visible in the log.
        self._compat = compat.probe()
        compat.ensure_discord_installed()

    # -- configuration ------------------------------------------------------

    def _snapshot_gate_env(self) -> None:
        """Add this plugin's flag to the per-adapter gate snapshot.

        The built-in snapshot is built from a fixed ``_GATE_ENV_KEYS`` tuple
        that cannot know about a plugin's key, so without this the flag would
        never be *in* the snapshot and the read would always fall through to
        the live process-global one — the core's documented "pre-connect
        fallback", not the path that is authoritative under multiplex.
        Extending the snapshot here puts the flag on the same footing as every
        other Discord gate: captured inside the owning profile's runtime scope
        at ``connect()``.

        On a Hermes build without the snapshot mechanism this is inert; the
        flag is then read live, exactly like every other Discord gate on that
        build.
        """
        parent = getattr(super(), "_snapshot_gate_env", None)
        if parent is None:
            return
        parent()
        snapshot = getattr(self, "_gate_env_snapshot", None)
        if isinstance(snapshot, dict):
            snapshot[ENV_FLAG] = compat.scoped_gate_env(ENV_FLAG)

    def _ownership_config(self) -> OwnershipConfig:
        """Resolve ``discord.thread_ownership_transfer``.

        Goes through the adapter's own gate reader so the read honours
        ``_gate_env_snapshot`` / the profile's secret scope under
        ``gateway.multiplex_profiles`` — a bare ``os.getenv`` would read
        another profile's process-global value.

        Precedence follows every other Discord gate: **env first, then
        ``config.extra``**.  That is what makes
        ``DISCORD_THREAD_OWNERSHIP_TRANSFER=false`` usable as a kill switch
        over a ``config.yaml`` that says true.
        """
        return OwnershipConfig.from_raw(compat.gate_raw(self, CONFIG_KEY, ENV_FLAG))

    @property
    def _ownership_store(self) -> ThreadOwnershipStore:
        if self._ownership_store_cache is None:
            self._ownership_store_cache = ThreadOwnershipStore()
        return self._ownership_store_cache

    # -- fact extraction ----------------------------------------------------

    def _ownership_facts(self, message: Any) -> MessageFacts | None:
        """Build engine facts, or ``None`` when ownership does not apply.

        Ownership is scoped to guild threads: DMs and plain channels keep the
        stock behaviour exactly.
        """
        discord = compat.discord_module()
        if discord is None:
            return None
        channel = getattr(message, "channel", None)
        if not isinstance(channel, discord.Thread):
            return None
        client_user = getattr(self._client, "user", None) if self._client else None
        if client_user is None:
            return None

        content = getattr(message, "content", "") or ""
        self_id = str(client_user.id)

        # Only inline ``<@id>`` tokens count.  Discord's reply-ping adds the
        # replied-to user to ``message.mentions`` without the author ever
        # typing a handle; treating that as an address would let a plain
        # "thanks!" reply silently reassign the thread.  Ordering follows the
        # raw content so every bot picks the same winner.
        bots: list[str] = []
        unresolved = False
        seen: set[str] = set()
        for match in _MENTION_RE.finditer(content):
            uid = match.group(1)
            if uid in seen:
                continue
            seen.add(uid)
            is_bot = self._resolve_mention_is_bot(message, uid)
            if is_bot is None:
                # Cannot tell bot from human.  Guessing would let two bots
                # reach different winners for the same message, which is the
                # double-answer bug this feature exists to remove.
                unresolved = True
            elif is_bot:
                bots.append(uid)

        return MessageFacts(
            thread_id=str(channel.id),
            message_id=str(getattr(message, "id", "")),
            author_id=str(getattr(message.author, "id", "")),
            author_is_bot=bool(getattr(message.author, "bot", False)),
            self_id=self_id,
            mentioned_bot_ids=tuple(bots),
            content=content,
            has_unresolved_mentions=unresolved,
        )

    def _resolve_mention_is_bot(self, message: Any, uid: str) -> bool | None:
        """Is inline mention *uid* a bot?  ``None`` when it cannot be resolved.

        ``message.mentions`` is not always populated — the core adapter notes
        that "mobile, edited, or relayed messages can carry the mention in the
        content while leaving the resolved mentions list empty".  Fall back to
        the guild member cache and then the client user cache so all bots in
        the thread reach the same answer from the same raw payload.
        """
        for mentioned in getattr(message, "mentions", None) or []:
            if str(getattr(mentioned, "id", "")) == uid:
                return bool(getattr(mentioned, "bot", False))

        try:
            numeric = int(uid)
        except (TypeError, ValueError):
            return None

        guild = getattr(message, "guild", None)
        getter = getattr(guild, "get_member", None) if guild is not None else None
        if callable(getter):
            try:
                member = getter(numeric)
            except Exception:
                member = None
            if member is not None:
                return bool(getattr(member, "bot", False))

        getter = getattr(self._client, "get_user", None) if self._client else None
        if callable(getter):
            try:
                user = getter(numeric)
            except Exception:
                user = None
            if user is not None:
                return bool(getattr(user, "bot", False))

        client_user = getattr(self._client, "user", None) if self._client else None
        if client_user is not None and str(client_user.id) == uid:
            return True
        return None

    def _ownership_author_trusted(self, message: Any) -> bool:
        """Whether this message may influence ownership at all.

        Ownership must be observed even for messages the built-in gate will
        drop — a transfer away from us is exactly such a message, and we have
        to record it or we would keep answering the thread.  That means the
        authorization check cannot be inherited from the gate's return value,
        so it is made here against the same helper the gate uses.
        """
        discord = compat.discord_module()
        if discord is None:
            return False
        if getattr(message, "type", None) not in _ownership_message_types(discord):
            return False

        author = getattr(message, "author", None)
        if author is None:
            return False

        # Channel gates live in _handle_message, i.e. after admission, so they
        # would otherwise let a busy ignored channel fill the thread LRU and
        # evict threads where ownership actually matters.  No bot answers in
        # these channels, so there is nothing to arbitrate.
        if not self._ownership_channel_in_scope(message):
            return False

        client_user = getattr(self._client, "user", None) if self._client else None
        if client_user is not None and author == client_user:
            # Our own message: needed to observe an outbound handoff.
            return True
        if getattr(author, "bot", False):
            # Bot authors are constrained by the engine (only the current owner
            # can move ownership), so no user allowlist applies.
            return True

        channel = getattr(message, "channel", None)
        channel_ids = {str(getattr(channel, "id", ""))}
        parent_id = self._get_parent_channel_id(channel)
        if parent_id:
            channel_ids.add(parent_id)
        try:
            return bool(
                self._is_allowed_user(
                    str(author.id),
                    author,
                    guild=getattr(message, "guild", None),
                    is_dm=False,
                    channel_ids=channel_ids,
                )
            )
        except Exception:
            logger.debug("Ownership authorization check failed", exc_info=True)
            return False

    def _ownership_channel_in_scope(self, message: Any) -> bool:
        """Whether this bot could ever answer in the message's channel.

        Mirrors the allow/ignore gates the built-in applies in
        ``_handle_message``.  Kept here so ownership state is only recorded for
        threads that matter.
        """
        channel = getattr(message, "channel", None)
        parent_id = self._get_parent_channel_id(channel)
        keys = self._discord_channel_keys(message, parent_id)

        allowed = compat.allowed_channels(self)
        if allowed and "*" not in allowed and not (keys & allowed):
            return False

        ignored = compat.ignored_channels(self)
        if "*" in ignored or (keys & ignored):
            return False
        return True

    def _ownership_is_replay(self, message: Any) -> bool:
        """Read-only peek at the built-in dedup cache.

        The built-in dedup gate runs inside ``super()``, i.e. *after* our
        decision point, so a redelivered message would otherwise be folded into
        ownership before it is recognised as a duplicate.
        ``_dispatch_recovered_message`` relies on exactly that gate (it calls
        admission with ``claim=False``), so the cache is consulted here without
        being mutated.

        On a build without ``_dedup`` the store's own applied-id set and
        snowflake ordering still cover replays; this peek only closes the
        window inside the dedup TTL.
        """
        dedup = getattr(self, "_dedup", None)
        contains = getattr(dedup, "contains", None)
        if not callable(contains):
            return False
        try:
            return bool(contains(str(getattr(message, "id", ""))))
        except Exception:
            logger.debug("Dedup peek failed; treating as a fresh message", exc_info=True)
            return False

    # -- overrides ----------------------------------------------------------

    def _get_allow_bots(self) -> str:
        """Widen the bot gate for the single message that is a handoff to us.

        The flag is set only for the duration of one synchronous call into the
        built-in admission gate, so it cannot leak into another message.

        Only reached on Hermes builds that route the bot gate through this
        hook.  Older builds read ``DISCORD_ALLOW_BOTS`` inline, and are handled
        by :func:`compat.forced_allow_bots` instead.
        """
        if self._ownership_force_admit:
            return "mentions"
        parent = getattr(super(), "_get_allow_bots", None)
        if parent is not None:
            return parent()
        return compat.allow_bots_from_env()

    def _discord_message_admission(
        self, message: Any, *, claim: bool
    ) -> tuple[bool, bool]:
        config = self._ownership_config()
        if not config.enabled:
            return super()._discord_message_admission(message, claim=claim)

        facts = self._ownership_facts(message)
        if facts is None or not self._ownership_author_trusted(message):
            return super()._discord_message_admission(message, claim=claim)

        if self._ownership_is_replay(message):
            decision = self._ownership_store.replayed(facts, config)
        else:
            decision = self._ownership_store.apply(facts, config, now=time.time())
        if decision.verdict in {"claim", "transfer", "handoff"}:
            logger.info(
                "[%s] thread %s ownership %s -> %s (%s)",
                self.name,
                facts.thread_id,
                decision.verdict,
                decision.owner_id,
                decision.reason,
            )

        force = decision.admit_self is True
        self._ownership_force_admit = force
        # On a core without ``_get_allow_bots`` our override is never consulted,
        # so the gate has to be widened through the environment instead.
        widen = compat.forced_allow_bots(
            force and not self._compat.has_allow_bots_hook
        )
        try:
            with widen:
                admitted, role_authorized = super()._discord_message_admission(
                    message, claim=claim
                )
        finally:
            self._ownership_force_admit = False

        if not admitted:
            return False, role_authorized
        if decision.admit_self is False:
            return False, False
        return True, role_authorized

    async def disconnect(self) -> None:
        """Drain queued ownership writes before the built-in teardown.

        State writes are dispatched to a background thread so they never stall
        the Discord event loop; a restart must not race them.  The flush runs
        in a worker thread so shutdown does not block the loop either, and a
        failure here must never prevent the adapter from disconnecting.
        """
        store = self._ownership_store_cache
        if store is not None:
            try:
                await asyncio.to_thread(store.close, 5.0)
            except Exception:
                logger.warning(
                    "[%s] Failed to flush thread ownership state on disconnect",
                    self.name,
                    exc_info=True,
                )
        await super().disconnect()


def build_adapter(config):
    """Factory used by the platform registry entry."""
    return DiscordCustomAdapter(config)
