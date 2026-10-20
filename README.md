# Discord Relay

A [Hermes](https://github.com/NousResearch/hermes-agent) Discord plugin for
controlled bot requests and single-owner threads. A trusted sender sends one
explicit request to an existing thread. The recipient works there and reports
to the human, without starting a bot conversation.

Both features are opt-in per profile. Relay is configured independently under
`discord.relay`. Existing `discord.thread_ownership_transfer` configuration and
the install name `hermes-discord-thread-ownership` remain compatible.

The plugin subclasses the built-in Discord adapter. Connection, voice, slash
commands, media, reactions, thread transport, and cron delivery remain native.
Relay adds a narrow ingress policy; it preserves bot identity and downstream
Hermes authorization. It makes no Discord API calls itself and does not change
Discord's thread owner.

See [Configure a receiver](#configure-a-receiver) and
[Send once to an existing thread](#send-once-to-an-existing-thread). For ownership
alone, the existing setting still works:

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

When `thread_ownership_transfer` is enabled, inside a **guild thread**:

| Event | Result |
|---|---|
| Human @mentions bot **B** | B owns the thread |
| Human posts with no bot mention | Only the owner answers |
| Human @mentions bot **C** | C owns it; B goes silent immediately |
| Human @mentions a *human* | Ownership unchanged |
| Human @mentions several bots | First mention in the message wins; the rest stay silent |
| A non-owner bot posts anything | Ownership unchanged — bots cannot claim or steal |
| The **owner** posts `[[handoff]] @C` | C owns it; the owner goes silent (agent-to-agent handoff) |

Ownership leaves DMs, plain channels, and every non-thread path untouched.
Relay accepts requests only inside explicitly allowed guild threads.

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

### Local source candidate

The 1.1.0 source candidate adds Discord Relay. Native-adapter, lifecycle,
security, and backward-compatibility checks are automated; no live guild
acceptance, installation, publication, or deployment is claimed. See the
[release and migration notes](docs/releases/1.1.0.md).

The previous registration baseline follows for historical context.

Registration source candidate `b2611f83e59c6b82332280fe8c80859d9391f949` on
`prime/audit-1015-discord` reconciles public fix `4547de4` with this branch's
existing behavior and documentation. Its full suite passes on stock Hermes
v0.22.0 (`e06df9c2eae6901f4c88f464e0ae6e753c0428b8`) with Python 3.14.7.
This is a local candidate, not a new public release or a deployment. Live guild
acceptance remains pending in the [outcome checklist](planning/next-epics.md).

The source rollback target is `d02ad1a607ec719bfa63f8ecc9bc06913ca26d10`.
It restores the known deferred-registration failure on v0.22.0. For a later
authorized rollout, disabling the plugin restores stock Discord on the next
gateway start, as described under [Update and rollback](#update-and-rollback).
No runtime rollback is needed for this source-only task.

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

Relay and ownership have separate opt-ins. Existing ownership-only profiles
need no migration. Keep the native bot gate at `none` for relay-only bot ingress.

### Configure a receiver

Use exact ID strings, never names or wildcards. The examples use generic IDs;
replace them with the sender bot, guild, and existing destination thread IDs.
Configure each receiving profile separately. Allowing A to send to B does not
allow B to send to A.

```yaml
discord:
  allow_bots: none
  thread_require_mention: false  # owner can accept unmentioned human follow-ups
  relay:
    enabled: true
    allow:
      - sender_id: "2000"
        guild_id: "4242"
        thread_id: "77"
    deny: []
  thread_ownership_transfer:
    enabled: true
    bot_handoff: off
```

The relay route is an additional restriction, **not a human authorization
grant**. The sender must pass both the native adapter policy and the gateway's live
authorization callback before a receipt is reserved. Missing or unknown callback
authorization denies relay. For an ID allowlist, include
the exact sender bot ID alongside the humans in `DISCORD_ALLOWED_USERS` on the
receiver. Keep `DISCORD_ALLOW_BOTS=none`. Do not set open access or a wildcard to
make relay work. With native bot mode `none`, role-only and channel-only grants fail the gateway
bot check and are refused
before consuming a receipt. A genuine pairing approval or user-ID grant can pass
both checks. The plugin always forwards `role_authorized=False` for relay.
Authorization is checked again downstream and can change after reservation;
local admission still does not prove an agent turn started.

| Key | Policy |
|---|---|
| `relay.enabled` | Boolean, default `false`. `relay: false` also disables it. A mapping alone does not enable relay. |
| `relay.allow` | At most 256 exact `{sender_id, guild_id, thread_id}` routes. Default empty means deny. |
| `relay.deny` | Same shape and limit. An exact deny wins over an allow. |

Unknown keys, numeric IDs, wildcards, partial routes, and malformed values are
configuration errors. The native YAML bridge always applies its settings first.
The raw relay block then reaches runtime validation. A malformed relay block
logs an error and denies all bot ingress while native human authorization,
allowed/ignored channels, and mention settings remain active. No relay
environment variable enables or widens this policy. Native allowed and ignored channel gates still apply.

With relay enabled, ordinary bot output, mention pings, debounce continuation
chunks, and bot DMs are denied even with native `allow_bots: all` or `mentions`.
This opt-in narrowing is intentional loop protection. A separately validated
owner handoff can still
pass when ownership handoff is enabled. Set `bot_handoff: off` as above to allow
only relay requests. With relay disabled, native bot admission is preserved.
Bot text containing `[[relay` cannot trigger mention-only ownership transfer.
An explicit handoff discussing that literal token must begin with the configured
handoff marker. Mention or quote prefixes remain reserved for relay text even
if configured as handoff markers. Relay-enabled adapters observe that handoff's
ownership change but refuse its model admission because the relay text is invalid.

### Send once to an existing thread

Before sending, keep the sender's native bot mode at `none`, or enable relay
mode there too. A sender using ordinary `all` or `mentions` ingress can react to
the receiver's reply and start a bot conversation. Stop and no-redelegation
instructions guide the models; they are not hard outbound guarantees.

1. Identify the existing thread from the human's message link or the native
   channel directory. A Discord message link contains guild, thread, and message
   IDs. If needed, use native `discord_admin` `channel_info` with the thread ID
   and `discord` `fetch_messages` to confirm its context. Use only actions your
   profile already permits. When receiver ownership is enabled, target a thread
   the receiver already owns. Keep `thread_require_mention: false` for
   unmentioned human continuity. The receiver refuses relay to an unowned thread
   or another bot's thread without changing ownership. Have the human address
   the receiver first if ownership must change. With ownership off, native
   mention rules still apply and multiple participating bots may answer
   unmentioned follow-ups. If the destination cannot be verified, report a
   blocker to the human and stop. Do not create another thread or guess by name.
2. Confirm the receiver permits your bot ID for that exact guild and thread.
   Generate a fresh lowercase UUID hex request ID and an expiry at most 300
   seconds ahead as measured by the receiver. Use a 120-second TTL as shown
   below, or at most 270 seconds to tolerate the full 30-second clock skew. A
   300-second TTL can be refused even for a fractionally slower receiver clock.
   Send within that window.
3. Use the existing `send_message` tool once, with `target: "discord:77"` for
   this example's thread, and the exact text format below. Use one message with
   no attachments, reply reference, forwarded snapshot, or extra mention.
4. Report the transport result to the human and stop. Do not poll the receiver,
   retry after an uncertain send, send a follow-up, monitor the thread, or
   redelegate. A human can resolve an uncertain outcome before a new request.

```text
<@1000> [[relay:v1 id=0123456789abcdef0123456789abcdef guild=4242 thread=77 expires=1800000120 hop=1]]
Summarize the task for the human in this thread.
```

Replace `1000` with the recipient's exact bot ID. The expiry above is illustrative,
not a reusable timestamp. Generate a request ID with `uuid.uuid4().hex` and expiry
with `int(time.time()) + 120`. The header must start at the first character, use
exactly the shown field order and spacing, and end with one newline before the
payload. Only `<@ID>` is accepted, not a reply ping or `<@!ID>`.

The total text limit is 2,000 characters and the payload limit is 1,400. The
payload must be nonempty and cannot contain additional user or role mentions,
`@everyone`, `@here`, nested relay envelopes, or handoff markers. Only native
bot-authored default messages from the last 300 seconds are eligible, with at
most 30 seconds of future clock skew. Webhooks, self messages, replies,
forwarded snapshots, attachments, other recipients, mismatched destinations,
stale messages, and hop values other than `1` are denied.

### Recipient behavior and delivery status

The recipient sees the real sender bot ID, guild, thread, message ID, request ID,
and a provenance notice before the payload. Native source identity remains a
bot. Text claiming a human identity, consent, or approval authenticates none of
those claims. Normal tool permissions and human approval requirements still
apply. The notice directs the recipient to report its result or blocker to the
human in that thread, without replying to the sender, forwarding, redelegating,
or monitoring for bot follow-ups. These are recipient instructions, not a
sandbox that can constrain a model or another plugin's outbound tools.

There is no admission acknowledgement on a supported bounded native seam, so
this plugin sends none. A successful native send proves only that Discord
stored the message. Report **"Sent to Discord; recipient admission unconfirmed;
human result pending."** Do not report accepted, delivered to the agent, or
completed from a send result. The receiver's `Discord relay admitted request`
log records a durable local admission, not downstream gateway acceptance or
completion. The human-facing result is the recipient's later report.

Relay uses native thread sessions and busy-input policy. With default shared
thread sessions, a relay and a human follow-up use the same session. A request
can interrupt or redirect a human's active run, or queue/steer when configured.
`thread_sessions_per_user: true` separates bot and human sessions. No receipt
proves execution, scheduling, or that a human run was left undisturbed.

### Relay persistence and failure behavior

Receipts live per profile in `$HERMES_HOME/discord_relay_receipts.sqlite3`.
One synchronous SQLite transaction commits before local admission returns.
Request IDs are unique per recipient across senders, and Discord message IDs are
unique across that profile's receipts. Duplicate requests are denied across
adapter recreation, plugin reload, and gateway restart. Concurrent connections
on the same profile cannot reserve the same request twice. This concurrency
guarantee applies to relay receipts; ownership still has its one-writer limit.

The store keeps at most 4,096 receipts for 24 hours. Capacity is shared by all
recipients using that profile database; a trusted sender can exhaust it. At capacity, it denies new
requests instead of evicting replay protection. Expired receipts are removed on
the next reservation. The record stores IDs and retention time, not the payload.
Never reuse request IDs. After retention expires, the same textual request ID
is no longer protected, but an original stale Discord message is still denied.
Disk errors, corruption, or lock contention deny admission without resetting
the database. SQLite's lock wait is 250ms; filesystem commit latency can also
pause ingress. There is no background receipt writer or open connection to drain.

This is **at-most-once admission**, not exactly-once execution. A crash after the
receipt commit can lose the task before the gateway accepts it. No automatic
retry or model-generated acknowledgement repairs that gap. Keep receipts during
updates and rollback. Deleting them removes replay protection for fresh messages.

### Ownership configuration

The original setting remains under the existing `discord:` block:

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

This section describes ownership handoff with relay disabled. Enabling
`discord.relay` adds the separate exact-sender, exact-destination policy above.
`bot_handoff: off` does not turn relay off. An ownership env switch never enables
relay or resets its routes. Disable both features to restore stock ingress.

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
| YAML bridge | **overridden** → relay validation, built-in bridge, then both opt-ins |

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

The receiving bot is admitted even though `DISCORD_ALLOW_BOTS` is `none`.
This is the ownership feature's force-admission path. Relay has a separate
explicit opt-in and never changes ownership.

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

Registering over `discord` is a supported registry operation. The plugin calls
`ctx.register_platform()` during `register()`, so Hermes records the override in
the owning profile's lifecycle ledger. Unloading the plugin restores the entry
or deferred loader it displaced. The platform ID remains `discord`.

A plugin-managed deferred loader can sit outside that ledger and lose to the
profile's bundled Discord entry. The override therefore materializes during
`register()`; Hermes controls when the platform plugin itself loads. See the
[registration decision](docs/adr/0001-plugin-scoped-registration.md) for the
historical evidence and tradeoff.

---

## Hermes compatibility

This plugin subclasses a core adapter, so it depends on Hermes internals that
are not public API. `compat.py` classifies adapter seams at load time. Relay
checks its additional capabilities before reserving a receipt:

| Situation | Behaviour |
|---|---|
| A **required** seam is missing (e.g. `_discord_message_admission`) | One `ERROR` log naming the exact symbols, then the **built-in Discord adapter is registered instead**. Discord keeps working; this feature is off. |
| An **optional** seam is missing (`_gate_raw`, `_get_allow_bots`, `_snapshot_gate_env`, the channel-gate helpers) | One `WARNING` naming them; the feature keeps its full behaviour on a documented fallback that mirrors core's own precedence. |
| The built-in YAML bridge is missing or raises | Loud failure. The override is declined (or the bridge re-raises) rather than applying a *partial* Discord config — losing `require_mention`, `auto_thread`, channel lists, reactions and backfill silently would be worse than not installing. |
| Relay bot hook, recovered dispatch, or live gateway sender authorization is missing | Relay is refused before a receipt. Ownership keeps its existing fallback behavior. |
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
| `adapter.py` | Native adapter subclass with ownership/relay admission, scoped bot gate, and relay provenance on dispatch. |
| `ownership.py` | Pure decision engine + per-profile persistent store. No `discord` import. |
| `relay.py` | Strict relay config, envelope, provenance text, and bounded durable SQLite receipts. |
| `config_bridge.py` | Validates relay, calls the built-in YAML bridge, then adds both opt-ins. |
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

The current stock Hermes gate runs the complete suite, including legacy gate
fallbacks exercised with those native helpers removed. Older builds without the
modern snapshot seam cannot run snapshot tests. Relay requires the native
per-adapter bot hook, live sender authorization callback, and
`_handle_message(..., recovered=...)`; without these capabilities,
relay refuses admission and logs the missing capability. Ownership retains its
older compatibility fallbacks. No relay path mutates process-global bot policy.

`tests/test_relay_adapter.py` covers actual native admission and dispatch,
including a real `discord.Message`, sender provenance, gateway authorization,
negative security cases, profile isolation, restart receipts, and concurrency.
`tests/test_relay_lifecycle.py` exercises the selected factory through stock
load/unload/reload and old configuration. These offline tests make no live guild
or model-completion claim.

## License

MIT — see [LICENSE](LICENSE).

## For agents

Coding agents start at [AGENTS.md](AGENTS.md).
