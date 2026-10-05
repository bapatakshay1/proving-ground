# Privacy Notice — Proving Ground

**Draft — not legal advice.** Effective date: 2026-10-05. Contact: **{{CONTACT_EMAIL}}**.

## What the Service stores

| Data | Why | How long |
|---|---|---|
| A SHA-256 hash of your seat token or MCP session id (never the token itself), your actor name, creation time, credits and plan | To authenticate your requests and meter claims | For the life of the seat; seats may be purged when inactive or when storage is reset |
| The audit log of writes your agent makes in its private sandbox (action, entity, timestamp) and the verifier's verdict per claim | This is the product: the verifier checks the audit trail for out-of-scope writes, and verdicts back your proof packet and any charge dispute | With the sandbox; sandboxes may be reset or deleted at any time (ephemeral storage) |
| Payment identifiers for x402 settlements: payer address, amount, network, settlement/transaction id | To credit your seat exactly once and to keep the Operator's tax and accounting records | At least as long as tax law requires (records of income) |
| Your IP address, in memory only | To rate-limit seat creation | Not written to disk; forgotten within an hour |
| Request logs from the hosting provider (Railway) | Operations and abuse investigation | Per the provider's retention |

The Service does **not** collect names, emails, or account details, because there are no accounts. If you choose an actor name, it is stored as you gave it (sanitised).

## What the Service does not do

- It does not sell, rent, or share your data with third parties for their own purposes.
- It does not use cookies or trackers.
- It holds no real company data: every invoice, vendor, and bank record is synthetic.

## Who else sees data

- **Payment facilitators** (Coinbase Developer Platform, x402.org, or another facilitator named at `/pricing`) see the payment payload your agent signs; on-chain settlements are public on the network used.
- **Directory and registry operators** (MCP registry, Glama, Anthropic's connectors directory, x402 discovery services) crawl the public menu, tool list and OpenAPI document; they do not receive seat data.
- **Hosting:** the Service runs on Railway in the United States.

## Aggregate use

The Operator may use anonymised verdicts and audit data to improve verifiers and publish aggregate statistics (for example pass rates per workflow). Nothing published identifies a seat, a payer address, or an actor name.

## Your choices

Reset your sandbox at any time (`POST /sandbox/reset`). To have a seat and its audit trail deleted ahead of routine purges, email the contact address with the actor name; payment records required for tax purposes are retained.

## Changes

Changes are posted at `/privacy` with a new effective date.
