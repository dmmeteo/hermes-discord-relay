"""Compatibility layer over the private Hermes seams this plugin borrows.

This plugin subclasses the built-in Discord adapter, which is the only design
that can express thread ownership (see README, "Why subclass"). The price is a
dependency on core internals that are not API and that move: the
``_gate_raw`` / ``_scoped_gate_env`` / ``_get_allow_bots`` family, for
instance, does not exist on Hermes builds branched before it landed.

Rather than crash a gateway — or, worse, silently answer nothing — every
borrowed symbol is classified once, at load time:

**Required.** Without these the override cannot do its job at all. Missing any
of them raises :class:`IncompatibleHermesError`, which the registration path
turns into one loud ``ERROR`` log plus a fall back to the stock adapter. Discord
keeps working; the feature is simply off.

**Optional.** Seams with a faithful fallback here. Missing ones are named in a
single ``WARNING`` so an operator can see exactly which path is in use, and the
feature keeps its full behaviour.

The fallbacks intentionally mirror core's own semantics (env first, then
``config.extra``; the same CSV parsing) so a build with and a build without the
helpers behave identically.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Sentinel for "this attribute should be absent" in tests.
ABSENT = object()

#: The core module every seam is read from.
CORE_MODULE = "plugins.platforms.discord.adapter"

#: Module-level symbols the override cannot be built without.
REQUIRED_MODULE_SYMBOLS = ("DiscordAdapter", "register", "_apply_yaml_config")

#: Adapter methods the override calls or delegates to.
REQUIRED_ADAPTER_METHODS = (
    "_discord_message_admission",
    "_get_parent_channel_id",
    "_is_allowed_user",
    "_discord_channel_keys",
    "disconnect",
)

#: Adapter methods with a fallback in this module.
OPTIONAL_ADAPTER_METHODS = (
    "_gate_raw",
    "_gate_env",
    "_get_allow_bots",
    "_snapshot_gate_env",
    "_get_allowed_channels",
    "_get_ignored_channels",
)

_ALLOW_BOTS_ENV = "DISCORD_ALLOW_BOTS"
_ALLOWED_CHANNELS_ENV = "DISCORD_ALLOWED_CHANNELS"
_IGNORED_CHANNELS_ENV = "DISCORD_IGNORED_CHANNELS"


class IncompatibleHermesError(RuntimeError):
    """The installed Hermes lacks a seam the override requires."""


@dataclass(frozen=True)
class CoreCompat:
    """What the installed core does and does not offer."""

    module_name: str
    missing_required: tuple
    missing_optional: tuple
    has_gate_raw: bool
    has_allow_bots_hook: bool
    has_gate_env_snapshot: bool
    has_channel_gate_helpers: bool

    @property
    def supported(self) -> bool:
        return not self.missing_required


def _has_method(cls: Any, name: str) -> bool:
    return callable(getattr(cls, name, None))


def _takes_claim_keyword(cls: Any) -> bool:
    """``_discord_message_admission(message, *, claim=...)`` — still?

    The plugin both calls ``super()`` with ``claim=`` and is called with it by
    core. A rename would raise ``TypeError`` deep inside ``on_message``, which
    is a far worse failure than declining to install.
    """
    method = getattr(cls, "_discord_message_admission", None)
    if method is None:
        return False
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins/C functions
        return True
    if "claim" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def probe(module: Any = None) -> CoreCompat:
    """Classify the seams offered by *module* (the core adapter by default)."""
    if module is None:
        module = __import__(CORE_MODULE, fromlist=["*"])

    missing_required: list = []
    for symbol in REQUIRED_MODULE_SYMBOLS:
        if getattr(module, symbol, None) is None:
            missing_required.append(symbol)

    adapter_cls = getattr(module, "DiscordAdapter", None)
    if adapter_cls is not None:
        for name in REQUIRED_ADAPTER_METHODS:
            if not _has_method(adapter_cls, name):
                missing_required.append(f"DiscordAdapter.{name}")
        if _has_method(adapter_cls, "_discord_message_admission") and not (
            _takes_claim_keyword(adapter_cls)
        ):
            missing_required.append(
                "DiscordAdapter._discord_message_admission(claim=...) keyword"
            )

    missing_optional = tuple(
        f"DiscordAdapter.{name}"
        for name in OPTIONAL_ADAPTER_METHODS
        if adapter_cls is None or not _has_method(adapter_cls, name)
    )

    has_scoped_gate_env = callable(getattr(module, "_scoped_gate_env", None))
    return CoreCompat(
        module_name=getattr(module, "__name__", CORE_MODULE),
        missing_required=tuple(missing_required),
        missing_optional=missing_optional,
        has_gate_raw=_has_method(adapter_cls, "_gate_raw"),
        has_allow_bots_hook=_has_method(adapter_cls, "_get_allow_bots"),
        has_gate_env_snapshot=(
            _has_method(adapter_cls, "_snapshot_gate_env") and has_scoped_gate_env
        ),
        has_channel_gate_helpers=(
            _has_method(adapter_cls, "_get_allowed_channels")
            and _has_method(adapter_cls, "_get_ignored_channels")
        ),
    )


def require_supported(result: CoreCompat) -> None:
    """Raise unless every required seam is present."""
    if result.supported:
        return
    raise IncompatibleHermesError(
        "This Hermes build does not expose the internals the Discord thread "
        "ownership override needs. Missing: "
        + ", ".join(result.missing_required)
        + f" (checked {result.module_name}). The built-in Discord adapter will "
        "be used unchanged. Update the plugin, or Hermes, so the two match."
    )


def describe(result: CoreCompat) -> str:
    """One line naming the optional seams running on a fallback."""
    if not result.missing_optional:
        return "all core gate helpers present"
    return (
        "using compatibility fallback for: "
        + ", ".join(result.missing_optional)
    )


# ── fallbacks ───────────────────────────────────────────────────────────────


def csv_set(raw: Any) -> set:
    """Parse a channel/id list the way core's ``_gate_csv_set`` does."""
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def gate_raw(adapter: Any, extra_key: str, env_key: str) -> Any:
    """Resolve one gate value: env (or the adapter's snapshot) first, then extra.

    Delegates to core's ``_gate_raw`` when it exists, so multiplex profile
    scoping is honoured. The fallback keeps the same precedence — which is what
    makes an env kill switch work identically on both builds — but reads the
    process-global environment, because a build without ``_gate_raw`` has no
    per-adapter snapshot to read instead.
    """
    helper = getattr(adapter, "_gate_raw", None)
    if callable(helper):
        return helper(extra_key, env_key)

    value = (os.getenv(env_key) or "").strip()
    if value:
        return value
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return extra.get(extra_key)
    return None


def gate_env(adapter: Any, env_key: str, default: str = "") -> str:
    """Read one gate env var, honouring the adapter's snapshot where there is one.

    Separate from :func:`gate_raw` on purpose: that helper collapses env and
    ``config.extra`` into a single value, and composing the two correctly is
    impossible once they have been flattened.
    """
    helper = getattr(adapter, "_gate_env", None)
    if callable(helper):
        try:
            return helper(env_key, default)
        except TypeError:  # pragma: no cover - a one-argument variant
            return helper(env_key)
    return scoped_gate_env(env_key, default)


def extra_value(adapter: Any, extra_key: str) -> Any:
    """Read one key from this adapter's ``config.extra``.

    ``extra`` is per-adapter and therefore the profile-safe carrier: it is
    where the YAML bridge puts the value, and it never leaks across profiles
    the way process-global env does.
    """
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return extra.get(extra_key)
    return None


def allow_bots_from_env() -> str:
    """Read ``DISCORD_ALLOW_BOTS`` the way a core without the hook reads it.

    Deliberately takes no adapter: the caller is the adapter's *own*
    ``_get_allow_bots`` override, so dispatching back through the instance
    would recurse.
    """
    return (os.getenv(_ALLOW_BOTS_ENV) or "none").lower().strip() or "none"


def allowed_channels(adapter: Any) -> set:
    helper = getattr(adapter, "_get_allowed_channels", None)
    if callable(helper):
        return helper()
    return csv_set(gate_raw(adapter, "allowed_channels", _ALLOWED_CHANNELS_ENV))


def ignored_channels(adapter: Any) -> set:
    helper = getattr(adapter, "_get_ignored_channels", None)
    if callable(helper):
        return helper()
    return csv_set(gate_raw(adapter, "ignored_channels", _IGNORED_CHANNELS_ENV))


@contextlib.contextmanager
def forced_allow_bots(active: bool) -> Iterator[None]:
    """Widen the bot gate for one synchronous admission call.

    *active* is the caller's answer to "does this core lack ``_get_allow_bots``
    and does this message need the gate widened?".  It is passed as a plain
    bool rather than derived from the adapter, because the adapter always
    defines the override — asking the instance would say "hook present" on
    every build and quietly disable the fallback.

    When active, the env var is the only seam a legacy core exposes: its
    admission gate reads ``os.environ`` inline.  It is restored
    unconditionally, and the window is a single ``super()`` call with no
    ``await`` in it, so no other coroutine on the loop can observe the widened
    value.
    """
    if not active:
        yield
        return

    previous = os.environ.get(_ALLOW_BOTS_ENV)
    os.environ[_ALLOW_BOTS_ENV] = "mentions"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_ALLOW_BOTS_ENV, None)
        else:
            os.environ[_ALLOW_BOTS_ENV] = previous


# ── the lazy discord.py dependency ──────────────────────────────────────────

#: ``discord`` is a lazily-installed Hermes extra, not a core dependency, so it
#: can legitimately be absent on the first gateway start after installing this
#: plugin. Cached rather than re-imported per message; cleared by tests.
_DISCORD_CACHE: dict = {}


def discord_module() -> Any | None:
    """Return the ``discord`` module, or ``None`` when it is not installed.

    Core guards its own import the same way. A bare ``import discord`` at
    module scope here would make the *plugin* the thing that fails on a host
    where discord.py has not been lazily installed yet — the override would
    raise, the gateway would fall back to the stock adapter, and the feature
    would silently vanish until the next restart.
    """
    if "module" not in _DISCORD_CACHE:
        try:
            import discord  # noqa: PLC0415 - deliberately deferred
        except Exception:  # ImportError, and partially-installed wheels
            _DISCORD_CACHE["module"] = None
        else:
            _DISCORD_CACHE["module"] = discord
    return _DISCORD_CACHE["module"]


def ensure_discord_installed() -> bool:
    """Give core a chance to lazily install discord.py, then re-check.

    ``check_discord_requirements`` is the same entry point the platform
    registry's ``check_fn`` uses; calling it here means the first gateway start
    after ``hermes plugins install`` gets the dependency in place instead of
    quietly degrading to the built-in adapter for one run.
    """
    if discord_module() is not None:
        return True
    try:
        from plugins.platforms.discord.adapter import check_discord_requirements

        check_discord_requirements()
    except Exception:
        logger.debug("Lazy discord.py install probe failed", exc_info=True)
    _DISCORD_CACHE.clear()
    return discord_module() is not None


def scoped_gate_env(name: str, default: str = "") -> str:
    """Read a gate env var through core's profile-scoped reader when it exists.

    Falls back to a plain environment read, which is what a build without the
    scoped reader does for every other Discord gate anyway.
    """
    try:
        from plugins.platforms.discord.adapter import _scoped_gate_env
    except ImportError:
        _scoped_gate_env = None
    if callable(_scoped_gate_env):
        try:
            return _scoped_gate_env(name, default)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Scoped gate env read failed for %s", name, exc_info=True)
    return (os.getenv(name) or default).strip()
