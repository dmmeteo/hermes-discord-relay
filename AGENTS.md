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

This checkout is local `main`. Public `main` is one commit ahead (`4547de4`), which registers the Discord override through the plugin context during `register()`. Here `register()` in `__init__.py` still installs a deferred loader through `platform_registry.register_deferred`. That public commit states that, on current Hermes, a deferred call to `ctx.register_platform()` escapes the plugin lifecycle and can leave the built-in adapter in use. Do not describe the plugin-scope registration as present in this revision. Reconciling this branch with public `main` is an owner decision.

## Routes

| Task | Read |
|---|---|
| Registration and fallback to the built-in adapter | `__init__.py` (`register`, `_install_override`, `_fall_back_to_builtin`, `PLATFORM_ID`) |
| Admission override and bot gate | `adapter.py` (`DiscordCustomAdapter`, `_discord_message_admission`) |
| Ownership rules and per-profile store | `ownership.py` (`OwnershipConfig`, `ThreadOwnershipStore`) |
| `discord.thread_ownership_transfer` config and env switch | `config_bridge.py` (`apply_yaml_config`) |
| Borrowed Hermes internals | `compat.py` (`probe`, `require_supported`) |
| Manifest and install slug | `plugin.yaml`, `tests/test_install_layout.py` |

## Boundaries that must hold

- Ingress filtering only. The plugin decides which inbound Discord messages reach an agent. It makes no Discord API calls and never changes Discord's own thread owner.
- Off by default. A profile opts in through `discord.thread_ownership_transfer`.
- Every failure path ends at the built-in Discord adapter, and a partial Discord config is never applied.
- Bots cannot claim or steal ownership. The only bot-authored message that is force-admitted is a validated handoff from the current owner, within the guards.
- `bot_handoff: off` preserves the stock `DISCORD_ALLOW_BOTS` boundary exactly. A truthy env switch must never reopen it.
- The platform ID stays `discord`.
- An unresolved inline mention makes no ownership change.

## Authority

Owner approval is required to change a boundary above, the default `bot_handoff`, product scope, or a checklist priority. A checklist item is not permission to implement it. Push, installs into live profiles and gateway restarts need explicit authorization.

## Done means

An outcome is complete only when its user-visible completion condition is verified on the real artifact. An implementation-only change does not tick an item. Passing tests or documentation checks are not runtime or user acceptance. Update `planning/next-epics.md` only for a verified outcome or an owner scope decision.

## Checks

`./run-tests.sh` runs the suite against a Hermes checkout, and `ruff check .` lints. Details are in the README [Development](README.md#development) section.
