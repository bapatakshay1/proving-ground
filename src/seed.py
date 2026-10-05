"""Deterministic synthetic AP twin: 12 months of POs, receipts, invoices, payments,
and an exceptions queue with human-resolved history (pre/post state captured so any
case can be reopened for replay) plus an open backlog.

ponytail: synthetic stand-in for an open-source ERP; the API surface in twin_api.py is
the adapter boundary, so an Odoo/ERPNext backend would implement the same endpoints.
"""
import datetime as dt
import json
import pathlib
import random
import sqlite3

from . import actions, policy

HERE = pathlib.Path(__file__).parent
NOW = dt.date(2026, 10, 1)
START = dt.date(2025, 10, 1)
OPEN_FROM = dt.date(2026, 9, 1)
PER_KIND_RESOLVED, PER_KIND_OPEN, HELD_OUT = 600, 60, 20
CLEAN_INVOICES = 4000
DEVIATION_RATE = 0.03
MINUTES = {"price_mismatch": (5, 9), "quantity_mismatch": (7, 13), "possible_duplicate": (3, 6),
           "missing_po": (8, 15), "unmatched_payment": (9, 16), "vendor_bank_change": (4, 7)}

ADJ = ["Acme", "Northwind", "Blue Ridge", "Summit", "Harbor", "Pioneer", "Keystone", "Granite", "Cascade", "Beacon",
       "Ironwood", "Silverline", "Meridian", "Prairie", "Redwood", "Atlas", "Crestview", "Lakeside", "Falcon", "Orion"]
NOUN = ["Industrial Supply", "Packaging", "Logistics", "Office Systems", "Electrical", "Fasteners", "Chemicals",
        "Printing", "Facilities Services", "Components"]
SUFFIX = ["LLC", "Inc", "Co", "Ltd"]
PARTS = ["bracket", "valve", "cartridge", "pallet", "sensor", "cable", "filter", "gasket", "label roll", "bearing",
         "panel", "coupling", "housing", "adapter", "switch"]


def iso(d, h=9, m=0):
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=dt.timezone.utc).isoformat()


def rdate(rng, a, b):
    return a + dt.timedelta(days=rng.randint(0, (b - a).days))


class Gen:
    def __init__(self, conn, rng):
        self.c, self.rng = conn, rng
        self.po_seq = 10000
        self.inv_seq = {}
        self.vendors = []
        self.skus = []

    def vendor(self):
        return self.rng.choice(self.vendors)

    def new_po(self, v, lines, created, status="open"):
        self.po_seq += 1
        n = f"PO-{self.po_seq}"
        cur = self.c.execute("insert into purchase_orders(po_number,vendor_id,created_at,status) values(?,?,?,?)",
                             (n, v["id"], created.isoformat(), status))
        pid = cur.lastrowid
        for i, (sku, qty, price) in enumerate(lines, 1):
            self.c.execute("insert into po_lines values(?,?,?,?,?,?)", (pid, i, sku, self.desc[sku], qty, price))
        return {"id": pid, "po_number": n, "vendor_id": v["id"]}

    def receive(self, po, lines, fractions, when):
        for i, ((sku, qty, _), f) in enumerate(zip(lines, fractions), 1):
            q = qty if f >= 1 else max(1, int(qty * f)) if f > 0 else 0
            if q > 0:
                self.c.execute("insert into goods_receipts(po_id,line_no,qty_received,received_at) values(?,?,?,?)",
                               (po["id"], i, q, when.isoformat()))

    def new_invoice(self, v, po_number, lines, date, number=None, remit=None):
        if number is None:
            self.inv_seq[v["id"]] = self.inv_seq.get(v["id"], 1000) + 1
            number = f"INV-{v['id']:02d}-{self.inv_seq[v['id']]}"
        sub = round(sum(q * p for _, q, p in lines), 2)
        tax = round(sub * v["tax_rate"], 2)
        cur = self.c.execute(
            "insert into invoices(invoice_number,vendor_id,po_number,invoice_date,due_date,subtotal,tax,total,remit_to_account,status) "
            "values(?,?,?,?,?,?,?,?,?,'received')",
            (number, v["id"], po_number, date.isoformat(), (date + dt.timedelta(days=v["payment_terms_days"])).isoformat(),
             sub, tax, round(sub + tax, 2), remit or v["bank_account"]))
        iid = cur.lastrowid
        for i, (sku, q, p) in enumerate(lines, 1):
            self.c.execute("insert into invoice_lines values(?,?,?,?,?,?)", (iid, i, sku, q, p, round(q * p, 2)))
        return iid

    def rand_lines(self, n=None, exclude=()):
        n = n or self.rng.choice([1, 1, 2, 2, 3, 4])
        pool = [s for s in self.skus if s not in exclude]
        chosen = self.rng.sample(pool, n)
        return [(s, self.rng.choice([1, 2, 5, 10, 12, 24, 50, 100]), self.price[s]) for s in chosen]

    def bank_txn(self, v, amount, date, status="unreconciled"):
        cur = self.c.execute("insert into bank_transactions(txn_date,amount,counterparty,memo,status) values(?,?,?,?,?)",
                             (date.isoformat(), round(amount, 2), v["name"].upper() + self.rng.choice(["", "", " " + v["name"].split()[-1].upper()]),
                              f"ACH DEBIT {self.rng.randint(100000, 999999)}", status))
        return cur.lastrowid


def snapshot(conn, kind, eid):
    """Capture every row this exception's resolution may touch, so the case can be reopened."""
    snap = {"invoices": {}, "bank_transactions": {}, "vendors": {}}
    if kind == "unmatched_payment":
        txn = policy.row(conn, "bank_transactions", eid)
        snap["bank_transactions"][eid] = txn
        v, cands = policy.payment_candidates(conn, txn)
        for c in cands:
            snap["invoices"][c["id"]] = c
    else:
        inv = policy.row(conn, "invoices", eid)
        snap["invoices"][eid] = inv
        v = policy.row(conn, "vendors", inv["vendor_id"])
        snap["vendors"][v["id"]] = {"id": v["id"], "flagged": v["flagged"], "flag_reason": v["flag_reason"]}
    return snap


def restore(conn, snap):
    for table, rows in snap.items():
        for rid, r in rows.items():
            cols = [k for k in r if k != "id"]
            conn.execute(f"update {table} set " + ",".join(f"{k}=?" for k in cols) + " where id=?", [r[k] for k in cols] + [int(rid)])


def reopen(conn, exception_id):
    x = policy.row(conn, "exceptions", exception_id)
    restore(conn, json.loads(x["pre_state"]))
    conn.execute("update exceptions set status='open', resolved_at=NULL, resolved_by=NULL, resolution=NULL where id=?", (exception_id,))
    conn.commit()


def human_resolve(conn, rng, kind, eid, xid, exp, when, clerk, deviate):
    """Apply the clerk's resolution via the same actions the agents use."""
    ts = iso(when, rng.randint(8, 17), rng.randint(0, 59))
    if kind == "unmatched_payment":
        if exp["status"] == "reconciled" and not deviate:
            actions.match_bank_transaction(conn, clerk, eid, exp["matched_invoice_ids"], ts)
            summary = f"matched to invoice(s) {exp['matched_invoice_ids']}"
        else:
            actions.flag_bank_transaction(conn, clerk, eid, "no_matching_invoice" if not deviate else "no_matching_invoice (manager override: vendor dispute)", ts)
            summary = "no matching invoice; flagged for treasury" if not deviate else "flagged for treasury (manager override: vendor dispute)"
    elif kind == "vendor_bank_change":
        inv = policy.row(conn, "invoices", eid)
        actions.hold_invoice(conn, clerk, eid, "bank_detail_mismatch", "remit-to differs from master; vendor callback required", ts)
        actions.flag_vendor(conn, clerk, inv["vendor_id"], "bank detail change on invoice " + str(eid), ts)
        summary = "held for bank detail verification; vendor flagged"
    elif exp["status"] == "approved" and not deviate:
        if kind == "missing_po":
            actions.link_po(conn, clerk, eid, exp["po_number"], ts)
        actions.approve_invoice(conn, clerk, eid, exp["approved_amount"], f"approved per AP-POL-7 ({kind})", ts)
        summary = f"approved {exp['approved_amount']}"
    elif exp["status"] == "approved" and deviate:
        inv = policy.row(conn, "invoices", eid)
        actions.approve_invoice(conn, clerk, eid, inv["total"], "manager override: approved in full", ts)
        summary = "approved in full (manager override)"
    elif exp["status"] == "disputed":
        if deviate:
            inv = policy.row(conn, "invoices", eid)
            actions.approve_invoice(conn, clerk, eid, inv["total"], "manager override: strategic vendor, approved despite variance", ts)
            summary = "approved despite variance (manager override)"
        else:
            actions.dispute_invoice(conn, clerk, eid, f"price variance of ${exp['_variance']:.2f} above tolerance", None, ts)
            summary = "disputed with vendor: price variance"
    elif exp["status"] == "rejected":
        if deviate:
            inv = policy.row(conn, "invoices", eid)
            actions.approve_invoice(conn, clerk, eid, inv["total"], "manager override: treated as reissued invoice", ts)
            summary = "approved (manager override)"
        else:
            actions.reject_invoice(conn, clerk, eid, f"duplicate of invoice {exp['_original_id']}", None, ts)
            summary = f"rejected as duplicate of invoice {exp['_original_id']}"
    elif exp["status"] == "on_hold":
        actions.hold_invoice(conn, clerk, eid, exp["hold_reason"], None, ts)
        summary = f"on hold: {exp['hold_reason']}"
    else:
        raise ValueError(exp)
    actions.resolve_exception(conn, clerk, xid, summary, ts)


def build(path, seed=7):
    path = pathlib.Path(path)
    if path.exists():
        path.unlink()
    conn = policy.connect(path)
    conn.executescript((HERE / "schema.sql").read_text())
    rng = random.Random(seed)
    for k, v in {"loaded_hourly_cost": "54", "price_tolerance_pct": "2.0", "price_tolerance_abs": "25", "seed": str(seed),
                 "as_of": NOW.isoformat(), "company": "Harbor Point Manufacturing"}.items():
        conn.execute("insert into settings values(?,?)", (k, v))

    g = Gen(conn, rng)
    names = set()
    while len(names) < 40:
        names.add(f"{rng.choice(ADJ)} {rng.choice(NOUN)} {rng.choice(SUFFIX)}")
    for i, n in enumerate(sorted(names), 1):
        conn.execute("insert into vendors(id,name,bank_account,tax_rate,payment_terms_days) values(?,?,?,?,?)",
                     (i, n, f"US{rng.randint(10**11, 10**12-1)}", rng.choice([0.0, 0.05, 0.07, 0.0825]), rng.choice([30, 30, 45, 60])))
    g.vendors = [dict(r) for r in conn.execute("select * from vendors")]
    g.skus = [f"SKU-{i:04d}" for i in range(1, 301)]
    g.price = {s: round(rng.uniform(4, 600), 2) for s in g.skus}
    g.desc = {s: f"{rng.choice(PARTS)} {rng.choice(['std', 'HD', 'XL', 'mini'])}" for s in g.skus}

    def clerk():
        return f"clerk-{rng.randint(1, 6)}"

    # ---- clean invoices: PO -> receipt -> invoice -> approved -> (paid via reconciled bank debit) ----
    for _ in range(CLEAN_INVOICES):
        v = g.vendor()
        d = rdate(rng, START, NOW - dt.timedelta(days=3))
        lines = g.rand_lines()
        po = g.new_po(v, lines, d - dt.timedelta(days=rng.randint(10, 30)), "closed")
        g.receive(po, lines, [1] * len(lines), d - dt.timedelta(days=rng.randint(1, 8)))
        iid = g.new_invoice(v, po["po_number"], lines, d)
        inv = policy.row(conn, "invoices", iid)
        actions.approve_invoice(conn, clerk(), iid, inv["total"], "3-way match", iso(d + dt.timedelta(days=1)))
        due = dt.date.fromisoformat(inv["due_date"])
        if due <= NOW - dt.timedelta(days=5):
            tid = g.bank_txn(v, inv["total"], due + dt.timedelta(days=rng.randint(-2, 2)))
            actions.match_bank_transaction(conn, "auto-recon", tid, [iid], iso(due))

    # ---- exception scenarios ----
    open_skus = {}

    def make_case(kind, date):
        v = g.vendor()
        lines = g.rand_lines()
        if kind == "price_mismatch":
            po = g.new_po(v, lines, date - dt.timedelta(days=rng.randint(10, 30)), "closed")
            g.receive(po, lines, [1] * len(lines), date - dt.timedelta(days=rng.randint(1, 8)))
            po_value = sum(q * p for _, q, p in lines)
            within = rng.random() < 0.55
            inv_lines = list(lines)
            k = rng.randrange(len(lines))
            sku, q, p = lines[k]
            if within:
                target = rng.uniform(-0.01 * po_value, min(25, 0.02 * po_value)) if rng.random() < 0.85 else rng.uniform(-30, -1)
            else:
                target = rng.uniform(max(26, 0.03 * po_value), max(40, 0.15 * po_value))
            newp = round(p + target / q, 2)
            if newp <= 0:
                newp = round(p * 1.1, 2)
            inv_lines[k] = (sku, q, newp)
            return v, g.new_invoice(v, po["po_number"], inv_lines, date), "invoice"
        if kind == "quantity_mismatch":
            po = g.new_po(v, lines, date - dt.timedelta(days=rng.randint(10, 30)), "open")
            if rng.random() < 0.70:
                fr = [rng.choice([0, 0.3, 0.5, 0.75]) for _ in lines]
                if all(f == 0 for f in fr):
                    fr[0] = 0.5
                g.receive(po, lines, fr, date - dt.timedelta(days=rng.randint(1, 8)))
            return v, g.new_invoice(v, po["po_number"], lines, date), "invoice"
        if kind == "possible_duplicate":
            po = g.new_po(v, lines, date - dt.timedelta(days=rng.randint(20, 40)), "closed")
            g.receive(po, lines, [1] * len(lines), date - dt.timedelta(days=rng.randint(12, 20)))
            d0 = date - dt.timedelta(days=rng.randint(2, 20))
            orig = g.new_invoice(v, po["po_number"], lines, d0)
            o = policy.row(conn, "invoices", orig)
            actions.approve_invoice(conn, clerk(), orig, o["total"], "3-way match", iso(d0 + dt.timedelta(days=1)))
            if rng.random() < 0.55:
                return v, g.new_invoice(v, po["po_number"], lines, date, number=o["invoice_number"]), "invoice"
            po2 = g.new_po(v, lines, date - dt.timedelta(days=rng.randint(10, 30)), "closed")
            g.receive(po2, lines, [1] * len(lines), date - dt.timedelta(days=rng.randint(1, 8)))
            if rng.random() < 0.5:
                return v, g.new_invoice(v, po2["po_number"], lines, d0 + dt.timedelta(days=rng.randint(0, 3))), "invoice"
            lines2 = g.rand_lines(exclude=[s for s, _, _ in lines])
            po3 = g.new_po(v, lines2, date - dt.timedelta(days=rng.randint(10, 30)), "closed")
            g.receive(po3, lines2, [1] * len(lines2), date - dt.timedelta(days=rng.randint(1, 8)))
            return v, g.new_invoice(v, po3["po_number"], lines2, date, number=o["invoice_number"]), "invoice"
        if kind == "missing_po":
            # SKUs on this vendor's open POs stay unique, so "exactly one matching open PO" is never ambiguous
            excl = open_skus.setdefault(v["id"], set())
            lines = g.rand_lines(rng.choice([1, 2]), exclude=excl)
            excl.update(s for s, _, _ in lines)
            if rng.random() < 0.60:
                po = g.new_po(v, lines, date - dt.timedelta(days=rng.randint(10, 30)), "open")
                g.receive(po, lines, [1] * len(lines), date - dt.timedelta(days=rng.randint(1, 8)))
                ref = None if rng.random() < 0.7 else f"PO-{rng.randint(90000, 99999)}"
            else:
                decoy = g.rand_lines(rng.choice([1, 2]), exclude=excl)
                excl.update(s for s, _, _ in decoy)
                g.new_po(v, decoy, date - dt.timedelta(days=rng.randint(10, 30)), "open")
                ref = None
            return v, g.new_invoice(v, ref, lines, date), "invoice"
        if kind == "unmatched_payment":
            def approved_invoice(d, ls):
                po = g.new_po(v, ls, d - dt.timedelta(days=rng.randint(10, 30)), "closed")
                g.receive(po, ls, [1] * len(ls), d - dt.timedelta(days=rng.randint(1, 8)))
                iid = g.new_invoice(v, po["po_number"], ls, d)
                actions.approve_invoice(conn, clerk(), iid, policy.row(conn, "invoices", iid)["total"], "3-way match", iso(d + dt.timedelta(days=1)))
                return iid
            d1 = date - dt.timedelta(days=rng.randint(25, 45))
            a = approved_invoice(d1, lines)
            total_a = policy.row(conn, "invoices", a)["total"]
            r = rng.random()
            if r < 0.60:
                amount = total_a
            elif r < 0.75:
                b = approved_invoice(d1 + dt.timedelta(days=rng.randint(0, 5)), g.rand_lines(exclude=[s for s, _, _ in lines]))
                amount = total_a + policy.row(conn, "invoices", b)["total"]
            else:
                amount = round(total_a * rng.choice([0.5, 1.5, 2.0]) + rng.uniform(1, 99), 2)
            return v, g.bank_txn(v, amount, date), "bank_transaction"
        if kind == "vendor_bank_change":
            po = g.new_po(v, lines, date - dt.timedelta(days=rng.randint(10, 30)), "closed")
            g.receive(po, lines, [1] * len(lines), date - dt.timedelta(days=rng.randint(1, 8)))
            return v, g.new_invoice(v, po["po_number"], lines, date, remit=f"US{rng.randint(10**11, 10**12-1)}"), "invoice"
        raise ValueError(kind)

    for kind in policy.KINDS:
        dates = [rdate(rng, START, dt.date(2026, 8, 20)) for _ in range(PER_KIND_RESOLVED)] + \
                [rdate(rng, OPEN_FROM, NOW - dt.timedelta(days=4)) for _ in range(PER_KIND_OPEN)]
        xids = []
        for date in sorted(dates):
            v, eid, etype = make_case(kind, date)
            opened = date + dt.timedelta(days=rng.randint(1, 3))
            exp = policy.expected(conn, kind, eid)
            pre = snapshot(conn, kind, eid)
            cur = conn.execute("insert into exceptions(kind,entity_type,entity_id,opened_at,status,pre_state,truth) values(?,?,?,?,'open',?,?)",
                               (kind, etype, eid, iso(opened), json.dumps(pre), json.dumps(exp)))
            xid = cur.lastrowid
            if date < OPEN_FROM:
                when = opened + dt.timedelta(days=rng.randint(0, 4))
                # a clerk override only counts as a deviation where it changes the outcome
                partial = exp["status"] == "approved" and etype == "invoice" and exp["approved_amount"] < policy.row(conn, "invoices", eid)["total"] - 0.01
                deviate = rng.random() < DEVIATION_RATE and kind not in ("missing_po", "vendor_bank_change") \
                    and (exp["status"] in ("disputed", "rejected", "reconciled") or partial)
                human_resolve(conn, rng, kind, eid, xid, exp, when, clerk(), deviate)
                lo, hi = MINUTES[kind]
                conn.execute("update exceptions set handling_minutes=?, post_state=? where id=?",
                             (round(rng.uniform(lo, hi) * (1.6 if deviate else 1.0), 1), json.dumps(snapshot(conn, kind, eid)), xid))
                xids.append(xid)
        for xid in rng.sample(xids, HELD_OUT):
            conn.execute("update exceptions set held_out=1 where id=?", (xid,))
    conn.commit()
    return summary(conn)


def summary(conn):
    out = {"vendors": conn.execute("select count(*) from vendors").fetchone()[0],
           "purchase_orders": conn.execute("select count(*) from purchase_orders").fetchone()[0],
           "invoices": conn.execute("select count(*) from invoices").fetchone()[0],
           "bank_transactions": conn.execute("select count(*) from bank_transactions").fetchone()[0],
           "exceptions": {}}
    for r in conn.execute("select kind, status, count(*) n, sum(held_out) h from exceptions group by kind, status"):
        out["exceptions"].setdefault(r["kind"], {})[r["status"]] = {"n": r["n"], "held_out": r["h"] or 0}
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(build(sys.argv[1] if len(sys.argv) > 1 else "out/twin.db"), indent=1))
