# AGENTS.md

Cold-start map for agents working in this repository. [README.md](README.md) owns the product purpose, behavior, configuration, compatibility rules and operational limits. [planning/next-epics.md](planning/next-epics.md) is the only outcome checklist.

The repository root is the installed plugin package. Files added here ship with every install.

## Start and recover

1. Read this file.
2. Read the README [behaviour](README.md#the-behaviour) table and the [bot ingress security note](README.md#security-note-what-bot_handoff-changes-about-discord_allow_bots) before any change to admission or handoff.
3. Read [Hermes compatibility](README.md#hermes-compatibility) before touching a borrowed Hermes internal.
4. Read the selected outcome in `planning/next-epics.md`. Work on one selected outcome at a time.

After compaction or context loss, re-read this file, the behaviour table, the security note and the selected outcome before the next consequential edit.

## Revision state

Discord Relay 1.1.0 includes the owner-authorized exact-sender and
exact-destination relay expansion of 2026-10-20, separate from ownership.
`register()` installs the Discord override through the supplied plugin context,
inside the profile-scoped lifecycle that Hermes owns. Native adapter, security,
and lifecycle tests cover source behavior. Publication, tagging, profile
installation, gateway restart, and live guild acceptance remain pending.
The revision used by a live profile is not recorded here.
The source manifest is now `hermes-discord-relay`. Existing technical config keys
and state filenames remain unchanged. Read [the identity migration](docs/migrate-to-discord-relay.md)
and [the identity decision](docs/adr/0003-discord-relay-identity.md)
before changing install identity or activation metadata. The old-to-new and
rollback checks use disposable homes; live migration and installer acceptance
remain pending. [Landing copy](docs/landing-page.md) is a source artifact for later
site integration.
Read [the relay decision](docs/adr/0002-controlled-discord-relay.md) before
changing relay admission, receipts, authorization, or acknowledgement behavior.
Read [the registration decision](docs/adr/0001-plugin-scoped-registration.md)
before changing host-owned registration timing.

## Routes

| Task | Read |
|---|---|
| Registration and fallback to the built-in adapter | `__init__.py` (`register`, `_install_override`, `_fall_back_to_builtin`, `PLATFORM_ID`) |
| Admission override and bot gate | `adapter.py` (`DiscordCustomAdapter`, `_discord_message_admission`) |
| Relay schema, protocol and durable receipts | `relay.py` (`RelayConfig`, `RelayEnvelope`, `RelayReceiptStore`), `adapter.py` (`_relay_admission`, `_handle_message`) |
| Ownership rules and per-profile store | `ownership.py` (`OwnershipConfig`, `ThreadOwnershipStore`) |
| `discord.thread_ownership_transfer` config and env switch | `config_bridge.py` (`apply_yaml_config`) |
| Borrowed Hermes internals | `compat.py` (`probe`, `require_supported`) |
| Manifest and install slug | `plugin.yaml`, `tests/test_install_layout.py` |

## Boundaries that must hold

- Ingress filtering only. The plugin decides which inbound Discord messages reach an agent. It makes no Discord API calls and never changes Discord's own thread owner.
- Off by default. Ownership opts in through `discord.thread_ownership_transfer`; relay separately requires `discord.relay.enabled: true`.
- Every failure path ends at the built-in Discord adapter, and a partial Discord config is never applied.
- Bots cannot claim or steal ownership. Force-admission is limited to a guarded owner handoff or an explicitly configured validated relay. Relay never changes ownership.
- With relay disabled, `bot_handoff: off` preserves the stock `DISCORD_ALLOW_BOTS` boundary exactly. A truthy ownership env switch must never reopen it or enable relay.
- Relay preserves bot identity and native human authorization. Routes and payload text never authenticate human identity or approval. No global bot-gate expansion, webhook, self, stale, duplicate, or reforwarded relay admission.
- Relay reserves a durable bounded receipt before admission. Storage failure denies. No transport result or admission log is represented as completion.
- The platform ID stays `discord`.
- An unresolved inline mention makes no ownership change.

## Authority

Owner approval is required to change a boundary above, the default `bot_handoff`, product scope, or a checklist priority. A checklist item is not permission to implement it. Push, installs into live profiles and gateway restarts need explicit authorization.

## Done means

An outcome is complete only when its user-visible completion condition is verified on the real artifact. An implementation-only change does not tick an item. Passing tests or documentation checks are not runtime or user acceptance. Update `planning/next-epics.md` only for a verified outcome or an owner scope decision.

## Checks

`./run-tests.sh` runs the suite against a Hermes checkout, and `ruff check .` lints. Details are in the README [Development](README.md#development) section.
