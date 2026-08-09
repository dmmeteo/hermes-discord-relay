# hermes-discord-thread-ownership

A [Hermes](https://github.com/NousResearch/hermes-agent) platform plugin that
gives Discord threads a **single owner**: inside a thread exactly one bot
answers at a time, and you move ownership by @mentioning whichever bot you
want.

It subclasses the built-in Discord adapter and overrides only admission —
connection, voice, slash commands, media, auth, reactions, threads and cron
delivery are inherited unchanged. Off by default; opt in per profile.

```yaml
discord:
  thread_ownership_transfer: true
```

---

## The problem

Run several Hermes profiles as separate bots in one Discord server and every bot
that has ever participated in a thread answers every unmentioned follow-up.
`discord.thread_require_mention: true` fixes the noise but breaks conversation:
you then have to @mention on *every* message.

## The behaviour

When enabled, inside a **guild thread**:

| Event | Result |
|---|---|
| Human @mentions bot **B** | B owns the thread |
| Human posts with no bot mention | Only the owner answers |
| Human @mentions bot **C** | C owns it; B goes silent immediately |
| Human @mentions a *human* | Ownership unchanged |
| Human @mentions several bots | First mention in the message wins; the rest stay silent |
| A non-owner bot posts anything | Ownership unchanged — bots cannot claim or steal |
| The **owner** posts `[[handoff]] @C` | C owns it; the owner goes silent (agent-to-agent handoff) |

DMs, plain channels, and every non-thread path are untouched.

Ownership survives `/new` and a gateway restart: it is cached per profile in
`$HERMES_HOME/discord_thread_ownership.json`.

**It makes no Discord API calls.** "Thread ownership" here is a Hermes-internal
notion, unrelated to Discord's own thread `owner_id`. The feature is a pure
ingress admission filter.

---

## Install

```bash
hermes plugins install dmmeteo/hermes-discord-thread-ownership
hermes plugins enable hermes-discord-thread-ownership
```

`hermes plugins install` clones this repository into
`~/.hermes/plugins/hermes-discord-thread-ownership/` — which is why the
repository root *is* the plugin package, with `plugin.yaml` and `__init__.py`
at the top level. There is no install script to run and nothing to `pip
install`.

The install directory and the `plugins enable` key both come from
`plugin.yaml`'s `name`, never from the directory a clone happens to sit in, so
a local checkout may be called anything. The published slug is
`dmmeteo/hermes-discord-thread-ownership`, and the manifest name matches it —
`tests/test_install_layout.py` asserts both, and that every command in this
README uses them.

Then enable the feature in that profile's `config.yaml`:

```yaml
discord:
  thread_ownership_transfer: true
```

and restart the gateway:

```bash
hermes gateway restart
```

### Several profiles

Each profile is an independent island with its own `HERMES_HOME`, its own
`plugins/` directory, its own `config.yaml` and its own bot token — so install
and enable once per profile:

```bash
for p in default alpha beta; do
  hermes -p "$p" plugins install dmmeteo/hermes-discord-thread-ownership
  hermes -p "$p" plugins enable hermes-discord-thread-ownership
  hermes -p "$p" gateway restart
done
```

Ownership state is per-profile too, which is exactly right: each bot keeps its
own view and they converge from the shared event stream.

Verify the override took effect:

```bash
hermes gateway status     # Discord still listed, still connected
grep 'thread ownership adapter registered' ~/.hermes/logs/gateway.log
```

### Update and rollback

```bash
hermes plugins update hermes-discord-thread-ownership
hermes plugins disable hermes-discord-thread-ownership   # keep it installed
hermes plugins remove  hermes-discord-thread-ownership   # uninstall
```

Disabling restores the bundled Discord adapter on the next gateway start;
nothing else changes. To also clear ownership state, delete
`$HERMES_HOME/discord_thread_ownership.json`.

---

## Configuration

One key, under the existing `discord:` block:

```yaml
discord:
  thread_ownership_transfer: true
```

Or the long form, when you want to tune the handoff guards:

```yaml
discord:
  thread_ownership_transfer:
    enabled: true
    bot_handoff: marker          # marker | mention | off
    handoff_marker: "[[handoff]]"
    max_handoffs: 8
    handoff_window_seconds: 3600
    handoff_cooldown_seconds: 30
```

Writing the mapping is itself the opt-in — `enabled` defaults to true inside
it, and an explicit `enabled: false` still wins.

**Zero semantics:** `max_handoffs: 0` freezes bot-initiated handoff entirely.
`handoff_window_seconds: 0` is clamped to a small positive floor, so both knobs
read the same direction — smaller is stricter. (Unclamped, a zero window would
have made the budget match nothing and silently disabled the limit.)

**Env override:** `DISCORD_THREAD_OWNERSHIP_TRANSFER` is an internal bridge for
parity with the other `DISCORD_*` gates; `config.yaml` is the user-facing
setting. It is a **switch, not an alternative config source** — it moves
`enabled` and nothing else:

| `DISCORD_THREAD_OWNERSHIP_TRANSFER` | Effect |
|---|---|
| `false` / `0` / `no` / `off` | Kill switch. Wins over any `config.yaml`. |
| `true` / `1` / `yes` / `on` | Turns the feature on. **Every guard in the mapping form — `bot_handoff`, `handoff_marker`, `max_handoffs`, the window and cooldown — is preserved.** |
| unset, empty, or unrecognised | No opinion; `config.yaml` stands on its own. |

In particular a truthy env value cannot resurrect `bot_handoff` from `off` back
to the `marker` default, so it can never re-open the bot ingress path described
in the security note below.

### Security note: what `bot_handoff` changes about `DISCORD_ALLOW_BOTS`

**Enabling this feature with the default `bot_handoff: marker` widens Discord's
bot ingress boundary.** Read this before enabling it on a server with
third-party bots.

Core's admission gate treats `DISCORD_ALLOW_BOTS` as the *only* gate for
bot-authored messages — the human allowlist (`DISCORD_ALLOWED_USERS` /
`DISCORD_ALLOWED_ROLES`) is not consulted on that branch at all. A validated
handoff is force-admitted past it. The full chain, on stock defaults:

1. An allowlisted human @mentions third-party bot **X** in a thread.
2. **X** becomes the thread owner — that is the feature working as designed.
3. **X** posts `[[handoff]] @us`.
4. We admit that message even though `DISCORD_ALLOW_BOTS=none`, and it becomes
   an agent turn.

So a bot you do not control can hand work to your agent, if a human you trust
put it in the thread first. The guards are real and tested — owner only, marker
required, no self-handoff, ambiguity refused, bounce guard, hop budget — and
they bound *who* and *how often*, but they do not restore the original
boundary.

| Setting | Bot ingress boundary |
|---|---|
| `bot_handoff: marker` (default) | Widened as above: the current owner, and only the current owner, can force-admit one handoff addressed to you. |
| `bot_handoff: mention` | Same widening, without the marker requirement — an owner mentioning exactly one other bot is enough. Loosest. |
| `bot_handoff: off` | **Stock boundary preserved exactly.** No bot-authored message is ever force-admitted; `DISCORD_ALLOW_BOTS` means precisely what it means without this plugin. Ownership still silences non-owners on human messages, which is the part most people want. |

If you only want "one of my bots answers per thread" and never agent-to-agent
handoff, set `bot_handoff: off`:

```yaml
discord:
  thread_ownership_transfer:
    enabled: true
    bot_handoff: off
```

The default is `marker` rather than `off` because agent-to-agent handoff is the
second half of the feature, and the marker requirement already makes a handoff
an explicit, greppable act rather than an accident of conversation.

### Credential and config reuse

Nothing to re-enter. The plugin registers under platform ID `discord` and
inherits the built-in `PlatformEntry` wholesale, then overrides two callables:

| Concern | Source |
|---|---|
| `DISCORD_BOT_TOKEN` | built-in, unchanged |
| `DISCORD_ALLOWED_USERS` / `DISCORD_ALLOW_ALL_USERS` / roles | built-in, unchanged |
| `DISCORD_HOME_CHANNEL`, cron `deliver=discord` | built-in, unchanged |
| Every `discord.*` key in `config.yaml` | built-in bridge, called first |
| `hermes setup` Discord wizard | built-in `setup_fn`, unchanged |
| `discord` / `discord_admin` toolsets | unchanged |
| Adapter construction | **overridden** → `DiscordCustomAdapter` |
| YAML bridge | **overridden** → built-in bridge, then `thread_ownership_transfer` |

---

## How it works

### Ownership is derived, not coordinated

Each bot observes the same thread events and applies the same rules, so several
profile gateways converge on the same owner without a shared database, a lock,
or any cross-profile IPC. The on-disk file is only a per-profile cache so the
derivation survives a restart.

Convergence holds as long as every bot sees the same inputs. Two things can
break that, and both are handled explicitly:

- **Mentions that do not resolve.** `message.mentions` is not always populated
  (mobile, edited, and relayed messages can carry the mention only in the raw
  content). The adapter resolves inline `<@id>` tokens through the guild member
  cache and the client user cache; if any inline id still cannot be resolved,
  **no ownership change is made at all**. "Refuse" is a verdict every bot
  reaches identically, whereas guessing is not.
- **Allowlists.** See the prerequisite below.

### Prerequisite: participating profiles must share one allowlist

Ownership is only observed for messages the bot is willing to act on, which
means each profile filters through its own `DISCORD_ALLOWED_USERS` /
`DISCORD_ALLOWED_ROLES`. **Every bot sharing a thread must trust the same set
of humans**, or they will disagree about who owns it.

Concretely, if human H is allowlisted for `alpha` but not for `beta`, and H
mentions `beta` in a thread `alpha` owns: `alpha` records the transfer and goes
quiet, while `beta` never saw the message and records no owner. `beta` then
falls back to built-in behaviour and answers unmentioned follow-ups. The thread
is not deadlocked, but the double-answer behaviour returns for that thread until
a mutually-trusted account mentions a bot.

Verify before enabling — the lists (and any role-based auth) should match:

```bash
grep -h DISCORD_ALLOWED_USERS ~/.hermes/.env ~/.hermes/profiles/*/.env
```

### Bot-to-bot handoff, and its guards

An agent hands its thread to another agent by emitting the marker plus a single
mention:

```
[[handoff]] <@OTHER_BOT_ID>
```

The receiving bot is admitted even though `DISCORD_ALLOW_BOTS` is `none` — this
is the only case where a bot-authored message is force-admitted.

Guards, all bot-initiated only (a human @mention always overrides and resets
them):

- **Marker required** (`bot_handoff: marker`, the default) — distinguishes an
  intentional handoff from the owner merely naming another bot in conversation.
  `bot_handoff: mention` accepts any owner mention of one other bot;
  `bot_handoff: off` disables bot handoff entirely.
- **Owner only** — a non-owner bot's marker is ignored, so no bot can forge a
  reassignment.
- **No self-handoff.**
- **Ambiguity refused** — a handoff naming two or more bots is a no-op. This is
  deliberately stricter than the human path: a human is present to correct a bad
  guess, an autonomous cascade is not.
- **Bounce guard** — handing straight back to the bot that just handed to you is
  refused within `handoff_cooldown_seconds` (default 30s).
- **Hop budget** — at most `max_handoffs` (default 8) bot-initiated handoffs per
  thread per `handoff_window_seconds` (default 1h). Exhausting it freezes bot
  handoff on that thread until the window rolls over.

Only inline `<@id>` tokens count. A Discord reply-ping adds the replied-to bot
to `message.mentions` without anyone typing a handle, so replies never move
ownership.

### Why the platform ID stays `discord`

A separate ID such as `discord-custom` is not safe in this codebase:

- `_TOOLSET_PLATFORM_RESTRICTIONS` in `hermes_cli/tools_config.py` pins the
  `discord` and `discord_admin` toolsets to the `discord` platform. Any other ID
  cannot enable them at all.
- A plugin platform's default composite toolset resolves as `hermes-<platform>`,
  so `discord-custom` would resolve to core tools only.
- Roughly 40 core sites compare `Platform.DISCORD` / `== "discord"` — voice
  mode, thread routing, reply mode, the channel directory, the relay,
  `send_message`, cron delivery. All would silently skip a new ID.

Registering over `discord` is a supported registry operation:
`PlatformRegistry.register()` is last-writer-wins, and `register_deferred`
replaces a pending loader for the same name. The plugin name, the adapter class
and the config key carry the distinct identity instead — the places an operator
actually reads. `PLATFORM_ID` is a single constant in `__init__.py`.

**The override is itself deferred.** `register()` installs a loader and imports
nothing heavy; the built-in adapter is imported the first time something asks
the registry for the Discord platform (gateway start, cron delivery,
`hermes setup`, `send_message`). This matters because user platform plugins are
loaded *eagerly* during plugin discovery, and discovery runs on nearly every
`hermes` invocation — importing the Discord adapter there would add 1–2s to
plain `hermes chat`.

---

## Hermes compatibility

This plugin subclasses a core adapter, so it depends on Hermes internals that
are not public API. `compat.py` classifies every borrowed symbol at load time
and the plugin behaves predictably at both ends of the range:

| Situation | Behaviour |
|---|---|
| A **required** seam is missing (e.g. `_discord_message_admission`) | One `ERROR` log naming the exact symbols, then the **built-in Discord adapter is registered instead**. Discord keeps working; this feature is off. |
| An **optional** seam is missing (`_gate_raw`, `_get_allow_bots`, `_snapshot_gate_env`, the channel-gate helpers) | One `WARNING` naming them; the feature keeps its full behaviour on a documented fallback that mirrors core's own precedence. |
| The built-in YAML bridge is missing or raises | Loud failure. The override is declined (or the bridge re-raises) rather than applying a *partial* Discord config — losing `require_mention`, `auto_thread`, channel lists, reactions and backfill silently would be worse than not installing. |
| `discord.py` is not installed yet | Not fatal. The plugin never imports `discord` at module scope, and the inherited dependency hook installs it before the gateway builds an adapter — `ensure_deps_fn=check_discord_requirements` on builds that split the hook, `check_fn=check_discord_requirements` on older ones. Both are inherited verbatim from the built-in entry, so whichever this Hermes has is what runs. |

The gate-env snapshot is the one place where behaviour genuinely differs
between builds. On a core that has it, the plugin extends the per-adapter
snapshot so `thread_ownership_transfer` is captured inside the owning profile's
runtime scope at `connect()` — the same footing as every other Discord gate,
which is what makes it safe under `gateway.multiplex_profiles`. On a core
without it, the flag is read live from the environment, exactly like every
other Discord gate on that build; precedence (and therefore the env kill switch)
is unchanged, and per-profile `config.yaml` opt-in still works because that path
goes through the per-adapter `config.extra`.

The YAML bridge never writes `DISCORD_THREAD_OWNERSHIP_TRANSFER` into the
process environment — that would make one profile's opt-in another profile's
policy.

---

## Operational notes

- **Text commands only reach the thread's owner.** `/help`, `/status`, `/new`
  and friends typed as messages arrive through the same admission path, so a
  non-owner bot will not see them inside an owned thread. @mentioning that bot
  to reach it *transfers ownership to it* as a side effect — that is the feature
  working as designed. Two ways around it: use Discord **slash commands**, which
  go through a separate authorization path and are unaffected; or address the
  bot outside the thread, where ownership does not apply.
- **`thread_require_mention` becomes unnecessary.** Leave it at its default
  (`false`); ownership does the gating. Setting it `true` alongside this feature
  makes the owner also require a mention on every message, which defeats the
  point.
- **An owner that goes offline parks its threads.** Nobody answers unmentioned
  follow-ups there until a human @mentions any bot, which re-claims instantly.
- **The marker stays in the text the agent sees.** The adapter strips only its
  own mention, as before.
- **Ownership is capped at 500 threads per profile**, oldest evicted first. An
  evicted thread falls back to built-in behaviour until the next mention
  re-establishes an owner. Threads in ignored or non-allowlisted channels are
  never recorded, so they cannot consume that budget.
- **Redelivered and backfilled messages never move ownership.** Discord can
  resurface a message two ways — an exact redelivery after a gateway `RESUME`,
  or missed-message backfill surfacing an older message for the first time. The
  first is caught by the adapter's dedup cache plus a per-thread set of applied
  message ids; the second by Discord's time-ordered snowflake ids, which let the
  store reject any ownership event older than the one already applied.

### Remaining constraints

- **One writer per profile.** The store re-reads and merges immediately before
  each write, so a second process on the same profile will not delete your
  threads — but two processes deciding on the *same* thread at the same instant
  is still last-writer-wins. Run one gateway per profile.
- **State writes never block the Discord event loop.** The durable write is
  temp-file + `fsync` + `os.replace`, reached from `on_message`, so when a loop
  is running the payload is handed to a single background writer thread per
  store. Ordering is preserved (one thread, FIFO). With no loop — CLI, cron,
  tests — the write stays inline, so "returned means persisted" still holds
  there. If the thread cannot start or its queue backs up, the caller falls back
  to writing inline rather than dropping the write.
- **A hard kill can lose the last ownership change.** Queued writes are drained
  on adapter `disconnect()` and at interpreter exit, so `hermes gateway restart`
  is safe. A `SIGKILL` mid-write can still lose the most recent claim/transfer,
  which self-heals the next time someone mentions a bot in that thread.
- **Editing the state file under a running gateway has no effect** until
  restart: the store loads once at construction and only merges other writers'
  threads, never re-reading its own.

---

## Repository layout

| Path | Role |
|---|---|
| `plugin.yaml` | Manifest. `name` is the install directory and the `plugins enable` key. |
| `__init__.py` | `register(ctx)` — inherits the built-in `PlatformEntry`, swaps two callables. |
| `adapter.py` | `DiscordCustomAdapter(DiscordAdapter)` — overrides admission and the bot gate only. |
| `ownership.py` | Pure decision engine + per-profile persistent store. No `discord` import. |
| `config_bridge.py` | Calls the built-in YAML bridge, then adds `thread_ownership_transfer`. |
| `compat.py` | Classifies the borrowed core internals; fallbacks and the loud failure path. |
| `tests/` | The suite below. |

## Development

The suite runs the real Hermes plugin loader, platform registry and Discord
adapter, so it needs a Hermes checkout. It is found automatically at
`$HERMES_HOME/hermes-agent` or `~/.hermes/hermes-agent`; override with
`HERMES_SOURCE_ROOT`.

```bash
./run-tests.sh                  # or: cd tests && pytest
ruff check .
```

`run-tests.sh` runs pytest from `tests/` on purpose: the repository root is the
plugin package, so pytest must not collect it as a test package.

Tests that exercise a seam the local Hermes build does not have are skipped with
a reason rather than failing — the compatibility matrix above is itself covered
by `tests/test_compat.py`.

## License

MIT — see [LICENSE](LICENSE).
