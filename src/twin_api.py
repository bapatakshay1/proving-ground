"""The sealed twin. Agents reach the system of record only through this API.

Headers:
  X-Sandbox: <name>   select an isolated copy of the twin (out/sandboxes/<name>.db); default = base twin
  X-Actor:   <id>     recorded on every write in the audit log

Hidden from the API on purpose: exceptions.pre_state/post_state/truth/held_out (harness-only columns).
"""
import json
import os
import pathlib
import sqlite3

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from . import actions, policy

BASE_DB = pathlib.Path(os.environ.get("PG_DB", "out/twin.db"))
SANDBOX_DIR = pathlib.Path(os.environ.get("PG_SANDBOXES", "out/sandboxes"))
app = FastAPI(title="Proving Ground twin", version="0.1")


def db(sandbox):
    if sandbox:
        if not sandbox.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "bad sandbox name")
        p = SANDBOX_DIR / f"{sandbox}.db"
        if not p.exists():
            raise HTTPException(404, f"sandbox {sandbox} not found")
    else:
        p = BASE_DB
    return policy.connect(p)


def public_exception(r):
    d = dict(r)
    for k in ("pre_state", "post_state", "truth", "held_out"):
        d.pop(k, None)
    if d.get("resolution"):
        d["resolution"] = json.loads(d["resolution"])
    return d


@app.get("/health")
def health():
    return {"ok": True, "db": str(BASE_DB)}


@app.get("/schema")
def schema():
    return {
        "entities": {
            "invoice": ["id", "invoice_number", "vendor_id", "po_number", "invoice_date", "due_date", "subtotal", "tax", "total",
                        "remit_to_account", "status(received|approved|on_hold|disputed|rejected|paid)", "approved_amount", "hold_reason",
                        "reject_reason", "dispute_reason", "note", "lines[sku,qty,unit_price,amount]"],
            "purchase_order": ["po_number", "vendor_id", "created_at", "status(open|closed)", "lines[sku,description,qty_ordered,unit_price,qty_received,qty_invoiced]"],
            "vendor": ["id", "name", "bank_account", "tax_rate", "payment_terms_days", "flagged", "flag_reason"],
            "bank_transaction": ["id", "txn_date", "amount", "counterparty", "memo", "status(unreconciled|reconciled|flagged)", "matched_invoice_ids", "flag_reason"],
            "exception": ["id", "kind", "entity_type", "entity_id", "opened_at", "status(open|resolved|escalated)", "resolved_by", "resolved_at", "resolution", "handling_minutes"],
        },
        "reads": ["/policy", "/stats/exceptions", "/exceptions", "/exceptions/{id}", "/invoices/{id}", "/invoices?...", "/pos/{po_number}",
                  "/pos?vendor_id=&status=", "/vendors/{id}", "/vendors?name=", "/bank_transactions/{id}", "/settings"],
        "writes": ["POST /invoices/{id}/approve|hold|reject|dispute|link_po", "POST /bank_transactions/{id}/match|flag",
                   "POST /vendors/{id}/flag", "POST /exceptions/{id}/resolve|escalate"],
    }


@app.get("/policy")
def get_policy():
    return {"policy": (pathlib.Path(__file__).parent / "policy.md").read_text()}


@app.get("/settings")
def get_settings(x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        return policy.settings(c)


@app.get("/stats/exceptions")
def stats(x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        rate = float(policy.settings(c)["loaded_hourly_cost"])
        out = []
        for r in c.execute("select kind, count(*) n, sum(status='open') open, sum(status='resolved') resolved, "
                           "avg(case when status='resolved' then handling_minutes end) mins from exceptions group by kind order by n desc"):
            months = {m["m"]: m["n"] for m in c.execute("select substr(opened_at,1,7) m, count(*) n from exceptions where kind=? group by m", (r["kind"],))}
            mins = r["mins"] or 0
            out.append({"kind": r["kind"], "entity_type": policy.ENTITY[r["kind"]], "total": r["n"], "open": r["open"], "resolved": r["resolved"],
                        "by_month": months, "avg_handling_minutes": round(mins, 1), "current_unit_cost_usd": round(mins / 60 * rate, 2)})
        return {"loaded_hourly_cost": rate, "as_of": policy.settings(c).get("as_of"), "kinds": out}


@app.get("/exceptions")
def list_exceptions(kind: str | None = None, status: str | None = None, limit: int = Query(20, le=200), x_sandbox: str | None = Header(None)):
    q, args = "select * from exceptions where 1=1", []
    if kind:
        q += " and kind=?"; args.append(kind)
    if status:
        q += " and status=?"; args.append(status)
    q += " order by opened_at desc limit ?"; args.append(limit)
    with db(x_sandbox) as c:
        return [public_exception(r) for r in c.execute(q, args)]


@app.get("/exceptions/{xid}")
def get_exception(xid: int, x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        r = c.execute("select * from exceptions where id=?", (xid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        return public_exception(r)


def invoice_view(c, iid):
    inv = policy.row(c, "invoices", iid)
    if not inv:
        raise HTTPException(404, "invoice not found")
    inv["lines"] = inv_lines = policy.inv_lines(c, iid)
    inv["vendor"] = policy.row(c, "vendors", inv["vendor_id"])
    inv["purchase_order"] = po_view(c, inv["po_number"]) if inv["po_number"] else None
    return inv


def po_view(c, po_number):
    po = policy.po_by_number(c, po_number)
    if not po:
        return None
    recv = policy.received_by_sku(c, po["id"])
    used = {}
    for r in c.execute("select il.sku, sum(il.qty) q from invoice_lines il join invoices i on i.id=il.invoice_id where i.po_number=? and i.status!='rejected' group by il.sku", (po_number,)):
        used[r["sku"]] = r["q"]
    po["lines"] = [{**l, "qty_received": recv.get(l["sku"], 0.0), "qty_invoiced": used.get(l["sku"], 0.0)} for l in policy.po_lines(c, po["id"])]
    po["invoice_ids"] = [r["id"] for r in c.execute("select id from invoices where po_number=? order by id", (po_number,))]
    return po


@app.get("/invoices/{iid}")
def get_invoice(iid: int, x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        return invoice_view(c, iid)


@app.get("/invoices")
def search_invoices(vendor_id: int | None = None, invoice_number: str | None = None, status: str | None = None, po_number: str | None = None,
                    min_total: float | None = None, max_total: float | None = None, date_from: str | None = None, date_to: str | None = None,
                    limit: int = Query(50, le=200), x_sandbox: str | None = Header(None)):
    q, a = "select * from invoices where 1=1", []
    for col, val in (("vendor_id", vendor_id), ("invoice_number", invoice_number), ("status", status), ("po_number", po_number)):
        if val not in (None, ""):
            q += f" and {col}=?"; a.append(val)
    if min_total is not None:
        q += " and total>=?"; a.append(min_total)
    if max_total is not None:
        q += " and total<=?"; a.append(max_total)
    if date_from:
        q += " and invoice_date>=?"; a.append(date_from)
    if date_to:
        q += " and invoice_date<=?"; a.append(date_to)
    if min_total is not None and max_total is not None and min_total == max_total:
        q = q.replace(" and total>=?", " and abs(total-?)<0.005").replace(" and total<=?", " and abs(total-?)<0.005")
    q += " order by invoice_date desc, id desc limit ?"; a.append(limit)
    with db(x_sandbox) as c:
        return [dict(r) for r in c.execute(q, a)]


@app.get("/pos/{po_number}")
def get_po(po_number: str, x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        po = po_view(c, po_number)
        if not po:
            raise HTTPException(404, "PO not found")
        return po


@app.get("/pos")
def search_pos(vendor_id: int, status: str | None = None, sku: str | None = None, limit: int = Query(50, le=200), x_sandbox: str | None = Header(None)):
    q, a = "select distinct p.po_number, p.created_at from purchase_orders p", [vendor_id]
    if sku:
        q += " join po_lines l on l.po_id=p.id and l.sku=?"; a.insert(0, sku)
    q += " where p.vendor_id=?"
    if status:
        q += " and p.status=?"; a.append(status)
    q += " order by p.created_at desc limit ?"; a.append(limit)
    with db(x_sandbox) as c:
        out = []
        for r in c.execute(q, a):
            po = po_view(c, r["po_number"])
            po["lines"] = [{k: l[k] for k in ("sku", "qty_ordered", "unit_price", "qty_received", "qty_invoiced")} for l in po["lines"]]
            out.append(po)
        return out


@app.get("/vendors/{vid}")
def get_vendor(vid: int, x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        v = policy.row(c, "vendors", vid)
        if not v:
            raise HTTPException(404, "vendor not found")
        return v


@app.get("/vendors")
def search_vendors(name: str, x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        v = policy.vendor_by_counterparty(c, name)
        like = [dict(r) for r in c.execute("select * from vendors where upper(name) like ? limit 10", (f"%{name.upper()}%",))]
        if v and v not in like:
            like.insert(0, v)
        return like


@app.get("/bank_transactions/{tid}")
def get_txn(tid: int, x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        t = policy.row(c, "bank_transactions", tid)
        if not t:
            raise HTTPException(404, "not found")
        t["matched_invoice_ids"] = json.loads(t["matched_invoice_ids"] or "[]")
        return t


class Approve(BaseModel):
    approved_amount: float | None = None
    note: str | None = None


class Reason(BaseModel):
    reason: str
    note: str | None = None


class LinkPO(BaseModel):
    po_number: str


class Match(BaseModel):
    invoice_ids: list[int]


class Summary(BaseModel):
    summary: str


def _do(sandbox, actor, fn, *args):
    with db(sandbox) as c:
        try:
            out = fn(c, actor or "anonymous", *args)
            c.commit()
            return out
        except actions.ActionError as e:
            raise HTTPException(400, str(e))


@app.post("/invoices/{iid}/approve")
def approve(iid: int, body: Approve, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.approve_invoice, iid, body.approved_amount, body.note)


@app.post("/invoices/{iid}/hold")
def hold(iid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.hold_invoice, iid, body.reason, body.note)


@app.post("/invoices/{iid}/reject")
def reject(iid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.reject_invoice, iid, body.reason, body.note)


@app.post("/invoices/{iid}/dispute")
def dispute(iid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.dispute_invoice, iid, body.reason, body.note)


@app.post("/invoices/{iid}/link_po")
def link(iid: int, body: LinkPO, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.link_po, iid, body.po_number)


@app.post("/bank_transactions/{tid}/match")
def match(tid: int, body: Match, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.match_bank_transaction, tid, body.invoice_ids)


@app.post("/bank_transactions/{tid}/flag")
def flag_txn(tid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.flag_bank_transaction, tid, body.reason)


@app.post("/vendors/{vid}/flag")
def flag_vendor(vid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.flag_vendor, vid, body.reason)


@app.post("/exceptions/{xid}/resolve")
def resolve(xid: int, body: Summary, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.resolve_exception, xid, body.summary)


@app.post("/exceptions/{xid}/escalate")
def escalate(xid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    return _do(x_sandbox, x_actor, actions.escalate_exception, xid, body.reason)
