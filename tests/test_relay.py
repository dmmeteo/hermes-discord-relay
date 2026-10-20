"""Protocol boundaries and atomic receipt behavior without Discord transport."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from conftest import load_plugin_module

relay = load_plugin_module("relay")


def content(**overrides):
    values = dict(
        recipient="1000",
        request="a" * 32,
        guild="4242",
        thread="77",
        expires="1800000120",
        hop="1",
        body="Work for the human.",
    )
    values.update(overrides)
    return (
        f"<@{values['recipient']}> [[relay:v1 id={values['request']} "
        f"guild={values['guild']} thread={values['thread']} "
        f"expires={values['expires']} hop={values['hop']}]]\n{values['body']}"
    )


@pytest.mark.parametrize("raw", [None, False, {}, {"enabled": False}, {"allow": []}])
def test_config_is_off_without_explicit_enable(raw):
    assert not relay.RelayConfig.parse(raw).enabled


@pytest.mark.parametrize(
    "raw",
    [
        True,
        "true",
        [],
        {"enabled": "true"},
        {"enabled": 1},
        {"enabled": True, "senders": ["2000"]},
        {"allow": "*"},
        {"allow": [{"sender_id": "2000", "guild_id": "4242"}]},
        {"allow": [{"sender_id": "*", "guild_id": "4242", "thread_id": "77"}]},
        {"allow": [{"sender_id": 2000, "guild_id": "4242", "thread_id": "77"}]},
        {"allow": [{"sender_id": "02000", "guild_id": "4242", "thread_id": "77"}]},
        {"allow": [{"sender_id": str(2**64), "guild_id": "4242", "thread_id": "77"}]},
        {"allow": [{}] * 257},
        {"allow": None},
        {"deny": [False]},
    ],
)
def test_malformed_config_is_rejected(raw):
    with pytest.raises(ValueError):
        relay.RelayConfig.parse(raw)


def test_exact_directional_allow_and_deny_precedence():
    route = {"sender_id": "2000", "guild_id": "4242", "thread_id": "77"}
    config = relay.RelayConfig.parse({"enabled": True, "allow": [route]})
    assert config.permits(relay.RelayRoute("2000", "4242", "77"))
    assert not config.permits(relay.RelayRoute("1000", "4242", "77"))
    assert not config.permits(relay.RelayRoute("2000", "4242", "78"))
    assert not config.permits(relay.RelayRoute("2000", "4243", "77"))
    config = relay.RelayConfig.parse({"enabled": True, "allow": [route], "deny": [route]})
    assert not config.permits(relay.RelayRoute("2000", "4242", "77"))


def test_envelope_parses_exact_protocol():
    envelope = relay.RelayEnvelope.parse(content(), now=1800000000, created=1800000000)
    assert envelope is not None
    assert envelope.recipient_id == "1000"
    assert envelope.request_id == "a" * 32
    assert envelope.body == "Work for the human."


@pytest.mark.parametrize(
    "overrides",
    [
        dict(recipient="01000"),
        dict(recipient=str(2**64)),
        dict(request="A" * 32),
        dict(request="a"),
        dict(hop="2"),
        dict(hop="0"),
        dict(expires="1800000000"),
        dict(expires="1800000301"),
        dict(body=" "),
        dict(body="x" * 1401),
        dict(body="\x00"),
        dict(body="<@2000> do this"),
        dict(body="<@&123> hi"),
        dict(body="@everyone hi"),
        dict(body="@HERE hi"),
        dict(body="[[relay:v1 id=anything]]"),
        dict(body="[[handoff]]"),
    ],
)
def test_invalid_envelopes_are_denied(overrides):
    assert (
        relay.RelayEnvelope.parse(content(**overrides), now=1800000000, created=1800000000) is None
    )


@pytest.mark.parametrize("text", ["prefix ", "> ", "```", "\n"])
def test_wrapped_envelope_is_not_directly_addressed(text):
    assert relay.RelayEnvelope.parse(text + content(), now=1800000000, created=1800000000) is None


@pytest.mark.parametrize("created", [1799999699, 1800000031, float("nan"), float("inf")])
def test_stale_or_future_message_denied(created):
    assert relay.RelayEnvelope.parse(content(), now=1800000000, created=created) is None


def reserve(store, *, request="a" * 32, message="100", now=1800000000):
    return store.reserve(
        sender_id="2000", recipient_id="1000", request_id=request, message_id=message, now=now
    )


def test_receipts_survive_restart_and_deny_request_or_message_reuse(tmp_path):
    path = tmp_path / relay.STATE_FILENAME
    assert reserve(relay.RelayReceiptStore(path))
    assert not reserve(relay.RelayReceiptStore(path), message="101")
    assert not reserve(relay.RelayReceiptStore(path), request="b" * 32)
    assert reserve(relay.RelayReceiptStore(path), request="b" * 32, message="102")


def test_concurrent_connections_have_one_winner(tmp_path):
    path = tmp_path / relay.STATE_FILENAME
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: reserve(relay.RelayReceiptStore(path)), range(4)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 3


def test_full_store_refuses_new_work_instead_of_evicting_live_receipts(tmp_path):
    store = relay.RelayReceiptStore(tmp_path / relay.STATE_FILENAME)
    with patch.object(relay, "MAX_RECEIPTS", 2):
        assert reserve(store)
        assert reserve(store, request="b" * 32, message="101")
        assert not reserve(store, request="c" * 32, message="102")
        assert not reserve(store)
        assert reserve(store, request="c" * 32, message="102", now=1800000000 + relay.RETENTION)
    with sqlite3.connect(store.path) as db:
        assert db.execute("select count(*) from receipts").fetchone()[0] == 1


def test_corrupt_receipt_database_raises_instead_of_starting_empty(tmp_path):
    path = tmp_path / relay.STATE_FILENAME
    path.write_text("corrupted")
    with pytest.raises(sqlite3.DatabaseError):
        reserve(relay.RelayReceiptStore(path))


@pytest.mark.parametrize("skew", [-30, -0.5, 0, 30])
@pytest.mark.parametrize("ttl", [120, 270])
def test_recommended_ttl_tolerates_documented_clock_skew(skew, ttl):
    sender_now = 1800000000
    assert relay.RelayEnvelope.parse(content(expires=str(sender_now + ttl)),
                                     now=sender_now + skew, created=sender_now) is not None


def test_maximum_ttl_is_a_receiver_bound_without_upper_expiry_skew():
    assert relay.RelayEnvelope.parse(content(expires="1800000300"),
                                     now=1799999999.5, created=1800000000) is None
