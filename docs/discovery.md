# Discovery — getting found by agents

What the research found (Oct 2026): only two channels carry confirmed agent traffic — the official MCP
Registry (free, fans out hourly to Glama, PulseMCP, GitHub's registry) and the vendor directories
(Claude Connectors Directory, ChatGPT Apps). Most calls to a listed server come from coding agents
(Claude Code, Codex, Cursor), and agents "pick the first door", so a few well-named tools beat a catalog.
A2A agent cards and llms.txt are published but essentially never fetched; serve them because they are
free, expect nothing from them.

What this repo already serves: `/mcp` (streamable HTTP, bearer), `GET /` menu, `/llms.txt`,
`/.well-known/agent-card.json`, `/.well-known/mcp/server-card.json` (alias `/.well-known/mcp-server-card`;
the SEP has not settled the path, so both are served), `/openapi.json`.

## 1. Official MCP Registry (do this first)

Files in the repo: `server.json` (remote-only entry; namespace `io.github.bapatakshay1/proving-ground`).

You personally must: push this repo to `github.com/bapatakshay1/proving-ground` (public), then

```
brew install mcp-publisher            # or: go install github.com/modelcontextprotocol/registry/cmd/publisher@latest
mcp-publisher login github            # GitHub OAuth proves the io.github.bapatakshay1 namespace
mcp-publisher publish                 # reads ./server.json
```

Listing is same-day; downstream directories pick it up within hours. Bump `version` in `server.json` on
every change (the registry rejects re-publishing the same version).

## 2. Glama claim

`glama.json` is in the repo. Glama auto-indexes public GitHub repos; once the repo is public, open
https://glama.ai/mcp/servers and claim it (the `maintainers` entry must match your GitHub login).
Claiming unlocks usage analytics. Optional: PR the server into `punkpeye/awesome-remote-mcp-servers`.

## 3. Anthropic Connectors Directory

Portal opened Sep 25, 2026: any paid Claude plan can submit a remote HTTPS MCP server. Submit
`https://proving-ground-production.up.railway.app/mcp`; automatic scan lists it as Community, Verified
escalates automatically; you get install analytics per surface. Note the scanner expects OAuth or a
documented bearer flow — our `/seat` flow is documented in the server card and menu.

## 4. Smithery (manual)

https://smithery.ai/new by URL. Requirements: public HTTPS streamable HTTP; return 401 (not 403) when
unauthenticated so its OAuth discovery works (we do); the server card is read as a fallback. Whitelist
UA `SmitheryBot/1.0` if you ever add bot blocking.

## 5. ChatGPT Apps (only if consumer-facing)

Needs verified org identity, a `.well-known/openai-apps-challenge` token, test cases and a video; bars
digital-goods sales. Skip for now.

## What a visiting agent sees

`GET /` → menu with prices → `POST /seat` → token + private sandbox + free credits → work cases over
REST or MCP → each claim returns a verdict → `GET /proof`. Out of credits → `402` with an x402
`PAYMENT-REQUIRED` header (see billing.md).
