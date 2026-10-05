"""State-changing actions on the twin. The only way any actor (human in history,
agent via the API) mutates the system of record. Every action writes the audit log."""
import json
import datetime as dt


class ActionError(Exception):
    pass


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def audit(conn, actor, action, etype, eid, detail, ts=None):
    conn.execute("insert into audit_log(ts,actor,action,entity_type,entity_id,detail) values(?,?,?,?,?,?)",
                 (ts or now_iso(), actor, action, etype, eid, json.dumps(detail)))


def _inv(conn, iid):
    r = conn.execute("select * from invoices where id=?", (iid,)).fetchone()
    if not r:
        raise ActionError(f"invoice {iid} not found")
    return r


def approve_invoice(conn, actor, iid, approved_amount=None, note=None, ts=None):
    inv = _inv(conn, iid)
    if inv["status"] in ("rejected", "paid"):
        raise ActionError(f"invoice {iid} is {inv['status']}; cannot approve")
    amt = round(float(approved_amount if approved_amount is not None else inv["total"]), 2)
    if amt <= 0 or amt > inv["total"] + 0.005:
        raise ActionError("approved_amount must be > 0 and <= invoice total")
    conn.execute("update invoices set status='approved', approved_amount=?, note=?, hold_reason=NULL, dispute_reason=NULL, "
                 "resolved_by=?, resolved_at=? where id=?", (amt, note, actor, ts or now_iso(), iid))
    audit(conn, actor, "approve_invoice", "invoice", iid, {"approved_amount": amt, "note": note}, ts)
    return {"ok": True, "invoice_id": iid, "status": "approved", "approved_amount": amt}


def hold_invoice(conn, actor, iid, reason, note=None, ts=None):
    inv = _inv(conn, iid)
    if inv["status"] in ("rejected", "paid"):
        raise ActionError(f"invoice {iid} is {inv['status']}; cannot hold")
    conn.execute("update invoices set status='on_hold', hold_reason=?, note=?, resolved_by=?, resolved_at=? where id=?",
                 (reason, note, actor, ts or now_iso(), iid))
    audit(conn, actor, "hold_invoice", "invoice", iid, {"reason": reason, "note": note}, ts)
    return {"ok": True, "invoice_id": iid, "status": "on_hold", "hold_reason": reason}


def reject_invoice(conn, actor, iid, reason, note=None, ts=None):
    inv = _inv(conn, iid)
    if inv["status"] == "paid":
        raise ActionError(f"invoice {iid} is paid; cannot reject")
    conn.execute("update invoices set status='rejected', reject_reason=?, note=?, approved_amount=NULL, resolved_by=?, resolved_at=? where id=?",
                 (reason, note, actor, ts or now_iso(), iid))
    audit(conn, actor, "reject_invoice", "invoice", iid, {"reason": reason, "note": note}, ts)
    return {"ok": True, "invoice_id": iid, "status": "rejected", "reject_reason": reason}


def dispute_invoice(conn, actor, iid, reason, note=None, ts=None):
    inv = _inv(conn, iid)
    if inv["status"] in ("rejected", "paid"):
        raise ActionError(f"invoice {iid} is {inv['status']}; cannot dispute")
    conn.execute("update invoices set status='disputed', dispute_reason=?, note=?, approved_amount=NULL, resolved_by=?, resolved_at=? where id=?",
                 (reason, note, actor, ts or now_iso(), iid))
    audit(conn, actor, "dispute_invoice", "invoice", iid, {"reason": reason, "note": note}, ts)
    return {"ok": True, "invoice_id": iid, "status": "disputed", "dispute_reason": reason}


def link_po(conn, actor, iid, po_number, ts=None):
    inv = _inv(conn, iid)
    po = conn.execute("select * from purchase_orders where po_number=?", (po_number,)).fetchone()
    if not po:
        raise ActionError(f"PO {po_number} not found")
    if po["vendor_id"] != inv["vendor_id"]:
        raise ActionError(f"PO {po_number} belongs to a different vendor")
    conn.execute("update invoices set po_number=? where id=?", (po_number, iid))
    audit(conn, actor, "link_po", "invoice", iid, {"po_number": po_number}, ts)
    return {"ok": True, "invoice_id": iid, "po_number": po_number}


def match_bank_transaction(conn, actor, tid, invoice_ids, ts=None):
    txn = conn.execute("select * from bank_transactions where id=?", (tid,)).fetchone()
    if not txn:
        raise ActionError(f"bank transaction {tid} not found")
    if txn["status"] == "reconciled":
        raise ActionError(f"bank transaction {tid} already reconciled")
    ids = sorted({int(i) for i in invoice_ids})
    if not ids:
        raise ActionError("invoice_ids required")
    for iid in ids:
        inv = _inv(conn, iid)
        if inv["status"] != "approved":
            raise ActionError(f"invoice {iid} is {inv['status']}; only approved invoices can be matched to a payment")
    for iid in ids:
        conn.execute("update invoices set status='paid', paid_at=? where id=?", (txn["txn_date"], iid))
        audit(conn, actor, "mark_paid", "invoice", iid, {"bank_transaction_id": tid}, ts)
    conn.execute("update bank_transactions set status='reconciled', matched_invoice_ids=?, flag_reason=NULL where id=?",
                 (json.dumps(ids), tid))
    audit(conn, actor, "match_bank_transaction", "bank_transaction", tid, {"invoice_ids": ids}, ts)
    return {"ok": True, "bank_transaction_id": tid, "status": "reconciled", "matched_invoice_ids": ids}


def flag_bank_transaction(conn, actor, tid, reason, ts=None):
    txn = conn.execute("select * from bank_transactions where id=?", (tid,)).fetchone()
    if not txn:
        raise ActionError(f"bank transaction {tid} not found")
    if txn["status"] == "reconciled":
        raise ActionError(f"bank transaction {tid} already reconciled")
    conn.execute("update bank_transactions set status='flagged', flag_reason=? where id=?", (reason, tid))
    audit(conn, actor, "flag_bank_transaction", "bank_transaction", tid, {"reason": reason}, ts)
    return {"ok": True, "bank_transaction_id": tid, "status": "flagged", "flag_reason": reason}


def flag_vendor(conn, actor, vid, reason, ts=None):
    if not conn.execute("select 1 from vendors where id=?", (vid,)).fetchone():
        raise ActionError(f"vendor {vid} not found")
    conn.execute("update vendors set flagged=1, flag_reason=? where id=?", (reason, vid))
    audit(conn, actor, "flag_vendor", "vendor", vid, {"reason": reason}, ts)
    return {"ok": True, "vendor_id": vid, "flagged": True}


def resolve_exception(conn, actor, xid, summary, ts=None):
    x = conn.execute("select * from exceptions where id=?", (xid,)).fetchone()
    if not x:
        raise ActionError(f"exception {xid} not found")
    if x["status"] != "open":
        raise ActionError(f"exception {xid} is {x['status']}")
    conn.execute("update exceptions set status='resolved', resolved_by=?, resolved_at=?, resolution=? where id=?",
                 (actor, ts or now_iso(), json.dumps({"summary": summary}), xid))
    audit(conn, actor, "resolve_exception", "exception", xid, {"summary": summary}, ts)
    return {"ok": True, "exception_id": xid, "status": "resolved"}


def escalate_exception(conn, actor, xid, reason, ts=None):
    x = conn.execute("select * from exceptions where id=?", (xid,)).fetchone()
    if not x:
        raise ActionError(f"exception {xid} not found")
    if x["status"] != "open":
        raise ActionError(f"exception {xid} is {x['status']}")
    conn.execute("update exceptions set status='escalated', resolved_by=?, resolved_at=?, resolution=? where id=?",
                 (actor, ts or now_iso(), json.dumps({"escalation_reason": reason}), xid))
    audit(conn, actor, "escalate_exception", "exception", xid, {"reason": reason}, ts)
    return {"ok": True, "exception_id": xid, "status": "escalated"}
