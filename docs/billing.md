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
