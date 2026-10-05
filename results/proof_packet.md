# Proof packet — Harbor Point Manufacturing (as of 2026-10-01)

Generated 2026-10-05T04:29:36Z. Proposer `google/gemini-2.5-flash`, solver `openai/gpt-4.1-mini`, breaker `deepseek/deepseek-chat-v3-0324`, verifier = code keyed to the system of record.

**Gate — at least five verifiers survive the breaker: PASS** (5 admitted). Stop rule tripped: ['quantity_mismatch'].

| Workflow | Monthly vol | Unit cost now | Admitted | Priced tier | Replay pass (n=20) | Price/outcome | Cost to serve | Margin | Live metered | Proj. monthly rev |
|---|---|---|---|---|---|---|---|---|---|---|
| unmatched_payment | 55.0 | $11.45 | yes | `gpt-4.1` | 60% [0.387, 0.781] | $5.72 | $0.325 | 94% | 75% (6/8) | $188.76 |
| missing_po | 55.0 | $10.45 | yes | `gpt-4.1-mini` | 90% [0.699, 0.972] | $5.22 | $0.256 | 95% | 100% (8/8) | $258.39 |
| quantity_mismatch | 55.0 | $9.16 | yes | `gpt-4.1-mini` | 100% [0.839, 1.0] | $4.58 | $0.257 | 94% | 88% (7/8) | $251.9 |
| price_mismatch | 55.0 | $6.35 | yes | `gpt-4.1-mini` | 95% [0.764, 0.991] | $3.17 | $0.257 | 92% | 88% (7/8) | $165.63 |
| possible_duplicate | 55.0 | $4.03 | yes | `gpt-4.1` | 100% [0.839, 1.0] | $2.02 | $0.282 | 86% | 100% (8/8) | $111.1 |
| vendor_bank_change | 55.0 | $4.94 | **no** | — | — | — | — | — | — | — |

## unmatched_payment — Automate Unmatched Payment Resolution

An autonomous agent can identify the vendor from the counterparty name and search for matching approved, unpaid invoices to reconcile bank transactions, either individually or in pairs, or flag for treasury if no match is found.

*Why it needs judgement (proposer):* The agent needs to determine if a single invoice or a pair of invoices matches the debit amount, or if no match exists, requiring a decision to flag for treasury.

**Verifier.** Identify the vendor from the counterparty, then the approved unpaid invoice(s) dated on/before the transaction whose total(s) equal the amount (one invoice, else exactly one pair). Transaction must be 'reconciled' with exactly that invoice set and those invoices 'paid'. If no match exists: status 'flagged' with flag_reason containing 'no_matching'. Writes only to the transaction and the matched invoices.

**Admission.** Verifier agrees with human history on 98.2% of 600 resolved cases. Record-blind shortcuts (n=25): resolve_only 0%, approve_full 0%, hold_generic 0%, hold_policy_reason 0%, reject_as_duplicate 0%, dispute_price 0%, flag_payment 28%. LLM breaker (deepseek/deepseek-chat-v3-0324, write-only tools): 0% of 8. Best blind 28% (95% CI 14%–48%) vs. screen-out threshold 70% on the lower bound. → **ADMITTED**

**Replay on 20 held-out past cases** (priced tier `openai/gpt-4.1`). Pass rate 60% (95% CI 39%–78%); agrees with the clerk's recorded outcome 55% (1 clerk overrides in sample). Mean 9.1 tool calls, $0.0359 model spend per case.

Known failure modes: out_of_scope_write ×9, wrong_status ×4, not_finished:max_steps ×3, invoice_not_paid ×1, wrong_match ×1.

**Price.** Current unit cost $11.45 (12.7 clerk-minutes). Price per verified outcome $5.72. Cost to serve $0.325 (measured $0.0359 model spend per attempt ÷ 60% verified = $0.0598, +25% overhead, +$0.25 operations allowance) → gross margin 94%. At 55.0/month and 60% verified: $188.76/month revenue, $189.09/month customer saving. Contract bar 90%: NOT met. Proposed annual minimum: 204 verified outcomes.

**Live backlog (metered).** 6/8 verified and billed ($34.32); gap to replay -15.0 pts. Routed to a human unbilled: #3245 max_steps; #3249 invoice_not_paid:7995,invoice_not_paid:7996,out_of_scope_write:invoice:2238:mark_paid,out_of_scope_write:invoice:8042:mark_paid,wrong_match:[2238, 8042]!=[7995, 7996].

## missing_po — Automate Missing or Invalid PO Reference Resolution

An autonomous agent can search for an open purchase order for the same vendor that contains all invoiced SKUs at the correct unit price and sufficient remaining quantity, then link the invoice or hold it if no suitable PO is found.

*Why it needs judgement (proposer):* The agent needs to determine if exactly one suitable PO exists to link, or if none exist, requiring a decision to hold the invoice and request a PO.

**Verifier.** Search the vendor's open POs for one whose lines cover every invoiced SKU at the same unit price with enough remaining quantity. Exactly one: invoice.po_number must equal it and status 'approved' with approved_amount == total. None: status 'on_hold' with hold_reason 'po_required'. No writes outside the invoice.

**Admission.** Verifier agrees with human history on 100.0% of 600 resolved cases. Record-blind shortcuts (n=25): resolve_only 0%, approve_full 0%, hold_generic 0%, hold_policy_reason 52%, reject_as_duplicate 0%, dispute_price 0%, flag_payment 0%. LLM breaker (deepseek/deepseek-chat-v3-0324, write-only tools): 38% of 8. Best blind 52% (95% CI 34%–70%) vs. screen-out threshold 70% on the lower bound. → **ADMITTED**

**Replay on 20 held-out past cases** (priced tier `openai/gpt-4.1-mini`). Pass rate 90% (95% CI 70%–97%); agrees with the clerk's recorded outcome 90% (0 clerk overrides in sample). Mean 6.8 tool calls, $0.0045 model spend per case.

Known failure modes: wrong_status ×2.

**Price.** Current unit cost $10.45 (11.6 clerk-minutes). Price per verified outcome $5.22. Cost to serve $0.256 (measured $0.0045 model spend per attempt ÷ 90% verified = $0.0050, +25% overhead, +$0.25 operations allowance) → gross margin 95%. At 55.0/month and 90% verified: $258.39/month revenue, $258.88/month customer saving. Contract bar 90%: met. Proposed annual minimum: 369 verified outcomes.

**Live backlog (metered).** 8/8 verified and billed ($41.76); gap to replay -10.0 pts. Routed to a human unbilled: none.

## quantity_mismatch — Automate Quantity Mismatch Resolution

An autonomous agent can determine if nothing has been received for an invoice, placing it on hold, or if some quantity has been received, approving only the received portion and noting short-paid lines.

*Why it needs judgement (proposer):* The agent needs to determine if any quantity has been received to decide between holding the invoice or approving a partial amount.

**Verifier.** Recompute received qty per line from goods receipts. If nothing received: status 'on_hold' with hold_reason 'awaiting_receipt'. Else status 'approved' with approved_amount == sum(min(invoiced, received) x unit price) x (1 + vendor tax rate), within $0.01. No writes outside the invoice.

**Admission.** Verifier agrees with human history on 97.7% of 600 resolved cases. Record-blind shortcuts (n=25): resolve_only 0%, approve_full 4%, hold_generic 0%, hold_policy_reason 40%, reject_as_duplicate 0%, dispute_price 0%, flag_payment 0%. LLM breaker (deepseek/deepseek-chat-v3-0324, write-only tools): 12% of 8. Best blind 40% (95% CI 23%–59%) vs. screen-out threshold 70% on the lower bound. → **ADMITTED**

**Replay on 20 held-out past cases** (priced tier `openai/gpt-4.1-mini`). Pass rate 100% (95% CI 84%–100%); agrees with the clerk's recorded outcome 100% (0 clerk overrides in sample). Mean 7.3 tool calls, $0.0054 model spend per case.

Known failure modes: none observed.

**Price.** Current unit cost $9.16 (10.2 clerk-minutes). Price per verified outcome $4.58. Cost to serve $0.257 (measured $0.0054 model spend per attempt ÷ 100% verified = $0.0054, +25% overhead, +$0.25 operations allowance) → gross margin 94%. At 55.0/month and 100% verified: $251.9/month revenue, $251.9/month customer saving. Contract bar 90%: met. Proposed annual minimum: 442 verified outcomes.

**Live backlog (metered).** 7/8 verified and billed ($32.06); gap to replay +12.5 pts. Routed to a human unbilled: #1266 wrong_amount:16803.53!=16903.53.

## price_mismatch — Automate Price Mismatch Resolution

An autonomous agent can calculate the price variance and, based on predefined tolerances, either approve the invoice at its total, or dispute it with the vendor if the variance is above tolerance.

*Why it needs judgement (proposer):* The agent needs to calculate the variance and compare it against tolerance thresholds to decide whether to approve or dispute the invoice.

**Verifier.** Recompute the price variance from invoice lines vs PO lines. Within tolerance (<=$25 or <=2% of PO value) or negative: invoice.status must be 'approved' with approved_amount == invoice total. Above tolerance: status must be 'disputed' and dispute_reason must mention 'price'. No writes outside the invoice.

**Admission.** Verifier agrees with human history on 98.2% of 600 resolved cases. Record-blind shortcuts (n=25): resolve_only 0%, approve_full 44%, hold_generic 0%, hold_policy_reason 0%, reject_as_duplicate 0%, dispute_price 56%, flag_payment 0%. LLM breaker (deepseek/deepseek-chat-v3-0324, write-only tools): 50% of 8. Best blind 56% (95% CI 37%–73%) vs. screen-out threshold 70% on the lower bound. → **ADMITTED**

**Replay on 20 held-out past cases** (priced tier `openai/gpt-4.1-mini`). Pass rate 95% (95% CI 76%–99%); agrees with the clerk's recorded outcome 95% (0 clerk overrides in sample). Mean 7.1 tool calls, $0.0053 model spend per case.

Known failure modes: wrong_status ×1.

**Price.** Current unit cost $6.35 (7.1 clerk-minutes). Price per verified outcome $3.17. Cost to serve $0.257 (measured $0.0053 model spend per attempt ÷ 95% verified = $0.0056, +25% overhead, +$0.25 operations allowance) → gross margin 92%. At 55.0/month and 95% verified: $165.63/month revenue, $166.15/month customer saving. Contract bar 90%: met. Proposed annual minimum: 403 verified outcomes.

**Live backlog (metered).** 7/8 verified and billed ($22.19); gap to replay +7.5 pts. Routed to a human unbilled: #606 wrong_status:approved!=disputed.

## possible_duplicate — Automate Possible Duplicate Invoice Resolution

An autonomous agent can identify true duplicates (same vendor, invoice number, and total as an earlier approved/paid invoice) and reject them, or approve legitimate invoices that are not true duplicates.

*Why it needs judgement (proposer):* The agent needs to compare multiple invoice attributes (vendor, number, total, PO) to determine if an invoice is a true duplicate or a legitimate, unique invoice.

**Verifier.** Search the vendor's earlier invoices. If one has the same invoice number and total and is approved/paid: status must be 'rejected' with reject_reason mentioning 'duplicate'. Otherwise status 'approved' with approved_amount == total. The earlier invoice must be untouched (any write to it fails).

**Admission.** Verifier agrees with human history on 98.0% of 600 resolved cases. Record-blind shortcuts (n=25): resolve_only 0%, approve_full 32%, hold_generic 0%, hold_policy_reason 0%, reject_as_duplicate 68%, dispute_price 0%, flag_payment 0%. LLM breaker (deepseek/deepseek-chat-v3-0324, write-only tools): 62% of 8. Best blind 68% (95% CI 48%–83%) vs. screen-out threshold 70% on the lower bound. → **ADMITTED**

**Replay on 20 held-out past cases** (priced tier `openai/gpt-4.1`). Pass rate 100% (95% CI 84%–100%); agrees with the clerk's recorded outcome 100% (0 clerk overrides in sample). Mean 7.0 tool calls, $0.0256 model spend per case.

Known failure modes: none observed.

**Price.** Current unit cost $4.03 (4.5 clerk-minutes). Price per verified outcome $2.02. Cost to serve $0.282 (measured $0.0256 model spend per attempt ÷ 100% verified = $0.0256, +25% overhead, +$0.25 operations allowance) → gross margin 86%. At 55.0/month and 100% verified: $111.1/month revenue, $110.55/month customer saving. Contract bar 90%: met. Proposed annual minimum: 442 verified outcomes.

**Live backlog (metered).** 8/8 verified and billed ($16.16); gap to replay +0.0 pts. Routed to a human unbilled: none.

## vendor_bank_change — Automate Vendor Bank Detail Mismatch Handling

An autonomous agent can identify when an invoice's remit-to account differs from the vendor master, then hold the invoice and flag the vendor for verification.

*Why it needs judgement (proposer):* The agent needs to compare the invoice's remit-to account with the vendor master record to determine if a mismatch exists, which triggers a hold and flag action.

**Verifier.** invoice.remit_to_account differs from vendor.bank_account. Invoice status must be 'on_hold' with hold_reason 'bank_detail_mismatch' and the vendor must be flagged. No other writes.

**Admission.** Verifier agrees with human history on 100.0% of 600 resolved cases. Record-blind shortcuts (n=25): resolve_only 0%, approve_full 0%, hold_generic 0%, hold_policy_reason 100%, reject_as_duplicate 0%, dispute_price 0%, flag_payment 0%. LLM breaker (deepseek/deepseek-chat-v3-0324, write-only tools): 100% of 8. Best blind 100% (95% CI 87%–100%) vs. screen-out threshold 70% on the lower bound. → **NOT ADMITTED**: record-blind shortcut 'hold_policy_reason' passes 100% (95% lower bound 87%) without reading the records: the outcome is a fixed rule, not a judgement task; route it to deterministic automation / a hard control instead of outcome pricing

## Solver tiers on the same held-out cases

| Model | unmatched_payment | missing_po | quantity_mismatch | price_mismatch | possible_duplicate |
|---|---|---|---|---|---|
| `openai/gpt-4.1-mini` | 55% @ $0.0101 | 90% @ $0.0045 | 100% @ $0.0054 | 95% @ $0.0053 | 60% @ $0.0074 |
| `openai/gpt-4.1` | 60% @ $0.0359 | 100% @ $0.0190 | 95% @ $0.0168 | 100% @ $0.0180 | 100% @ $0.0256 |

Each workflow is priced on the cheapest tier that meets the contract bar (else the best-passing tier). Pass rate @ measured model spend per attempt.

## Return path — what came back as candidate work

- unmatched_payment: not finished (max_steps) (×1)
- unmatched_payment: verifier rejected: invoice_not_paid, out_of_scope_write, wrong_match (×1)
- quantity_mismatch: verifier rejected: wrong_amount (×1)
- price_mismatch: verifier rejected: wrong_status (×1)

## Totals

- Projected monthly revenue across admitted workflows: **$975.78**
- Billed on the live backlog sample: $146.49
- Model spend for this discovery run (proposer + breaker + both solver tiers + live): $3.573

Thresholds (proposals, not findings): {"history_agreement_min": 0.9, "blind_pass_max": 0.7, "price_fraction_of_current_cost": 0.5, "serve_overhead_on_model_spend": 0.25, "contract_bar": 0.9, "annual_minimum_fraction": 0.8, "ops_allowance_per_outcome_usd": 0.25, "discovery_run": "fixed fee, credited against first-year outcome fees (not counted as revenue)", "shadow_gap_max_pts": 10.0}
