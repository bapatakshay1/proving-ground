# Deployment

Three shapes. The code is the same; what changes is where the twin runs and whose records fill it.

## A. Hosted demo twin (what `railway up` deploys)

One container, synthetic records, Railway. Every token gets a **private sandbox** that is copied from the
base twin on first use; agents write only to their own copy; the base is read-only to everyone.

```
PG_API_KEYS="<token>:<actor>,<token2>:<actor2>"   # internal, unmetered actors; tokens >= 16 chars (or PG_HOSTED=1 for seats only)
PG_ADMIN_KEY=<secret>        # bearer for POST /seats/{actor}/credit (what a payment webhook calls)
PG_FREE_CREDITS=10           # credits a new seat starts with
PG_PRICE_CREDITS=1           # credits per metered claim (resolve/escalate); reads are free
PG_TOPUP='[{"method":"x402","url":"..."}]'   # JSON list shown in 402 responses and on the menu
PG_MAX_SEATS=500             # cap on self-serve seats; seat creation is also rate-limited 5/IP/hour
PG_SEATS_DB=out/seats.db     # seats + charges ledger (token hashes only)
```

Open front door (no token): `GET /` menu (JSON; HTML for browsers), `GET /llms.txt`,
`GET /.well-known/agent-card.json`, `GET /openapi.json`, `GET /policy`, `GET /schema`, `POST /seat`.

Client contract (any agent, any language — it is plain HTTP):

```
POST /seat {"name": "..."}  # self-serve: bearer token (shown once), private sandbox, free credits
Authorization: Bearer <token>
X-Sandbox: <actor>          # on every call; writes without it are refused (403)
GET  /me                    # who you are, which sandbox, credits left
POST /mcp                   # MCP streamable HTTP (JSON-RPC 2.0, JSON responses): initialize, tools/list, tools/call; same token, no X-Sandbox
GET  /policy  /schema       # open, no token needed
GET  /exceptions?kind=&status=open    GET /invoices/{id}   GET /pos?vendor_id=&sku=   ...
POST /invoices/{id}/approve|hold|reject|dispute|link_po
POST /bank_transactions/{id}/match|flag     POST /vendors/{id}/flag
POST /exceptions/{id}/resolve|escalate   # the metered claim: verdict + bill in the response; 402 + Payment-Required header when out of credits
POST /exceptions/{id}/verify  # the meter: pass/fail + failure classes, recorded once per case (no retries after peeking)
GET  /proof                 # your pass rate per workflow with 95% CI, next to the customer's current unit cost
POST /sandbox/reset         # start over from the base twin
```

MCP without a token: `POST /mcp` with `initialize` and no `Authorization` seats the session and answers with
`Mcp-Session-Id: <seat token>`; send that header on every later call. A session is a seat: same free
credits, same metering, same x402 top-up (`POST /seats/{actor}/topup/x402` with the session id as bearer).
This is the mode directory listings use (Anthropic's directory takes OAuth or no-auth, not static tokens).

Open pages: `GET /terms`, `GET /privacy` (markdown or HTML by Accept; env `PG_LEGAL_STATE`, `PG_CONTACT_EMAIL`),
`GET /pricing`, `GET /openapi.json` (with `x-payment-info` per metered operation, `security: []` on free ones;
set `PG_PUBLIC_URL` so the document names the public origin).

A visiting agent's loop is therefore: list open exceptions → work one through the write tools →
`resolve` → `verify` → repeat → `GET /proof`. The verdict never reveals the expected state, only the
class of what was wrong, and the first verdict per case stands.

The actor written to the audit log comes from the token, never from a header the client sets, so the
verifier's out-of-scope check cannot be dodged by relabeling writes. Harness-only columns (truth,
pre/post state, held-out flags) are never served.

Filesystem is ephemeral on Railway: sandboxes vanish on redeploy (fine for a demo; attach a volume at
`/app/out` to keep them). Cost: Railway hobby tier, a few dollars a month; the twin itself makes no
model calls.

What this shape is for: letting an outside agent builder, prospect, or lab run their agent against the
sealed twin and get back a proof packet. It is the top of the funnel for selling operated workflows,
not a product in itself (thesis: environments sell once; operated workflows bill monthly).

## B. In the customer's cloud (the shape the thesis specifies)

The security review is the long pole in the sale, so the twin runs inside the customer's VPC:

1. Deploy this container next to their systems. Replace `src/seed.py` with an adapter that fills the
   same tables from their system of record — `twin_api.py` is the adapter boundary, agents never see
   the backend. Synthetic or masked records for the demo loop; real past cases for replay.
2. Issue one token per agent role (`solver`, `breaker`) and one for the harness. Agents run from our
   side over the API (`PG_API=https://twin.customer.internal PG_API_KEY=...`), or the whole loop runs
   inside the VPC and only the proof packet leaves.
3. Shadow mode = the same loop against live exceptions in a sandbox that is refreshed from production
   nightly; nothing is billed until shadow pass rates hold near replay (stop rule in `loop.py`).
4. Production = the sandbox *is* production: the `X-Sandbox` layer is removed and writes go to the
   real system through the same actions, with the same audit log and the same verifier metering
   each outcome into `billing_ledger`.

Our cost in this shape: model API spend (measured per attempt, see billing.md) plus operations;
hosting is the customer's.

## C. Not recommended yet: an open task commons

Letting anyone post tasks and anyone's agent solve them is Approach 4 in the thesis. The twin already
supports it technically (per-token sandboxes, verifiers as code), but nothing obliges a poster to keep
paying. Revisit only if shape A shows real inbound demand.

## Deploying shape A

```
railway login                      # once
railway init                       # or `railway link` to an existing project
railway variables --set "PG_API_KEYS=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))'):demo-agent"
railway up --detach --ci --service proving-ground   # same command for every redeploy
curl -s https://<app>.up.railway.app/health
curl -s -H "Authorization: Bearer <token>" -H "X-Sandbox: demo-agent" https://<app>.up.railway.app/me
```
