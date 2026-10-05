# Billing — how the customer is billed, how we get billed

## The meter

A billable event is one row in `billing_ledger` with `outcome='verified'`: the solver claimed the
exception done **and** the code verifier (`policy.py`) confirmed the system of record is in the state
the policy requires, with no writes outside the task's scope. The row carries the price, the verifier's
expected/actual state, and the model spend for that attempt. Anything else — agent escalated, ran out of
steps, or claimed done and the verifier disagreed — is `routed_to_human` at $0.00 and the exception is
handed back to the customer's queue.

The verifier the customer inspected at admission is the contract's definition of done. A dispute is
settled by replaying that one case against it; `python -m src.cli statement` prints the evidence hash
per billed line.

## What the customer pays

| Line | Basis | Where it comes from |
|---|---|---|
| Discovery run | Fixed fee, credited against first-year outcome fees | `--discovery-credit` on the statement; not revenue |
| Operated workflow | Per verified outcome, priced at 50% of their measured unit cost | `pricing.json` → `price_per_outcome_usd` |
| Annual minimum | Committed verified outcomes per live workflow (80% × volume × lower CI of pass rate) | `proposed_annual_minimum_outcomes`; statement shows YTD progress; shortfall trued up at year end |

From the Oct 5 run on the synthetic customer (55 exceptions/month per kind):

| Workflow | Their cost today | Our price | Verified rate | Their monthly bill | Their saving |
|---|---|---|---|---|---|
| missing_po | $10.45 | $5.22 | 90% | $258 | $259 |
| quantity_mismatch | $9.16 | $4.58 | 100% | $252 | $252 |
| price_mismatch | $6.35 | $3.17 | 95% | $166 | $166 |
| possible_duplicate | $4.03 | $2.02 | 100% (gpt-4.1) | $111 | $111 |
| unmatched_payment | $11.45 | $5.72 | 60% — not offered until it clears 90% | — | — |

## What we pay

| Cost | Measured / assumed | Per verified outcome |
|---|---|---|
| Model API (OpenRouter, every attempt whether or not it bills) | measured: $0.005–0.036 per attempt by tier | attempt cost ÷ pass rate, e.g. $0.0050–0.026 |
| Overhead on model spend (retries, breaker re-runs) | +25% (assumption) | |
| Operations: twin hosting, verifier upkeep, audit sample, support | $0.25 (assumption, replace with real) | |
| Hosting | Railway hobby for the demo; customer's cloud in production | ≈ 0 |

Gross margin on the run: 86–95% per workflow at these prices. The thesis assumed $0.90 cost to serve;
measured model cost is two orders of magnitude lower, so the operations allowance dominates — that
number is the one to replace with real data from the first design partner.

## Mechanics

Monthly: `python -m src.cli statement --db <twin> --period YYYY-MM [--discovery-credit N]` → `out/statement_<period>.{md,json}`.
The `.md` is the customer statement with the evidence appendix; `.json` feeds invoicing.

Stripe mapping (when a payments account exists): one metered price per live workflow (`price_per_outcome_usd`);
report one usage event per `verified` ledger row with `exception_id` as the idempotency key; invoice
monthly; the discovery fee becomes a customer credit balance; the annual minimum is a scheduled
true-up invoice. Nothing in the loop depends on Stripe — the ledger is the system of record for billing.

Model bills: OpenRouter invoices us; every call's real cost is captured (`llm.COST`, per-case `usd`) and
flows into the ledger, so our cost of goods per customer is reconcilable line by line.

## x402 top-up (the agent-native rail)

Research (Oct 2026): x402 — HTTP 402 + USDC, Coinbase-originated, now governed by an x402 Foundation
under the Linux Foundation (Visa, Mastercard, Stripe, Google, AWS among the Premier members) — is the
one rail where a seller can gate an endpoint and receive money from a machine with no buyer-side human
and no seller KYC beyond a CDP API key. CDP facilitator fees: first 1,000 settlements/month free, then
$0.001 each; verification is free; no percentage fee; no chargebacks; USDC does not move in value.
Independent analysis (CoinDesk, Mar 2026) put real volume at ~$28k/day with ~half of activity gamed,
so expect early payers to be developers testing, not agents with budgets.

**Flow (V2, `exact` scheme, USDC on Base):**
1. A metered claim with no credits → `402` with `PAYMENT-REQUIRED: <base64 PaymentRequired>`; the JSON
   body also carries it under `x402` for humans. `GET /pricing` shows the same requirements.
2. The agent's wallet signs an EIP-3009 transfer for `amount` to `payTo` and retries with
   `PAYMENT-SIGNATURE: <base64 PaymentPayload>` — on the claim itself, or on
   `POST /seats/<actor>/topup/x402` to buy credits ahead (any multiple of the per-credit price).
3. The server calls the facilitator `/verify` then `/settle`, credits the seat once per payment
   (idempotency key = the payer's signature; replays return the earlier settlement and never credit
   twice), and the claim proceeds. Success responses carry `PAYMENT-RESPONSE: <base64 SettlementResponse>`.

**Env to go live:**

| Var | Meaning |
|---|---|
| `PG_X402_PAY_TO` | Your receiving address (0x…, any EVM address you control: Coinbase Business, CDP wallet, or self-custody) |
| `PG_X402_NETWORK` | `base` (eip155:8453, default) or `base-sepolia` (eip155:84532, testnet) |
| `PG_CREDIT_USD` | USD per credit (default 0.05; one claim = `PG_PRICE_CREDITS` credits) |
| `PG_X402_FACILITATOR` | Override; default CDP (`https://api.cdp.coinbase.com/platform/v2/x402`) on mainnet, `https://x402.org/facilitator` on Base Sepolia |
| `PG_CDP_API_KEY_ID` / `PG_CDP_API_KEY_SECRET` | CDP secret API key (Ed25519) for the CDP facilitator; built-in EdDSA JWT signing, no dependency |

**What you must create:** a Coinbase Developer Platform account → Secret API key (Ed25519); a receiving
wallet (Coinbase Business if you want free ACH cash-out to a bank, else any address). Testnet first:
`PG_X402_NETWORK=base-sepolia` uses the public facilitator with no account at all.

**Alternative — Stripe Machine Payments Protocol (MPP):** stablecoin payments from $0.01 at a flat 1.5%,
settling to your Stripe balance in fiat; requires a Stripe account, review for the "Stablecoins and
Crypto" method, US seller (not NY), preview APIs. Stripe also accepts x402 on Base via the CDP
facilitator. Card-network agent rails (Visa TAP, Mastercard Agent Pay, AP2) are not seller rails for
API micro-calls yet. The meter here is rail-agnostic: a Stripe webhook would call
`POST /seats/{actor}/credit` with the admin key.

## Tax export

`python -m src.cli ledger-export --period YYYY-MM` writes two CSVs under `out/`: `ledger_<period>_receipts.csv`
(one row per settled payment: received-at UTC, payer, amount USDC, USD fair value at $1.00, transaction id,
network, rail, seat, credits granted, payment key, plus a TOTAL line) and `ledger_<period>_usage.csv` (credits
consumed per seat). For Schedule C: USDC received for the Service is ordinary income at $1.00 fair market value
on receipt; conversion to USD later is a separate (near-zero-gain) disposition. Set `PG_SEATS_DB` to the live
seats database (on Railway: the volume at `/app/out/seats.db`).

Charge-on-error rule (also in `/terms` and `/pricing`): a verifier error or timeout is not charged; a submitted
resolution that fails the verifier is charged — the verdict is the product; reads are free.
