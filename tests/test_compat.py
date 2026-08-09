"""Version-compatibility probe over the private Hermes seams we borrow.

This plugin subclasses a core adapter and therefore depends on internal
symbols.  Hermes moves fast, and the seams are not API.  The contract these
tests pin down is:

* **required** symbols missing  → refuse to install the override, loudly, and
  let the caller fall back to the stock adapter;
* **optional** seams missing    → keep the feature, on a documented fallback.

Anything else (a silent degrade, or a hard crash that takes Discord down with
it) is a bug.
"""

from __future__ import annotations

import os
import types
from unittest.mock import patch

import pytest
from conftest import load_plugin_module

compat = load_plugin_module("compat")


class _StubAdapter:
    """A minimal stand-in carrying every *required* adapter method."""

    def _discord_message_admission(self, message, *, claim):
        return True, False

    def _get_parent_channel_id(self, channel):
        return None

    def _is_allowed_user(self, user_id, author=None, **kwargs):
        return True

    def _discord_channel_keys(self, message, parent_channel_id=None):
        return set()

    async def disconnect(self):
        return None


def _stub_module(adapter_cls=_StubAdapter, **overrides):
    module = types.ModuleType("stub_discord_adapter")
    module.DiscordAdapter = adapter_cls
    module.register = lambda ctx: None
    module._apply_yaml_config = lambda yaml_cfg, discord_cfg: {}
    for key, value in overrides.items():
        if value is compat.ABSENT:
            delattr(module, key)
        else:
            setattr(module, key, value)
    return module


# ── the probe ───────────────────────────────────────────────────────────────


class TestProbeAgainstRealCore:
    """The installed Hermes must at least satisfy the required set."""

    def test_the_installed_core_is_supported(self):
        assert compat.probe().supported

    def test_no_required_symbol_is_missing(self):
        assert compat.probe().missing_required == ()

    def test_the_probe_names_the_core_module_it_examined(self):
        assert compat.probe().module_name == "plugins.platforms.discord.adapter"


class TestRequiredSymbols:
    def test_a_missing_module_symbol_is_reported(self):
        module = _stub_module()
        del module._apply_yaml_config
        result = compat.probe(module)
        assert not result.supported
        assert "_apply_yaml_config" in result.missing_required

    def test_a_missing_adapter_method_is_reported(self):
        class Crippled:
            """Everything required except the admission gate itself."""

            def _get_parent_channel_id(self, channel):
                return None

            def _is_allowed_user(self, user_id, author=None, **kwargs):
                return True

            def _discord_channel_keys(self, message, parent_channel_id=None):
                return set()

            async def disconnect(self):
                return None

        result = compat.probe(_stub_module(Crippled))
        assert not result.supported
        assert "DiscordAdapter._discord_message_admission" in result.missing_required

    def test_admission_must_still_take_the_claim_keyword(self):
        class Renamed(_StubAdapter):
            def _discord_message_admission(self, message, claimed=True):
                return True, False

        result = compat.probe(_stub_module(Renamed))
        assert not result.supported
        assert any("claim" in item for item in result.missing_required)

    def test_require_supported_raises_and_names_every_gap(self):
        module = _stub_module()
        del module._apply_yaml_config
        del module.register
        result = compat.probe(module)
        with pytest.raises(compat.IncompatibleHermesError) as excinfo:
            compat.require_supported(result)
        message = str(excinfo.value)
        assert "_apply_yaml_config" in message
        assert "register" in message

    def test_require_supported_is_a_no_op_when_supported(self):
        compat.require_supported(compat.probe(_stub_module()))


# ── optional seams ──────────────────────────────────────────────────────────


class TestOptionalSeams:
    def test_a_core_without_the_gate_helpers_is_still_supported(self):
        result = compat.probe(_stub_module())
        assert result.supported
        assert not result.has_gate_raw
        assert not result.has_allow_bots_hook
        assert not result.has_gate_env_snapshot
        assert not result.has_channel_gate_helpers

    def test_the_missing_optional_seams_are_listed_for_the_log(self):
        result = compat.probe(_stub_module())
        assert "DiscordAdapter._gate_raw" in result.missing_optional

    def test_a_core_with_the_gate_helpers_advertises_them(self):
        class Modern(_StubAdapter):
            def _gate_raw(self, extra_key, env_key):
                return None

            def _get_allow_bots(self):
                return "none"

            def _snapshot_gate_env(self):
                return None

            def _get_allowed_channels(self):
                return set()

            def _get_ignored_channels(self):
                return set()

        module = _stub_module(Modern, _scoped_gate_env=lambda name, default="": "")
        result = compat.probe(module)
        assert result.supported
        assert result.has_gate_raw
        assert result.has_allow_bots_hook
        assert result.has_gate_env_snapshot
        assert result.has_channel_gate_helpers
        assert result.missing_optional == ()

    def test_the_snapshot_seam_needs_both_halves(self):
        """``_snapshot_gate_env`` without ``_scoped_gate_env`` is not usable."""

        class Half(_StubAdapter):
            def _snapshot_gate_env(self):
                return None

        assert not compat.probe(_stub_module(Half)).has_gate_env_snapshot

    def test_describe_is_human_readable(self):
        text = compat.describe(compat.probe(_stub_module()))
        assert "fallback" in text.lower()


# ── the fallbacks themselves ────────────────────────────────────────────────


class _Config:
    def __init__(self, extra=None):
        self.extra = extra if extra is not None else {}


class _Legacy:
    """An adapter with no gate helpers at all — the old-core shape."""

    def __init__(self, extra=None):
        self.config = _Config(extra)


class _Modern(_Legacy):
    def _gate_raw(self, extra_key, env_key):
        return f"from-gate-raw:{extra_key}"

    def _get_allow_bots(self):
        return "all"

    def _get_allowed_channels(self):
        return {"from-helper"}

    def _get_ignored_channels(self):
        return {"ignored-helper"}


class TestGateRawFallback:
    def test_it_delegates_when_the_core_helper_exists(self):
        assert compat.gate_raw(_Modern(), "k", "K") == "from-gate-raw:k"

    def test_env_wins_over_extra(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TEST_KEY", "from-env")
        adapter = _Legacy({"test_key": "from-extra"})
        assert compat.gate_raw(adapter, "test_key", "DISCORD_TEST_KEY") == "from-env"

    def test_extra_is_used_when_env_is_unset(self, monkeypatch):
        monkeypatch.delenv("DISCORD_TEST_KEY", raising=False)
        adapter = _Legacy({"test_key": "from-extra"})
        assert compat.gate_raw(adapter, "test_key", "DISCORD_TEST_KEY") == "from-extra"

    def test_an_empty_env_value_does_not_mask_extra(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TEST_KEY", "   ")
        adapter = _Legacy({"test_key": "from-extra"})
        assert compat.gate_raw(adapter, "test_key", "DISCORD_TEST_KEY") == "from-extra"

    def test_missing_everywhere_is_none(self, monkeypatch):
        monkeypatch.delenv("DISCORD_TEST_KEY", raising=False)
        assert compat.gate_raw(_Legacy(), "test_key", "DISCORD_TEST_KEY") is None

    def test_an_adapter_without_a_config_does_not_explode(self, monkeypatch):
        monkeypatch.delenv("DISCORD_TEST_KEY", raising=False)
        assert compat.gate_raw(object(), "test_key", "DISCORD_TEST_KEY") is None


class TestCsvSet:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, set()),
            ("", set()),
            ("a, b ,c", {"a", "b", "c"}),
            (["a", " b "], {"a", "b"}),
            ("*", {"*"}),
            (12345, {"12345"}),
        ],
    )
    def test_parsing_matches_core_semantics(self, raw, expected):
        assert compat.csv_set(raw) == expected


class TestAllowBotsFallback:
    def test_it_reads_the_env_var(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOW_BOTS", " MENTIONS ")
        assert compat.allow_bots_from_env() == "mentions"

    def test_the_default_is_none(self, monkeypatch):
        monkeypatch.delenv("DISCORD_ALLOW_BOTS", raising=False)
        assert compat.allow_bots_from_env() == "none"

    def test_it_takes_no_adapter(self):
        """Dispatching through the adapter would recurse into the override."""
        import inspect

        assert not inspect.signature(compat.allow_bots_from_env).parameters


class TestChannelGateFallback:
    def test_it_delegates_when_the_core_helpers_exist(self):
        assert compat.allowed_channels(_Modern()) == {"from-helper"}
        assert compat.ignored_channels(_Modern()) == {"ignored-helper"}

    def test_it_falls_back_to_env_and_extra(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "111,222")
        monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
        adapter = _Legacy({"ignored_channels": ["333"]})
        assert compat.allowed_channels(adapter) == {"111", "222"}
        assert compat.ignored_channels(adapter) == {"333"}

    def test_unset_means_no_gate(self, monkeypatch):
        monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
        monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
        assert compat.allowed_channels(_Legacy()) == set()
        assert compat.ignored_channels(_Legacy()) == set()


class TestForcedAllowBots:
    """Widening the bot gate for exactly one synchronous admission call."""

    def test_the_env_var_is_restored_afterwards(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
        with compat.forced_allow_bots(True):
            assert os.environ["DISCORD_ALLOW_BOTS"] == "mentions"
        assert os.environ["DISCORD_ALLOW_BOTS"] == "none"

    def test_an_unset_var_is_removed_again(self, monkeypatch):
        monkeypatch.delenv("DISCORD_ALLOW_BOTS", raising=False)
        with compat.forced_allow_bots(True):
            assert os.environ["DISCORD_ALLOW_BOTS"] == "mentions"
        assert "DISCORD_ALLOW_BOTS" not in os.environ

    def test_it_is_restored_even_when_the_body_raises(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
        with pytest.raises(RuntimeError):
            with compat.forced_allow_bots(True):
                raise RuntimeError("boom")
        assert os.environ["DISCORD_ALLOW_BOTS"] == "none"

    def test_inactive_touches_nothing(self, monkeypatch):
        """On a core with ``_get_allow_bots`` the adapter overrides that instead."""
        monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
        with compat.forced_allow_bots(False):
            assert os.environ["DISCORD_ALLOW_BOTS"] == "none"


# ── the lazy discord dependency ─────────────────────────────────────────────


class TestDiscordModule:
    def test_it_returns_the_real_module_when_installed(self):
        assert compat.discord_module() is not None

    def test_it_returns_none_instead_of_raising_when_absent(self):
        with patch.dict("sys.modules", {"discord": None}):
            compat._DISCORD_CACHE.clear()
            try:
                assert compat.discord_module() is None
            finally:
                compat._DISCORD_CACHE.clear()
