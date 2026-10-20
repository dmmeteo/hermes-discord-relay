# Discord Relay

Editorial content source for later integration into the owner's existing site.
This file does not publish a page. The canonical repository link below is
prepared for the authorized rename and remains pending publication.

## Page metadata

- Title: Discord Relay for Hermes
- Description: Give a Hermes bot ownership of a Discord conversation. Let an explicitly trusted bot pass one request into an allowed existing thread.
- Proposed page path: `/things/discord-relay/`
- Primary link after publication: [View source on GitHub](https://github.com/dmmeteo/hermes-discord-relay)
- Documentation: [Receiver and sender guide](../README.md#configure-a-receiver)

## Hero

One bot owns the conversation. A trusted bot can pass it one request.

Discord Relay is an opt-in Hermes plugin for teams that run several bots in
Discord. A human mention chooses who handles a thread. A separate relay rule
lets an explicitly trusted sender submit one request to an allowed existing
thread, with the sender's bot identity intact.

## Keep follow-ups with one owner

Mention Builder in a guild thread. Builder handles the unmentioned human
follow-ups while the other participating bots stay silent. Mention Scout to
transfer the conversation to Scout. An authorized owner bot can also use a
guarded handoff to transfer ownership.

This ownership belongs to Hermes' conversation rules. Discord's native thread
owner does not change.

## Pass work to the current owner

Maya asks Scout to send Builder a question. Builder already owns the destination
thread. Its profile trusts Scout's exact bot ID for that exact guild and thread,
and native Hermes authorization allows Scout. Scout sends one bounded request
using Hermes' existing Discord send tool, reports the send status, and stops.
Builder receives the request as bot-authored work and reports its result or
blocker directly to Maya in that thread.

Relay leaves ownership unchanged. Allowing Scout to send to Builder does not
allow Builder to send back. Unknown senders and unlisted destinations are denied.

## Know what is enforced

The receiver checks exact routes, bot identity, destination, freshness, and a
single-hop envelope. Durable admission receipts reject duplicate requests
across restart. Self messages, webhooks, forwarded messages, nested relays, and
ordinary bot pings cannot become relay requests.

The sender's stop instruction and the recipient's direct human report guide the
models. They do not restrict outbound tools. Relay preserves native human
authorization and approval requirements. Payload text cannot grant human consent.

A successful send means Discord stored a message. It does not prove the
recipient admitted, executed, or completed the request. Receipts prove local
admission at most once. A crash after reservation can lose work before dispatch.

## Reuse your existing Hermes bot

Both capabilities are off by default and configured separately per profile.
Discord Relay inherits the native Discord connection and tools. It creates no
new Discord bot, makes no Discord API calls of its own, and does not synchronize
servers or channels.

Version 1.1.0 is a local source candidate. Public rename, release, site
publication, installation into live profiles, and live guild acceptance remain
pending. Existing installs have a reversible identity migration recipe that
preserves their Discord configuration and per-profile stores.

## Integration locations observed read-only

The existing site is `dmmeteo/bullshitai.dev`, with local checkout
`/home/me/projects/dmmeteo.github.io`. Its `site/data/systems.yaml` currently says
"Discord thread ownership" in the Hermes plugins blurb. No dedicated Discord
ownership or relay page was found in the remote tree examined for this source
checkpoint. The likely integration follows `site/content/things/`, alongside
existing Hermes pages. `/things/discord-relay/` is a proposed path, not a live URL.

The other candidate, `dmmeteo/dimaforcepush-site`, exposes repository metadata
and a `main` branch, but its root contents response is empty and its recursive
`main` tree request returns 404. It supplied no existing page to update.
Neither site repository was edited.
