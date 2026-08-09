"""Deterministic thread-ownership engine for Discord.

Pure logic plus a small persistent store.  Deliberately free of any
``discord`` import so it can be reasoned about, and tested, on its own.

The model
--------
Exactly one bot *owns* a Discord thread at a time.  Ownership is derived by
every bot independently from the same event stream, using rules that are a
pure function of the message — so three separate profile gateways converge on
the same owner without sharing any state.  The on-disk store is a per-profile
cache of that derivation, which is what makes ownership survive ``/new`` and a
gateway restart.

Rules
-----
* A **human** who @mentions a bot in a thread makes that bot the owner.  If
  several bots are mentioned, the first mention in the message wins — a total
  order every bot computes identically.
* A human message that mentions no bot never changes ownership; only the
  current owner answers it.
* Mentioning a **human** never transfers ownership.
* A **bot** message can never claim a thread for itself, and a non-owner bot
  can never move ownership at all.
* The one exception is an **owner-initiated handoff**: the current owner may
  pass the thread to exactly one other bot, so agents can hand work to each
  other.  This is bounded by the guards below.

Handoff guards (bot-initiated only — a human can always re-assign)
-----------------------------------------------------------------
* ``bot_handoff="marker"`` (default) requires an explicit marker token in the
  message, which is what separates an intentional handoff from an owner merely
  naming another bot in conversation.  ``"mention"`` accepts any owner mention
  of a single other bot; ``"off"`` disables bot-initiated handoff.
* A bot cannot hand off to itself.
* Ambiguity is refused: a handoff naming two or more bots is a no-op, unlike
  the human path.  A human is present to correct a bad guess; an autonomous
  cascade is not worth the risk.
* Bounce guard: handing straight back to the bot that just handed to you is
  refused inside ``handoff_cooldown_seconds``.
* Hop budget: at most ``max_handoffs`` bot-initiated handoffs per thread per
  ``handoff_window_seconds``.  Exhausting it freezes bot handoff for that
  thread until the window rolls over — a human mention resets it immediately.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import queue
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: File under ``$HERMES_HOME`` holding this profile's ownership cache.
STATE_FILENAME = "discord_thread_ownership.json"

_TRUTHY = {"true", "1", "yes", "on"}
_FALSEY = {"false", "0", "no", "off"}
_HANDOFF_MODES = {"off", "marker", "mention"}

#: How many state-changing message ids to remember per thread for replay
#: detection.  Only ownership *changes* are recorded — every other verdict is
#: already idempotent — so this stays small even on busy threads.
_APPLIED_ID_MEMORY = 32

#: ``handoff_window_seconds: 0`` would make the "recent handoffs" filter match
#: nothing, silently disabling the hop budget instead of tightening it.  Clamp
#: to a small positive floor so both budget knobs read the same direction:
#: smaller is stricter.
_MIN_HANDOFF_WINDOW = 1.0


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return default


def _as_tristate(value: Any) -> bool | None:
    """``True``/``False`` for a recognised flag value, ``None`` for "unset".

    Distinct from :func:`_as_bool`, which folds "unset" and "unparseable" into
    a caller-supplied default.  Composing the env bridge with the YAML mapping
    needs the third state: only an explicit yes/no may move ``enabled``, and
    anything else has to leave the config file's own answer standing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return None


def _as_number(value: Any, default: float, *, cast=float) -> Any:
    try:
        parsed = cast(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, cast(0))


@dataclass(frozen=True)
class OwnershipConfig:
    """Resolved ``discord.thread_ownership_transfer`` settings."""

    enabled: bool = False
    bot_handoff: str = "marker"
    handoff_marker: str = "[[handoff]]"
    max_handoffs: int = 8
    handoff_window_seconds: float = 3600.0
    handoff_cooldown_seconds: float = 30.0

    @classmethod
    def from_raw(cls, raw: Any) -> OwnershipConfig:
        """Build from the config value, which may be a bool or a mapping.

        ``thread_ownership_transfer: true`` turns the feature on with defaults.
        A mapping is itself the opt-in signal, so ``enabled`` defaults to True
        inside one — but an explicit ``enabled: false`` still wins.
        """
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls(enabled=_as_bool(raw, False))

        defaults = cls()
        mode = str(raw.get("bot_handoff", defaults.bot_handoff)).strip().lower()
        if mode not in _HANDOFF_MODES:
            mode = defaults.bot_handoff

        marker = str(raw.get("handoff_marker", defaults.handoff_marker)).strip()
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            bot_handoff=mode,
            handoff_marker=marker or defaults.handoff_marker,
            max_handoffs=int(
                _as_number(raw.get("max_handoffs"), defaults.max_handoffs, cast=int)
            ),
            handoff_window_seconds=max(
                _MIN_HANDOFF_WINDOW,
                _as_number(
                    raw.get("handoff_window_seconds"), defaults.handoff_window_seconds
                ),
            ),
            handoff_cooldown_seconds=_as_number(
                raw.get("handoff_cooldown_seconds"), defaults.handoff_cooldown_seconds
            ),
        )


    @classmethod
    def resolve(cls, env_raw: Any, extra_raw: Any) -> OwnershipConfig:
        """Compose the env bridge with the ``config.yaml`` value.

        The env var is an operator **switch**, not an alternative config
        source: ``config.yaml`` is where the guards live.  So a recognised env
        value moves ``enabled`` and nothing else.

        * ``false`` — kill switch.  Wins over any YAML, which is the documented
          direction and the reason the variable exists.
        * ``true``  — turns the feature on while leaving ``bot_handoff``,
          ``handoff_marker`` and the hop budget exactly as the mapping set
          them.  Reading it as a whole config used to reset ``bot_handoff:
          off`` to the ``marker`` default, silently re-opening bot-authored
          force-admission on a deployment that had deliberately closed it.
        * anything else (unset, empty, unparseable) — no opinion; the YAML
          value stands on its own.
        """
        switch = _as_tristate(env_raw)
        base = cls.from_raw(extra_raw)
        if switch is None:
            return base
        return replace(base, enabled=switch)


@dataclass(frozen=True)
class MessageFacts:
    """Everything the engine needs about one Discord message.

    ``mentioned_bot_ids`` holds only confirmed bot accounts, ordered by first
    appearance in the raw content, so the multi-mention winner is deterministic
    across processes.  Mentioned humans are simply absent from it — which is
    what makes "mentioning a human never transfers" fall out of the model
    rather than needing a rule.
    """

    thread_id: str
    message_id: str
    author_id: str
    author_is_bot: bool
    self_id: str
    mentioned_bot_ids: tuple = ()
    content: str = ""
    #: True when the message carries an inline ``<@id>`` the adapter could not
    #: resolve to a user.  An unresolvable id might be a bot, so no bot can
    #: safely decide the winner — every bot refuses the change instead, which
    #: is a verdict they all reach identically.
    has_unresolved_mentions: bool = False


@dataclass(frozen=True)
class Decision:
    """Outcome of applying one message to a thread's ownership state.

    ``admit_self`` is this adapter's ownership opinion for the message:

    * ``None``  — no opinion; the built-in Discord admission rules decide.
    * ``False`` — veto; this bot is not the owner and must stay silent.
    * ``True``  — force-admit; a handoff addressed to this bot, which must get
      through even though the author is a bot.
    """

    owner_id: str | None
    verdict: str
    reason: str = ""
    admit_self: bool | None = None


@dataclass
class ThreadState:
    owner_id: str | None = None
    previous_owner_id: str | None = None
    last_handoff_at: float = 0.0
    handoffs: list = field(default_factory=list)
    #: Ids of the messages that actually *changed* ownership, oldest first,
    #: bounded to ``_APPLIED_ID_MEMORY``.  Every other verdict is idempotent,
    #: so only these need replay protection.
    applied_message_ids: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "previous_owner_id": self.previous_owner_id,
            "last_handoff_at": self.last_handoff_at,
            "handoffs": list(self.handoffs),
            "applied_message_ids": list(self.applied_message_ids),
        }

    @classmethod
    def from_json(cls, data: Any) -> ThreadState:
        if not isinstance(data, dict):
            return cls()
        handoffs = data.get("handoffs")
        applied = data.get("applied_message_ids")
        if not isinstance(applied, list):
            # Back-compat with the single-id format this replaced.
            legacy = _opt_str(data.get("last_event_message_id"))
            applied = [legacy] if legacy else []
        return cls(
            owner_id=_opt_str(data.get("owner_id")),
            previous_owner_id=_opt_str(data.get("previous_owner_id")),
            last_handoff_at=_as_number(data.get("last_handoff_at"), 0.0),
            handoffs=[
                float(t) for t in handoffs if isinstance(t, (int, float))
            ] if isinstance(handoffs, list) else [],
            applied_message_ids=[str(m) for m in applied if m not in (None, "")],
        )

    def record_applied(self, message_id: str) -> list:
        """Return the applied-id list with *message_id* appended and bounded."""
        ids = [m for m in self.applied_message_ids if m != message_id]
        ids.append(message_id)
        return ids[-_APPLIED_ID_MEMORY:]

    def newest_applied_id(self) -> str | None:
        return self.applied_message_ids[-1] if self.applied_message_ids else None


def _opt_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _is_older_snowflake(candidate: str | None, reference: str | None) -> bool:
    """True when *candidate* is a Discord snowflake older than *reference*.

    Discord ids are time-ordered, which lets us reject an ownership event that
    predates the one already applied — the case a replay set alone cannot
    catch, because a backfill can surface an old message we have never seen.
    Ids that are not numeric (tests, synthetic events) skip the check.
    """
    if not candidate or not reference:
        return False
    try:
        return int(candidate) < int(reference)
    except (TypeError, ValueError):
        return False


# ── the engine ──────────────────────────────────────────────────────────────


def evaluate(
    state: ThreadState,
    facts: MessageFacts,
    config: OwnershipConfig,
    now: float,
) -> tuple[Decision, ThreadState]:
    """Pure transition: return the decision and the resulting thread state."""
    if not config.enabled:
        return Decision(owner_id=state.owner_id, verdict="disabled"), state

    if facts.author_is_bot:
        decision_owner, verdict, reason, next_state = _bot_event(
            state, facts, config, now
        )
    else:
        decision_owner, verdict, reason, next_state = _human_event(state, facts)

    admit = _admission(
        effective_owner=decision_owner,
        facts=facts,
        verdict=verdict,
    )
    return (
        Decision(
            owner_id=decision_owner,
            verdict=verdict,
            reason=reason,
            admit_self=admit,
        ),
        next_state,
    )


def _human_event(
    state: ThreadState, facts: MessageFacts
) -> tuple[str | None, str, str, ThreadState]:
    """A human message: mentioning a bot claims or transfers the thread."""
    if facts.has_unresolved_mentions:
        return state.owner_id, "none", "unresolved_mention", state
    if not facts.mentioned_bot_ids:
        return state.owner_id, "none", "no_bot_mentioned", state

    # Deterministic across every bot observing this same message.
    winner = facts.mentioned_bot_ids[0]
    if winner == state.owner_id:
        return winner, "reaffirm", "already_owner", state

    verdict = "transfer" if state.owner_id else "claim"
    # A human re-assignment is the escape hatch from a frozen thread, so it
    # clears the bot-handoff budget as well as moving the owner.
    new_state = replace(
        state,
        owner_id=winner,
        previous_owner_id=state.owner_id,
        handoffs=[],
        last_handoff_at=0.0,
        applied_message_ids=state.record_applied(facts.message_id),
    )
    return winner, verdict, "human_mention", new_state


def _bot_event(
    state: ThreadState, facts: MessageFacts, config: OwnershipConfig, now: float
) -> tuple[str | None, str, str, ThreadState]:
    """A bot message: only the current owner can move ownership, once."""
    owner = state.owner_id
    if config.bot_handoff == "off":
        return owner, "none", "bot_handoff_disabled", state
    if facts.has_unresolved_mentions:
        return owner, "none", "unresolved_mention", state
    if owner is None or facts.author_id != owner:
        # Covers both "bots cannot claim" and "non-owner bots cannot steal".
        return owner, "none", "author_is_not_owner", state

    if config.bot_handoff == "marker":
        marker = config.handoff_marker.lower()
        if marker not in (facts.content or "").lower():
            return owner, "none", "no_handoff_marker", state

    targets = [b for b in facts.mentioned_bot_ids if b != facts.author_id]
    if len(targets) != 1:
        reason = "no_handoff_target" if not targets else "ambiguous_handoff"
        return owner, "none", reason, state
    target = targets[0]

    if (
        state.previous_owner_id == target
        and (now - state.last_handoff_at) < config.handoff_cooldown_seconds
    ):
        return owner, "none", "bounce_blocked", state

    recent = [t for t in state.handoffs if (now - t) < config.handoff_window_seconds]
    if len(recent) >= config.max_handoffs:
        return owner, "none", "budget_exhausted", state

    new_state = replace(
        state,
        owner_id=target,
        previous_owner_id=owner,
        last_handoff_at=now,
        handoffs=recent + [now],
        applied_message_ids=state.record_applied(facts.message_id),
    )
    return target, "handoff", "owner_handoff", new_state


def _admission(
    *, effective_owner: str | None, facts: MessageFacts, verdict: str
) -> bool | None:
    if effective_owner is None:
        # Nothing owns this thread yet — the built-in rules are unchanged.
        return None
    if effective_owner != facts.self_id:
        return False
    if verdict == "handoff":
        # We are the handoff target.  The author is a bot, so the built-in
        # allow_bots gate would drop this; force it through.
        return True
    return None


# ── persistence ─────────────────────────────────────────────────────────────


def _event_loop_is_running() -> bool:
    """True when called from inside a running asyncio loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


#: Queued snapshots before ``submit`` applies backpressure by writing inline.
#: Ownership changes are rare (claim / transfer / handoff, not every message),
#: so reaching this depth means the disk is pathologically slow — at which
#: point blocking the caller is better than growing an unbounded queue.
_WRITE_QUEUE_DEPTH = 64

_STOP = object()


class _StateWriter:
    """Serialises state writes onto one background thread.

    One thread per store, so writes land in submission order.  Payloads are
    plain dicts snapshotted by the caller, so the thread shares no mutable
    state with the event loop.  Every failure mode degrades to a synchronous
    write rather than to data loss: the thread refusing to start, the queue
    filling up, and ``close()`` having been called all make ``submit`` return
    False, which sends the caller down the inline path.
    """

    def __init__(self, write_fn: Callable[[Path, dict], None]):
        self._write = write_fn
        self._queue: queue.Queue = queue.Queue(maxsize=_WRITE_QUEUE_DEPTH)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, path: Path, snapshot: dict) -> bool:
        """Queue a write.  False means "caller must write it synchronously".

        *path* is resolved by the caller, never by this thread: under
        ``gateway.multiplex_profiles`` the profile's home lives in a
        ContextVar, and a plain ``threading.Thread`` starts with an empty
        context — so resolving it here would silently target the process-global
        home instead of the owning profile's.
        """
        with self._lock:
            if self._closed:
                return False
            if self._thread is None or not self._thread.is_alive():
                try:
                    self._thread = threading.Thread(
                        target=self._run,
                        name="discord-ownership-writer",
                        daemon=True,
                    )
                    self._thread.start()
                except RuntimeError:
                    # Interpreter shutdown, or thread limit reached.
                    logger.debug("Ownership writer thread unavailable", exc_info=True)
                    self._thread = None
                    return False
        try:
            self._queue.put_nowait((path, snapshot))
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                self._write(*item)
            except Exception:
                logger.warning("Ownership state write failed", exc_info=True)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        # Queue.join() has no timeout, so poll unfinished work instead of
        # risking a hang on shutdown.
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        drained = self.flush(timeout)
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        thread.join(timeout=timeout)
        return drained and not thread.is_alive()


#: Live stores, so a clean interpreter exit drains queued writes.  Weak refs
#: keep this from pinning a store (and its writer thread) alive.
_LIVE_STORES: weakref.WeakSet = weakref.WeakSet()
_EXIT_HOOK_INSTALLED = False


def _flush_all_stores() -> None:
    for store in list(_LIVE_STORES):
        try:
            store.close(timeout=2.0)
        except Exception:
            pass


def _register_for_exit_flush(store: ThreadOwnershipStore) -> None:
    global _EXIT_HOOK_INSTALLED
    _LIVE_STORES.add(store)
    if not _EXIT_HOOK_INSTALLED:
        try:
            atexit.register(_flush_all_stores)
            _EXIT_HOOK_INSTALLED = True
        except Exception:
            pass


class ThreadOwnershipStore:
    """Per-profile, disk-backed cache of thread ownership.

    Mirrors ``gateway.platforms.helpers.ThreadParticipationTracker``: state
    lives under ``get_hermes_home()`` so every profile is isolated, and is
    written atomically on change.  Nothing is written while the feature is
    disabled.
    """

    def __init__(self, max_threads: int = 500):
        self._max_threads = max(1, int(max_threads))
        self._threads: dict[str, ThreadState] = self._load()
        self._writer = _StateWriter(self._write_snapshot)
        _register_for_exit_flush(self)

    # -- paths / io ---------------------------------------------------------

    def _state_path(self) -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / STATE_FILENAME

    def _load(self, path: Path | None = None) -> dict:
        if path is None:
            path = self._state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            threads = data.get("threads") if isinstance(data, dict) else None
            if not isinstance(threads, dict):
                return {}
            return {
                str(tid): ThreadState.from_json(raw) for tid, raw in threads.items()
            }
        except Exception:
            # A corrupt cache must never take the gateway down; ownership
            # simply re-derives from the next messages in each thread.
            logger.warning("Discarding unreadable %s", path, exc_info=True)
            return {}

    def _save(self, changed_thread_id: str | None = None) -> None:
        """Persist state, off the event loop when there is one.

        The durable write is ``atomic_json_write`` — temp file, ``fsync``,
        ``os.replace``.  On the gateway this runs inside ``on_message``, so
        doing it inline would stall the Discord event loop for the length of an
        fsync.  When a loop is running the payload is handed to a single
        background writer thread instead; ordering is preserved because there
        is exactly one such thread per store.  With no loop (CLI, cron, tests)
        the write stays inline and synchronous, so callers keep the simple
        "returned means persisted" contract.

        Only the *serialisation* happens here.  ``self._threads`` is never
        touched by the writer thread, so the event loop remains the sole
        mutator of in-memory state.

        The target path is resolved *here*, on the calling thread, and handed
        to the writer.  Under ``gateway.multiplex_profiles`` the profile's home
        is a ContextVar (``set_hermes_home_override`` deliberately does not
        touch ``os.environ``), and a new thread starts with an empty context —
        so a path resolved on the writer thread would point at the
        process-global home and a secondary profile would write into the
        primary's state file.
        """
        if changed_thread_id is not None and changed_thread_id in self._threads:
            # Keep the freshly-changed thread newest in LRU order.
            self._threads[changed_thread_id] = self._threads.pop(changed_thread_id)
        if len(self._threads) > self._max_threads:
            keep = list(self._threads)[-self._max_threads:]
            self._threads = {tid: self._threads[tid] for tid in keep}

        # Snapshot to plain data on this thread: the writer must never read a
        # dict the event loop can mutate underneath it.
        snapshot = {tid: st.to_json() for tid, st in self._threads.items()}
        path = self._state_path()

        if _event_loop_is_running() and self._writer.submit(path, snapshot):
            return
        self._write_snapshot(path, snapshot)

    def _write_snapshot(self, path: Path, snapshot: dict) -> None:
        """Merge with whatever is at *path* and write it atomically.

        Runs on the writer thread when async, inline otherwise.  *path* comes
        from the caller because this thread cannot resolve the profile home
        (see :meth:`_save`); it is used for **both** the merge read and the
        write so the two can never target different profiles.

        The merge exists because this class holds a long-lived in-memory
        snapshot: a plain full-file rewrite would drop threads another writer
        on the same profile recorded after we loaded.  Our own threads stay
        authoritative — they are the ones we just decided on.
        """
        from utils import atomic_json_write

        try:
            on_disk = self._load(path)
        except Exception:
            on_disk = {}

        merged: dict[str, dict] = {
            tid: st.to_json() for tid, st in on_disk.items() if tid not in snapshot
        }
        merged.update(snapshot)
        if len(merged) > self._max_threads:
            keep = list(merged)[-self._max_threads:]
            merged = {tid: merged[tid] for tid in keep}

        try:
            atomic_json_write(path, {"version": 1, "threads": merged}, indent=None)
        except Exception:
            logger.warning("Failed to persist thread ownership", exc_info=True)

    # -- write lifecycle ----------------------------------------------------

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until queued writes have landed.  True if the queue drained.

        Called on adapter shutdown and by tests.  A synchronous store has
        nothing to wait for and returns True immediately.
        """
        return self._writer.flush(timeout)

    def close(self, timeout: float = 5.0) -> bool:
        """Flush and stop the writer thread."""
        return self._writer.close(timeout)

    # -- api ----------------------------------------------------------------

    def owner_of(self, thread_id: str) -> str | None:
        state = self._threads.get(str(thread_id))
        return state.owner_id if state else None

    def replayed(self, facts: MessageFacts, config: OwnershipConfig) -> Decision:
        """Decide from settled state without folding *facts* in.

        Used when the caller already knows the message is a redelivery (the
        adapter's dedup cache says so).  Ownership must not move, but the
        current owner still decides who may answer.
        """
        if not config.enabled:
            return Decision(owner_id=None, verdict="disabled")
        owner = self.owner_of(facts.thread_id)
        return Decision(
            owner_id=owner,
            verdict="replay",
            reason="duplicate_delivery",
            admit_self=_admission(
                effective_owner=owner, facts=facts, verdict="replay"
            ),
        )

    def apply(
        self, facts: MessageFacts, config: OwnershipConfig, now: float
    ) -> Decision:
        """Fold one message into this thread's state and return the decision."""
        if not config.enabled:
            return Decision(owner_id=None, verdict="disabled")

        thread_id = str(facts.thread_id)
        state = self._threads.get(thread_id, ThreadState())

        # Two independent replay guards, because Discord can resurface a
        # message in two different ways:
        #
        #  * exact redelivery (gateway RESUME) — caught by the applied-id set;
        #  * an *older* message we have never seen, surfaced by missed-message
        #    backfill — caught by snowflake ordering, since the applied-id set
        #    has nothing to match on.
        #
        # Without both, an old "@bot-A" message re-applied after "@bot-C" would
        # roll ownership back to A and resurrect the double-answer bug.
        stale_reason = None
        if facts.message_id in state.applied_message_ids:
            stale_reason = "already_applied"
        elif _is_older_snowflake(facts.message_id, state.newest_applied_id()):
            stale_reason = "older_than_current_owner_event"

        if stale_reason is not None:
            return Decision(
                owner_id=state.owner_id,
                verdict="replay",
                reason=stale_reason,
                admit_self=_admission(
                    effective_owner=state.owner_id, facts=facts, verdict="replay"
                ),
            )

        decision, new_state = evaluate(state, facts, config, now)
        if new_state is not state:
            self._threads[thread_id] = new_state
            self._save(changed_thread_id=thread_id)
        return decision
