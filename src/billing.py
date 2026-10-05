"""Monthly statement from the billing ledger.

The ledger is the meter: one row per worked exception, outcome 'verified' (billable, with the verifier's
evidence) or 'routed_to_human' (free). The statement is what the customer receives and what we reconcile
our own model bills against. Every billed line carries the exception id and a hash of its evidence so a
dispute can be settled by replaying that one case.
"""
import hashlib
import json
import pathlib

from . import policy


def statement(db, period=None, discovery_credit=0.0, pricing_path="out/pricing.json", out_dir="out"):
    conn = policy.connect(db)
    settings = policy.settings(conn)
    if not period:
        period = (conn.execute("select max(substr(ts,1,7)) from billing_ledger").fetchone()[0] or "")
    rows = [dict(r) for r in conn.execute("select * from billing_ledger where substr(ts,1,7)=? order by id", (period,))]
    ytd = {r["kind"]: r["n"] for r in conn.execute(
        "select kind, count(*) n from billing_ledger where outcome='verified' and substr(ts,1,4)=? group by kind", (period[:4],))}
    pricing = json.loads(pathlib.Path(pricing_path).read_text()) if pathlib.Path(pricing_path).exists() else {"workflows": {}, "assumptions": {}}
    ops_allow = float(pricing.get("assumptions", {}).get("ops_allowance_per_outcome_usd", 0.0))

    wf = {}
    for r in rows:
        ev = json.loads(r["evidence"] or "{}")
        w = wf.setdefault(r["kind"], {"verified": 0, "routed_to_human": 0, "price": None, "amount": 0.0, "model_usd": 0.0, "lines": []})
        w["model_usd"] += float(ev.get("model_usd") or 0.0)
        if r["outcome"] == "verified":
            w["verified"] += 1
            w["amount"] = round(w["amount"] + r["price"], 2)
            w["price"] = r["price"]
            w["lines"].append({"exception_id": r["exception_id"], "ts": r["ts"], "price": r["price"],
                               "evidence_sha256": hashlib.sha256((r["evidence"] or "").encode()).hexdigest()[:16],
                               "expected": ev.get("expected"), "actual": ev.get("actual")})
        else:
            w["routed_to_human"] += 1
    for kind, w in wf.items():
        pw = pricing["workflows"].get(kind, {})
        w["annual_minimum"] = pw.get("proposed_annual_minimum_outcomes")
        w["ytd_verified"] = ytd.get(kind, 0)
        w["cogs_usd"] = round(w["model_usd"] * (1 + float(pricing.get("assumptions", {}).get("serve_overhead_on_model_spend", 0))) + ops_allow * w["verified"], 4)
        w["model_usd"] = round(w["model_usd"], 4)

    subtotal = round(sum(w["amount"] for w in wf.values()), 2)
    credit = round(min(discovery_credit, subtotal), 2)
    due = round(subtotal - credit, 2)
    cogs = round(sum(w["cogs_usd"] for w in wf.values()), 4)
    out = {"customer": settings.get("company"), "period": period, "workflows": wf, "subtotal_usd": subtotal,
           "discovery_credit_applied_usd": credit, "amount_due_usd": due,
           "ours": {"model_spend_usd": round(sum(w["model_usd"] for w in wf.values()), 4), "cost_to_serve_usd": cogs,
                    "gross_margin": round((subtotal - cogs) / subtotal, 3) if subtotal else None},
           "terms": {"billable_event": "verifier pass recorded in billing_ledger with evidence", "unfinished_work": "routed to customer staff, not billed",
                     "dispute": "cite the exception id; the case is replayed against the verifier the customer signed off at admission"}}
    out["markdown"] = render(out)
    pathlib.Path(out_dir).mkdir(exist_ok=True)
    pathlib.Path(out_dir, f"statement_{period}.json").write_text(json.dumps(out, indent=1))
    pathlib.Path(out_dir, f"statement_{period}.md").write_text(out["markdown"])
    return out


def render(o):
    L = [f"# Statement — {o['customer']} — {o['period']}", "",
         "| Workflow | Verified outcomes | Price | Amount | Routed to your staff (free) | YTD verified / annual minimum |", "|---|---|---|---|---|---|"]
    for k, w in o["workflows"].items():
        L.append(f"| {k} | {w['verified']} | ${w['price'] if w['price'] is not None else '—'} | ${w['amount']:.2f} | {w['routed_to_human']} | {w['ytd_verified']} / {w['annual_minimum'] or '—'} |")
    L += ["", f"Subtotal **${o['subtotal_usd']:.2f}** · discovery-run credit applied ${o['discovery_credit_applied_usd']:.2f} · **amount due ${o['amount_due_usd']:.2f}**", "",
          f"Billable event: {o['terms']['billable_event']}. Unfinished work: {o['terms']['unfinished_work']}. Disputes: {o['terms']['dispute']}.", "",
          "## Evidence (one line per billed outcome)", "", "| Exception | When | Price | Verified end state | Evidence hash |", "|---|---|---|---|---|"]
    for k, w in o["workflows"].items():
        for l in w["lines"]:
            exp = l.get("expected") or {}
            state = ", ".join(f"{a}={b}" for a, b in exp.items() if a not in ("entity", "id"))
            L.append(f"| #{l['exception_id']} ({k}) | {l['ts'][:16]} | ${l['price']} | {state} | `{l['evidence_sha256']}` |")
    r = o["ours"]
    L += ["", "## Our side (internal, not sent to the customer)", "",
          f"Model API spend this period ${r['model_spend_usd']} (every attempt, billed or not); cost to serve incl. overhead and operations allowance ${r['cost_to_serve_usd']}; "
          f"realised gross margin {r['gross_margin']:.0%}." if r["gross_margin"] is not None else "No billable activity this period.", ""]
    return "\n".join(L)


def ledger_export(period, out_dir="out"):
    """Tax/accounting export from the seats database: one CSV of USDC receipts (ordinary income at $1.00 FMV on receipt,
    Schedule C) and one of credits consumed per seat for the period (YYYY-MM)."""
    import csv
    from . import diner
    pathlib.Path(out_dir).mkdir(exist_ok=True)
    with diner._conn() as c:
        pays = [dict(r) for r in c.execute("select * from payments where substr(ts,1,7)=? order by ts", (period,))]
        use = [dict(r) for r in c.execute("select actor, count(*) claims, sum(credits) credits, sum(passed) passed from charges where substr(ts,1,7)=? group by actor order by actor", (period,))]
    p1 = pathlib.Path(out_dir, f"ledger_{period}_receipts.csv")
    with open(p1, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["received_at_utc", "payer", "amount_usdc", "usd_fair_value", "transaction_id", "network", "rail", "seat_actor", "credits_granted", "payment_key"])
        for r in pays:
            w.writerow([r["ts"], r["payer"], f"{r['amount_usd']:.6f}", f"{r['amount_usd']:.2f}", r["transaction_id"], r["network"], r["rail"], r["actor"], r["credits"], r["payment_key"]])
        w.writerow(["TOTAL", "", f"{sum(r['amount_usd'] for r in pays):.6f}", f"{sum(r['amount_usd'] for r in pays):.2f}", "", "", "", "", sum(r["credits"] for r in pays), f"{len(pays)} receipts"])
    p2 = pathlib.Path(out_dir, f"ledger_{period}_usage.csv")
    with open(p2, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seat_actor", "claims", "credits_consumed", "verdicts_passed"])
        for r in use:
            w.writerow([r["actor"], r["claims"], r["credits"], r["passed"]])
        w.writerow(["TOTAL", sum(r["claims"] for r in use), sum(r["credits"] or 0 for r in use), sum(r["passed"] or 0 for r in use)])
    return {"receipts_csv": str(p1), "usage_csv": str(p2), "receipts": len(pays), "usd_received": round(sum(r["amount_usd"] for r in pays), 6), "seats_active": len(use)}
