# Accounts Payable Exception Policy (AP-POL-7, effective 2025-09-01)

All exceptions are worked from the exceptions queue. Resolve the underlying record,
then mark the exception resolved with a one-line summary. Escalate anything you cannot
complete under these rules; escalated items go to the AP supervisor queue.

## 1. Price mismatch (invoice unit price differs from PO unit price)
Compute the price variance: sum over mismatched lines of (invoice price - PO price) x quantity.
- Variance <= 0 (invoice is cheaper): approve the invoice at the invoice total.
- Variance within tolerance (<= $25.00 in total OR <= 2.0% of the PO value of the invoiced lines): approve at the invoice total. Note the variance.
- Variance above tolerance: dispute the invoice with the vendor. Reason must state "price variance" and the amount. Do not approve any part of it.

## 2. Quantity mismatch (invoice quantity exceeds goods received)
- If nothing at all has been received (every invoiced line has zero received quantity): put the invoice on hold with reason `awaiting_receipt`.
- Otherwise (at least one line has some received quantity) approve the received portion only: approved amount = sum over lines of min(invoiced qty, received qty) x invoice unit price, plus tax at the vendor's rate, rounded to cents. A line with zero received quantity contributes nothing and is simply short-paid; do not hold the invoice because of it. Note the short-paid lines.

## 3. Possible duplicate (another invoice from the same vendor matches on number or amount)
- True duplicate: same vendor, same invoice number AND same total as an earlier invoice that is approved or paid. Reject the newer invoice with reason `duplicate of <invoice id>`.
- Otherwise (different invoice number with the same amount, or same number with a different amount and a different PO): it is a legitimate invoice. Approve it at the invoice total.
- Never alter the earlier invoice.

## 4. Missing or invalid PO reference
Find an open purchase order for the same vendor whose lines contain every invoiced SKU at the same unit price with enough remaining (ordered minus already invoiced) quantity.
- Exactly one such PO: link the invoice to it, then approve at the invoice total.
- None: hold the invoice with reason `po_required` and request a PO from the requester.

## 5. Unmatched payment (bank debit with no reconciled invoice)
Identify the vendor from the counterparty name. Among that vendor's approved, unpaid invoices dated on or before the transaction date:
- One invoice whose total equals the debit amount: match the transaction to it (the invoice becomes paid).
- No single invoice but exactly one pair of invoices whose totals sum to the amount: match both.
- Otherwise flag the transaction with reason `no_matching_invoice` for treasury.

## 6. Vendor bank detail mismatch (remit-to account differs from vendor master)
Treat as potential fraud. Hold the invoice with reason `bank_detail_mismatch` and flag the vendor for verification. Never approve or pay until the vendor master has been re-verified by phone.

## Costing
Loaded cost of an AP clerk is recorded in system settings (`loaded_hourly_cost`). Handling minutes are logged on each resolved exception.
