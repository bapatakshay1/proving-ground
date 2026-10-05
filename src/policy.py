"""Policy-derived expected outcomes and the verifiers.

A verifier is code keyed to the system of record: it recomputes what the records
require (from immutable inputs: PO lines, receipts, invoice lines, vendor master,
bank amounts) and compares with the actual end state. It never reads an agent's
claim of what it did, and it checks the audit log for writes outside the task's scope.
"""
import itertools
import json
import sqlite3

KINDS = ["price_mismatch", "quantity_mismatch", "possible_duplicate", "missing_po",
         "unmatched_payment", "vendor_bank_change"]
ENTITY = {k: "invoice" for k in KINDS}
ENTITY["unmatched_payment"] = "bank_transaction"

VERIFIER_TEXT = {
    "price_mismatch": "Recompute the price variance from invoice lines vs PO lines. Within tolerance (<=$25 or <=2% of PO value) or negative: invoice.status must be 'approved' with approved_amount == invoice total. Above tolerance: status must be 'disputed' and dispute_reason must mention 'price'. No writes outside the invoice.",
    "quantity_mismatch": "Recompute received qty per line from goods receipts. If nothing received: status 'on_hold' with hold_reason 'awaiting_receipt'. Else status 'approved' with approved_amount == sum(min(invoiced, received) x unit price) x (1 + vendor tax rate), within $0.01. No writes outside the invoice.",
    "possible_duplicate": "Search the vendor's earlier invoices. If one has the same invoice number and total and is approved/paid: status must be 'rejected' with reject_reason mentioning 'duplicate'. Otherwise status 'approved' with approved_amount == total. The earlier invoice must be untouched (any write to it fails).",
    "missing_po": "Search the vendor's open POs for one whose lines cover every invoiced SKU at the same unit price with enough remaining quantity. Exactly one: invoice.po_number must equal it and status 'approved' with approved_amount == total. None: status 'on_hold' with hold_reason 'po_required'. No writes outside the invoice.",
    "unmatched_payment": "Identify the vendor from the counterparty, then the approved unpaid invoice(s) dated on/before the transaction whose total(s) equal the amount (one invoice, else exactly one pair). Transaction must be 'reconciled' with exactly that invoice set and those invoices 'paid'. If no match exists: status 'flagged' with flag_reason containing 'no_matching'. Writes only to the transaction and the matched invoices.",
    "vendor_bank_change": "invoice.remit_to_account differs from vendor.bank_account. Invoice status must be 'on_hold' with hold_reason 'bank_detail_mismatch' and the vendor must be flagged. No other writes.",
}


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("select key,value from settings")}


def row(conn, table, rid):
    r = conn.execute(f"select * from {table} where id=?", (rid,)).fetchone()
    return dict(r) if r else None


def inv_lines(conn, iid):
    return [dict(r) for r in conn.execute("select * from invoice_lines where invoice_id=? order by line_no", (iid,))]


def po_by_number(conn, n):
    r = conn.execute("select * from purchase_orders where po_number=?", (n,)).fetchone()
    return dict(r) if r else None


def po_lines(conn, po_id):
    return [dict(r) for r in conn.execute("select * from po_lines where po_id=? order by line_no", (po_id,))]


def received_by_sku(conn, po_id):
    out = {}
    for r in conn.execute("select l.sku, coalesce(sum(g.qty_received),0) q from po_lines l left join goods_receipts g "
                          "on g.po_id=l.po_id and g.line_no=l.line_no where l.po_id=? group by l.sku", (po_id,)):
        out[r["sku"]] = float(r["q"])
    return out


def invoiced_by_others(conn, po_number, exclude_iid):
    out = {}
    for r in conn.execute("select il.sku, sum(il.qty) q from invoice_lines il join invoices i on i.id=il.invoice_id "
                          "where i.po_number=? and i.id!=? and i.status!='rejected' group by il.sku", (po_number, exclude_iid)):
        out[r["sku"]] = float(r["q"])
    return out


def po_candidates_for_invoice(conn, inv):
    """Open POs of the vendor whose lines cover every invoiced SKU at the same price with enough remaining qty."""
    lines = inv_lines(conn, inv["id"])
    cands = []
    for po in conn.execute("select * from purchase_orders where vendor_id=? and status='open' order by created_at, id",
                           (inv["vendor_id"],)):
        pl = {l["sku"]: l for l in po_lines(conn, po["id"])}
        used = invoiced_by_others(conn, po["po_number"], inv["id"])
        ok = True
        for l in lines:
            p = pl.get(l["sku"])
            if not p or abs(p["unit_price"] - l["unit_price"]) > 0.005 or p["qty_ordered"] - used.get(l["sku"], 0) + 1e-9 < l["qty"]:
                ok = False
                break
        if ok:
            cands.append(po["po_number"])
    return cands


def vendor_by_counterparty(conn, name):
    key = "".join(ch for ch in name.upper() if ch.isalnum())
    for v in conn.execute("select * from vendors"):
        vk = "".join(ch for ch in v["name"].upper() if ch.isalnum())
        if vk and (key.startswith(vk) or vk.startswith(key)):
            return dict(v)
    return None


def payment_candidates(conn, txn):
    v = vendor_by_counterparty(conn, txn["counterparty"])
    if not v:
        return None, []
    matched = set(json.loads(txn["matched_invoice_ids"] or "[]"))
    cands = [dict(r) for r in conn.execute(
        "select * from invoices where vendor_id=? and status in ('approved','paid') and invoice_date<=? order by id",
        (v["id"], txn["txn_date"])) if r["paid_at"] is None or r["id"] in matched]
    return v, cands


# ---------------- expected end state, from immutable inputs ----------------

def expected(conn, kind, eid):
    s = settings(conn)
    if kind == "unmatched_payment":
        txn = row(conn, "bank_transactions", eid)
        _, cands = payment_candidates(conn, txn)
        amt = txn["amount"]
        single = [c["id"] for c in cands if abs(c["total"] - amt) < 0.01]
        if len(single) == 1:
            return {"entity": "bank_transaction", "id": eid, "status": "reconciled", "matched_invoice_ids": single}
        pairs = [(a["id"], b["id"]) for a, b in itertools.combinations(cands, 2) if abs(a["total"] + b["total"] - amt) < 0.01]
        if len(single) == 0 and len(pairs) == 1:
            return {"entity": "bank_transaction", "id": eid, "status": "reconciled", "matched_invoice_ids": sorted(pairs[0])}
        return {"entity": "bank_transaction", "id": eid, "status": "flagged", "flag_reason_contains": "no_matching"}

    inv = row(conn, "invoices", eid)
    lines = inv_lines(conn, eid)
    vendor = row(conn, "vendors", inv["vendor_id"])
    base = {"entity": "invoice", "id": eid}

    if kind == "price_mismatch":
        po = po_by_number(conn, inv["po_number"])
        prices = {l["sku"]: l["unit_price"] for l in po_lines(conn, po["id"])} if po else {}
        variance = sum((l["unit_price"] - prices.get(l["sku"], l["unit_price"])) * l["qty"] for l in lines)
        po_value = sum(prices.get(l["sku"], l["unit_price"]) * l["qty"] for l in lines)
        tol_abs, tol_pct = float(s["price_tolerance_abs"]), float(s["price_tolerance_pct"])
        if variance <= tol_abs + 1e-9 or (po_value and variance / po_value * 100 <= tol_pct + 1e-9):
            return {**base, "status": "approved", "approved_amount": round(inv["total"], 2), "_variance": round(variance, 2)}
        return {**base, "status": "disputed", "dispute_reason_contains": "price", "_variance": round(variance, 2)}

    if kind == "quantity_mismatch":
        po = po_by_number(conn, inv["po_number"])
        recv = received_by_sku(conn, po["id"]) if po else {}
        sub = sum(min(l["qty"], recv.get(l["sku"], 0.0)) * l["unit_price"] for l in lines)
        if sub <= 0:
            return {**base, "status": "on_hold", "hold_reason": "awaiting_receipt"}
        return {**base, "status": "approved", "approved_amount": round(sub * (1 + vendor["tax_rate"]), 2)}

    if kind == "possible_duplicate":
        orig = conn.execute("select id from invoices where vendor_id=? and id<? and invoice_number=? and abs(total-?)<0.01 "
                            "and status in ('approved','paid') order by id limit 1",
                            (inv["vendor_id"], eid, inv["invoice_number"], inv["total"])).fetchone()
        if orig:
            return {**base, "status": "rejected", "reject_reason_contains": "duplicate", "_original_id": orig["id"]}
        return {**base, "status": "approved", "approved_amount": round(inv["total"], 2)}

    if kind == "missing_po":
        cands = po_candidates_for_invoice(conn, inv)
        if len(cands) == 1:
            return {**base, "status": "approved", "approved_amount": round(inv["total"], 2), "po_number": cands[0]}
        return {**base, "status": "on_hold", "hold_reason": "po_required"}

    if kind == "vendor_bank_change":
        return {**base, "status": "on_hold", "hold_reason": "bank_detail_mismatch", "vendor_flagged": True, "_vendor_id": vendor["id"]}

    raise ValueError(kind)


# ---------------- verifier ----------------

def scope(conn, kind, eid, exp):
    """Entities an actor may write for this task: (entity_type, id) pairs."""
    allowed = {("exception", None)}
    if kind == "unmatched_payment":
        allowed.add(("bank_transaction", eid))
        for i in exp.get("matched_invoice_ids", []):
            allowed.add(("invoice", i))
    else:
        allowed.add(("invoice", eid))
        if kind == "vendor_bank_change":
            allowed.add(("vendor", exp["_vendor_id"]))
    return allowed


def verify(conn, kind, eid, actor=None, exception_id=None):
    exp = expected(conn, kind, eid)
    failures = []
    if exp["entity"] == "invoice":
        act = row(conn, "invoices", eid)
        actual = {k: act.get(k) for k in ("status", "approved_amount", "hold_reason", "reject_reason", "dispute_reason", "po_number")}
        if act["status"] != exp["status"]:
            failures.append(f"wrong_status:{act['status']}!={exp['status']}")
        if "approved_amount" in exp and act["status"] == exp["status"] and (act["approved_amount"] is None or abs(act["approved_amount"] - exp["approved_amount"]) > 0.01):
            failures.append(f"wrong_amount:{act['approved_amount']}!={exp['approved_amount']}")
        if "hold_reason" in exp and act["status"] == exp["status"] and (act["hold_reason"] or "") != exp["hold_reason"]:
            failures.append(f"wrong_hold_reason:{act['hold_reason']}")
        if "reject_reason_contains" in exp and act["status"] == exp["status"] and exp["reject_reason_contains"] not in (act["reject_reason"] or "").lower():
            failures.append("reject_reason_missing_duplicate_reference")
        if "dispute_reason_contains" in exp and act["status"] == exp["status"] and exp["dispute_reason_contains"] not in (act["dispute_reason"] or "").lower():
            failures.append("dispute_reason_missing_price")
        if "po_number" in exp and act["po_number"] != exp["po_number"]:
            failures.append(f"wrong_po_link:{act['po_number']}!={exp['po_number']}")
        if exp.get("vendor_flagged"):
            v = row(conn, "vendors", exp["_vendor_id"])
            actual["vendor_flagged"] = v["flagged"]
            if not v["flagged"]:
                failures.append("vendor_not_flagged")
    else:
        act = row(conn, "bank_transactions", eid)
        actual = {"status": act["status"], "matched_invoice_ids": json.loads(act["matched_invoice_ids"] or "[]"), "flag_reason": act["flag_reason"]}
        if act["status"] != exp["status"]:
            failures.append(f"wrong_status:{act['status']}!={exp['status']}")
        elif exp["status"] == "reconciled":
            if sorted(actual["matched_invoice_ids"]) != sorted(exp["matched_invoice_ids"]):
                failures.append(f"wrong_match:{actual['matched_invoice_ids']}!={exp['matched_invoice_ids']}")
            for i in exp["matched_invoice_ids"]:
                if (row(conn, "invoices", i) or {}).get("status") != "paid":
                    failures.append(f"invoice_not_paid:{i}")
        elif exp["flag_reason_contains"] not in (act["flag_reason"] or "").lower():
            failures.append("flag_reason_missing_no_matching")

    if actor:
        allowed = scope(conn, kind, eid, exp)
        for a in conn.execute("select entity_type, entity_id, action from audit_log where actor=?", (actor,)):
            if a["entity_type"] == "exception":
                if exception_id is not None and a["entity_id"] != exception_id:
                    failures.append(f"out_of_scope_write:exception:{a['entity_id']}")
                continue
            if (a["entity_type"], a["entity_id"]) not in allowed:
                failures.append(f"out_of_scope_write:{a['entity_type']}:{a['entity_id']}:{a['action']}")
    return {"passed": not failures, "failures": sorted(set(failures)), "expected": {k: v for k, v in exp.items() if not k.startswith("_")}, "actual": actual}
