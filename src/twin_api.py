"""The sealed twin. Agents reach the system of record only through this API.

Headers:
  X-Sandbox: <name>   select an isolated copy of the twin (out/sandboxes/<name>.db); default = base twin
  X-Actor:   <id>     recorded on every write in the audit log

Hidden from the API on purpose: exceptions.pre_state/post_state/truth/held_out (harness-only columns).
"""
import datetime as dt
import json
import math
import os
import pathlib
import shutil
import sqlite3

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import actions, agents, diner, policy, x402

BASE_DB = pathlib.Path(os.environ.get("PG_DB", "out/twin.db"))
SANDBOX_DIR = pathlib.Path(os.environ.get("PG_SANDBOXES", "out/sandboxes"))
app = FastAPI(title="Proving Ground twin", version="0.2")


def _safe(name):
    return bool(name) and name.replace("-", "").replace("_", "").isalnum()


def _load_keys():
    """PG_API_KEYS="token:actor,token2:actor2". When set, every request needs a bearer token; the actor
    recorded in the audit log comes from the token, never from the client. Each actor gets a private sandbox."""
    out = {}
    for pair in os.environ.get("PG_API_KEYS", "").split(","):
        if ":" in pair:
            tok, actor = pair.split(":", 1)
            if _safe(actor.strip()) and len(tok.strip()) >= 16:
                out[tok.strip()] = actor.strip()
    return out


API_KEYS = _load_keys()
# hosted mode: bearer auth, private sandboxes, metering. Dev mode (the harness) has none of it.
HOSTED = bool(API_KEYS) or os.environ.get("PG_HOSTED") == "1"
ADMIN_KEY = os.environ.get("PG_ADMIN_KEY", "")
OPEN_PATHS = {"/", "/health", "/docs", "/openapi.json", "/schema", "/policy", "/llms.txt", "/.well-known/agent-card.json", "/seat", "/stats/exceptions",
              "/pricing", "/.well-known/mcp/server-card.json", "/.well-known/mcp-server-card"}


def _ensure_sandbox(actor):
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    if not (SANDBOX_DIR / f"{actor}.db").exists():
        shutil.copy(BASE_DB, SANDBOX_DIR / f"{actor}.db")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if not HOSTED or path in OPEN_PATHS or (path.startswith("/seats/") and path.endswith("/credit")):
        return await call_next(request)
    raw = request.headers.get("authorization", "")
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    actor, metered = API_KEYS.get(token), False
    if not actor and token:
        actor, metered = diner.lookup(token), True
    if not actor:
        return JSONResponse({"detail": "missing or invalid bearer token", "get_a_seat": "POST /seat", "menu": "GET /"}, status_code=401)
    sandbox = actor if path == "/mcp" else request.headers.get("x-sandbox")
    if sandbox and sandbox != actor:
        return JSONResponse({"detail": f"your sandbox is '{actor}'; other sandboxes are not visible"}, status_code=403)
    if request.method != "GET" and sandbox != actor:
        return JSONResponse({"detail": f"writes go to your private sandbox: send X-Sandbox: {actor}"}, status_code=403)
    if sandbox == actor:
        _ensure_sandbox(actor)
    headers = [(k, v) for k, v in request.scope["headers"] if k not in (b"x-actor", b"x-seat", b"x-sandbox")]
    headers += [(b"x-actor", actor.encode()), (b"x-seat", b"1" if metered else b"0")]
    if sandbox:
        headers.append((b"x-sandbox", sandbox.encode()))
    request.scope["headers"] = headers
    return await call_next(request)


def _base_url(request: Request):
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('x-forwarded-host', request.headers.get('host', 'localhost'))}"


def _public_url(request: Request):
    """The URL a payer sees (behind Railway's TLS terminator the app itself sees http)."""
    return _base_url(request) + request.url.path


def _menu(request: Request):
    with db(None) as c:
        st = stats_for(c)
    m = diner.menu(_base_url(request), st)
    methods = _topup_methods(_base_url(request))
    if methods:
        m["pricing"]["top_up"] = methods
        m["links"]["pricing"] = f"{_base_url(request)}/pricing"
    return m


@app.get("/")
def front_door(request: Request):
    m = _menu(request)
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept.split(",")[0]:
        return HTMLResponse(diner.html(m))
    return m


@app.get("/llms.txt")
def llms(request: Request):
    return PlainTextResponse(diner.llms_txt(_menu(request)), media_type="text/markdown")


@app.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    return diner.agent_card(_base_url(request))


def server_card(base_url):
    """MCP Server Card (SEP-2127, draft): catalog metadata only - tools are discovered live over /mcp."""
    return {"serverInfo": {"name": "proving-ground", "version": "0.3.0"}, "protocolVersion": diner.PROTOCOL,
            "description": "Sealed accounts-payable twin: work real exceptions, get a code-verified pass/fail per case and a proof packet.",
            "transport": {"type": "streamable-http", "url": f"{base_url}/mcp"},
            "authentication": {"type": "bearer", "obtain": f"POST {base_url}/seat (no account; returns a token, a private sandbox and free credits)"},
            "capabilities": {"tools": {}}, "pricing": f"{base_url}/pricing", "homepage": base_url, "openapi": f"{base_url}/openapi.json"}


@app.get("/.well-known/mcp/server-card.json")
@app.get("/.well-known/mcp-server-card")
def mcp_server_card(request: Request):
    return server_card(_base_url(request))


@app.get("/pricing")
def pricing(request: Request):
    base = _base_url(request)
    out = {"unit": "credit", "reads": "free", "price_credits_per_claim": diner.PRICE_CREDITS, "free_credits_per_seat": diner.FREE_CREDITS,
           "credit_usd": x402.CREDIT_USD if x402.enabled() else None, "top_up": _topup_methods(base)}
    if x402.enabled():
        out["x402"] = {"network": x402.NETWORK, "asset": x402.ASSETS[x402.NETWORK][0], "payTo": x402.PAY_TO, "scheme": "exact",
                       "facilitator": x402.FACILITATOR, "example_402": x402.payment_required(diner.PRICE_CREDITS, f"{base}/exceptions/{{id}}/resolve", "metered claim")}
    return out


def _topup_methods(base_url):
    methods = list(diner.TOPUP)
    if x402.enabled():
        methods.insert(0, x402.topup_method(base_url, diner.PRICE_CREDITS))
    return methods


class SeatRequest(BaseModel):
    name: str | None = None


@app.post("/seat", status_code=201)
def take_seat(request: Request, body: SeatRequest | None = None):
    """Self-serve seating: a bearer token (shown once), a private sandbox, and free credits."""
    if not HOSTED:
        raise HTTPException(400, "seating is only available in hosted mode (set PG_API_KEYS or PG_HOSTED=1)")
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?"))
    if not diner.rate_ok(ip):
        raise HTTPException(429, "too many seats from this address; try again later")
    try:
        token, actor, credits = diner.create_seat((body.name if body else None), reserved=set(API_KEYS.values()))
    except ValueError as e:
        raise HTTPException(503, str(e))
    _ensure_sandbox(actor)
    return {"token": token, "actor": actor, "sandbox": actor, "credits": credits, "price_credits_per_claim": diner.PRICE_CREDITS,
            "how": f"send 'Authorization: Bearer <token>' and 'X-Sandbox: {actor}' on every call, or point an MCP client at /mcp with the token. "
                   "Reads are free; each resolve/escalate costs credits and returns a verdict. Keep the token: it is not shown again."}


@app.get("/me")
def me(x_actor: str | None = Header(None), x_seat: str | None = Header(None)):
    out = {"actor": x_actor, "auth_enabled": HOSTED, "sandbox": x_actor if HOSTED else None,
           "how": "send X-Sandbox: <your actor> on every call to work in your private copy; POST /sandbox/reset to start over" if HOSTED else "dev mode: no auth"}
    if x_seat == "1":
        s = diner.seat(x_actor) or {}
        out.update({"metered": True, "credits": s.get("credits"), "spent": s.get("spent"), "plan": s.get("plan"), "price_credits_per_claim": diner.PRICE_CREDITS})
    elif HOSTED:
        out["metered"] = False
    return out


class Credit(BaseModel):
    credits: int
    plan: str | None = None


@app.post("/seats/{actor}/credit")
def credit_seat(actor: str, body: Credit, authorization: str | None = Header(None)):
    """Admin: what a payment webhook calls once money has moved. Bearer PG_ADMIN_KEY."""
    if not ADMIN_KEY or (authorization or "")[7:].strip() != ADMIN_KEY or not (authorization or "").lower().startswith("bearer "):
        raise HTTPException(403, "admin key required")
    if body.credits <= 0:
        raise HTTPException(400, "credits must be positive")
    out = diner.credit(actor, body.credits, body.plan)
    if not out:
        raise HTTPException(404, "no such seat")
    return out


VERIF_DDL = "create table if not exists verifications(exception_id integer primary key, actor text, ts text, passed integer, result text, detail text)"


def _wilson(p, n, z=1.96):
    if n == 0:
        return [0.0, 0.0]
    c = p + z * z / (2 * n); s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)); d = 1 + z * z / n
    return [round((c - s) / d, 3), round((c + s) / d, 3)]


def record_verdict(c, xid, actor):
    """The meter. Runs the verifier on a worked exception and records the verdict once; later reads return
    the first verdict, so nobody can read it, patch the record and try again. Public result carries failure
    classes only, never the expected state; the full detail stays server-side for audits and disputes."""
    c.execute(VERIF_DDL)
    x = c.execute("select * from exceptions where id=?", (xid,)).fetchone()
    if not x:
        raise HTTPException(404, "not found")
    prev = c.execute("select result from verifications where exception_id=?", (xid,)).fetchone()
    if prev:
        return {**json.loads(prev["result"]), "recorded_earlier": True}
    if x["status"] == "open":
        raise HTTPException(409, "exception is still open; resolve or escalate it first")
    if x["status"] == "escalated":
        v, classes = {"passed": False, "failures": ["escalated"], "expected": None, "actual": None}, ["escalated"]
    else:
        v = policy.verify(c, x["kind"], x["entity_id"], actor=actor, exception_id=xid)
        classes = sorted({f.split(":")[0] for f in v["failures"]})
    public = {"exception_id": xid, "kind": x["kind"], "passed": bool(v["passed"]), "failure_classes": classes,
              "verified_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
    c.execute("insert into verifications(exception_id,actor,ts,passed,result,detail) values(?,?,?,?,?,?)",
              (xid, actor, public["verified_at"], int(v["passed"]), json.dumps(public), json.dumps(v)))
    return public


@app.post("/exceptions/{xid}/verify")
def verify_outcome(xid: int, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    if not x_sandbox:
        raise HTTPException(400, "verification runs against a sandbox: send X-Sandbox")
    with db(x_sandbox) as c:
        out = record_verdict(c, xid, x_actor)
        c.commit()
        return out


@app.get("/proof")
def proof(x_sandbox: str | None = Header(None), x_actor: str | None = Header(None)):
    """What a visiting agent walks away with: pass rate per workflow on the cases it worked, with the
    customer's current unit cost next to it."""
    if not x_sandbox:
        raise HTTPException(400, "send X-Sandbox")
    with db(x_sandbox) as c:
        c.execute(VERIF_DDL)
        rate = float(policy.settings(c)["loaded_hourly_cost"])
        cost = {r["kind"]: round((r["m"] or 0) / 60 * rate, 2) for r in c.execute(
            "select kind, avg(case when status='resolved' and resolved_by like 'clerk-%' then handling_minutes end) m from exceptions group by kind")}
        out = {}
        for r in c.execute("select v.exception_id, v.passed, v.ts, v.result, e.kind from verifications v join exceptions e on e.id=v.exception_id where v.actor=? order by v.ts", (x_actor,)):
            w = out.setdefault(r["kind"], {"attempted": 0, "passed": 0, "current_unit_cost_usd": cost.get(r["kind"]), "cases": []})
            w["attempted"] += 1; w["passed"] += r["passed"]
            w["cases"].append({"exception_id": r["exception_id"], "passed": bool(r["passed"]), "failure_classes": json.loads(r["result"])["failure_classes"]})
        for w in out.values():
            w["pass_rate"] = round(w["passed"] / w["attempted"], 3); w["ci95"] = _wilson(w["pass_rate"], w["attempted"])
        return {"actor": x_actor, "sandbox": x_sandbox, "workflows": out,
                "note": "pass = the verifier found the system of record in the state the policy requires, with no writes outside the task. "
                        "Pricing in production is 50% of current_unit_cost_usd per verified outcome; a workflow is offered once it clears 90% on held-out history."}


@app.post("/sandbox/reset")
def sandbox_reset(x_actor: str | None = Header(None), x_sandbox: str | None = Header(None)):
    if not HOSTED:
        raise HTTPException(400, "sandbox reset is only available when auth is enabled")
    shutil.copy(BASE_DB, SANDBOX_DIR / f"{x_actor}.db")
    return {"ok": True, "sandbox": x_actor, "reset_from": "base twin"}


def db(sandbox):
    if sandbox:
        if not _safe(sandbox):
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
        "meter": ["POST /exceptions/{id}/verify (after resolve/escalate; recorded once)", "GET /proof (your pass rate per workflow)",
                  "GET /me", "POST /sandbox/reset"],
    }


@app.get("/policy")
def get_policy():
    return {"policy": (pathlib.Path(__file__).parent / "policy.md").read_text()}


@app.get("/settings")
def get_settings(x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        return policy.settings(c)


def stats_for(c):
    rate = float(policy.settings(c)["loaded_hourly_cost"])
    out = []
    for r in c.execute("select kind, count(*) n, sum(status='open') open, sum(status='resolved') resolved, "
                       "avg(case when status='resolved' then handling_minutes end) mins from exceptions group by kind order by n desc"):
        months = {m["m"]: m["n"] for m in c.execute("select substr(opened_at,1,7) m, count(*) n from exceptions where kind=? group by m", (r["kind"],))}
        mins = r["mins"] or 0
        out.append({"kind": r["kind"], "entity_type": policy.ENTITY[r["kind"]], "total": r["n"], "open": r["open"], "resolved": r["resolved"],
                    "by_month": months, "avg_handling_minutes": round(mins, 1), "current_unit_cost_usd": round(mins / 60 * rate, 2)})
    return {"loaded_hourly_cost": rate, "as_of": policy.settings(c).get("as_of"), "kinds": out}


@app.get("/stats/exceptions")
def stats(x_sandbox: str | None = Header(None)):
    with db(x_sandbox) as c:
        return stats_for(c)


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
    with db(x_sandbox) as c:
        total = c.execute("select count(*) from (" + q + ")", a).fetchone()[0]
        rows = [dict(r) for r in c.execute(q + " order by invoice_date desc, id desc limit ?", a + [limit])]
    return {"results": rows, "total": total, "returned": len(rows), "truncated": total > len(rows)}


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
    with db(x_sandbox) as c:
        total = c.execute("select count(*) from (" + q + ")", a).fetchone()[0]
        out = []
        for r in c.execute(q + " order by p.created_at desc limit ?", a + [limit]):
            po = po_view(c, r["po_number"])
            po["lines"] = [{k: l[k] for k in ("sku", "qty_ordered", "unit_price", "qty_received", "qty_invoiced")} for l in po["lines"]]
            out.append(po)
    return {"results": out, "total": total, "returned": len(out), "truncated": total > len(out)}


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
        return {"results": like, "total": len(like), "returned": len(like), "truncated": False}


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


class PaymentRequired(Exception):
    def __init__(self, body, headers=None):
        self.body, self.headers = body, headers or {}


def _required(actor, resource, error=None):
    """The 402: the diner's JSON body plus, when x402 is configured, the spec'd PAYMENT-REQUIRED header (base64 JSON)."""
    body = diner.payment_required(actor)
    if error:
        body["detail"] = f"payment rejected: {error}"
    body["top_up"]["methods"] = _topup_methods(resource.rsplit("/exceptions", 1)[0] if "/exceptions" in resource else resource.rsplit("/seats", 1)[0])
    # HTTP header names are case-insensitive: the spec'd PAYMENT-REQUIRED (base64 PaymentRequired) replaces the
    # plain "Payment-Required: true" flag rather than sitting next to it, or clients see two values for one name.
    if x402.enabled():
        pr = x402.payment_required(diner.PRICE_CREDITS, resource, "Proving Ground: one metered claim (resolve/escalate) with verdict", error)
        body["x402"] = pr
        headers = {"PAYMENT-REQUIRED": x402.b64(pr)}
    else:
        headers = {"Payment-Required": "true"}
        if error:
            body["error"] = error
    return PaymentRequired(body, headers)


def settle_payment(actor, payment_header, resource, min_credits=None):
    """Turn a PAYMENT-SIGNATURE into credits: verify, settle, credit once per payment. Returns the settlement info."""
    if not x402.enabled():
        raise _required(actor, resource, "x402 is not enabled on this server")
    try:
        payload = x402.parse_payment(payment_header)
        req = x402.requirements(min_credits or diner.PRICE_CREDITS, resource, "Proving Ground credits")
        amount = x402.check_accepted(payload, req)
    except x402.PaymentError as e:
        raise _required(actor, resource, str(e))
    key = x402.payment_key(payload)
    with diner._conn() as c:
        seen = c.execute("select credits, payer, transaction_id from payments where payment_key=?", (key,)).fetchone()
    if seen:  # replayed header: honour the earlier settlement, never settle or credit twice
        return {"payment_key": key[:16], "credits": seen["credits"], "payer": seen["payer"], "transaction": seen["transaction_id"], "replayed": True}
    ok, info = x402.verify_and_settle(payload, {**req, "amount": payload["accepted"]["amount"]})
    if not ok:
        raise _required(actor, resource, f"payment {info['stage']} failed: {info['reason']}")
    usd = amount / 1_000_000
    credits = int(usd / x402.CREDIT_USD + 1e-9)
    diner.credit_for_payment(actor, key, "x402", usd, credits, info)
    return {"payment_key": key[:16], "credits": credits, "amount_usd": usd, **info}


def claim(sandbox, actor, metered, fn, xid, arg, payment=None, resource=""):
    """A claim (resolve/escalate). Hosted mode: the verifier runs at once and the verdict rides in the response;
    seat actors pay the price in credits, checked before the state changes so an unpaid claim changes nothing.
    A PAYMENT-SIGNATURE on the request tops the seat up first (x402), then the claim proceeds."""
    settled = None
    if metered and payment:
        settled = settle_payment(actor, payment, resource)
    if metered and not diner.can_pay(actor):
        raise _required(actor, resource)
    out = _do(sandbox, actor, fn, xid, arg)
    if settled:
        out["payment"] = settled
    if HOSTED:
        with db(sandbox) as c:
            out["verdict"] = record_verdict(c, xid, actor)
            c.commit()
        if metered:
            bal = diner.charge(actor, xid, out["verdict"].get("passed"))
            out["bill"] = {"charged_credits": diner.PRICE_CREDITS, "credits_left": bal["credits"], "spent": bal["spent"]}
    return out


def _claim_route(request, x_sandbox, x_actor, x_seat, payment, fn, xid, arg):
    try:
        out = claim(x_sandbox, x_actor, x_seat == "1", fn, xid, arg, payment, _public_url(request))
    except PaymentRequired as e:
        return JSONResponse(e.body, status_code=402, headers=e.headers)
    if out.get("payment"):
        return JSONResponse(out, headers={"PAYMENT-RESPONSE": x402.settlement_header(out["payment"])})
    return out


@app.post("/exceptions/{xid}/resolve")
def resolve(request: Request, xid: int, body: Summary, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None),
            x_seat: str | None = Header(None), payment_signature: str | None = Header(None)):
    return _claim_route(request, x_sandbox, x_actor, x_seat, payment_signature, actions.resolve_exception, xid, body.summary)


@app.post("/exceptions/{xid}/escalate")
def escalate(request: Request, xid: int, body: Reason, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None),
             x_seat: str | None = Header(None), payment_signature: str | None = Header(None)):
    return _claim_route(request, x_sandbox, x_actor, x_seat, payment_signature, actions.escalate_exception, xid, body.reason)


@app.post("/seats/{actor}/topup/x402")
def topup_x402(request: Request, actor: str, x_actor: str | None = Header(None), x_seat: str | None = Header(None), payment_signature: str | None = Header(None)):
    """Buy credits ahead of time with an x402 payment. Any multiple of the per-credit price buys that many credits."""
    if actor != x_actor:
        raise HTTPException(403, "you can only top up your own seat")
    if x_seat != "1":
        raise HTTPException(400, "env-key actors are unmetered; nothing to top up")
    if not payment_signature:
        e = _required(actor, _public_url(request), "send a PAYMENT-SIGNATURE header with an x402 payment")
        return JSONResponse(e.body, status_code=402, headers=e.headers)
    try:
        settled = settle_payment(actor, payment_signature, _public_url(request), min_credits=1)
    except PaymentRequired as e:
        return JSONResponse(e.body, status_code=402, headers=e.headers)
    return JSONResponse({"ok": True, "seat": diner.seat(actor), "payment": settled}, headers={"PAYMENT-RESPONSE": x402.settlement_header(settled)})


# ---------------- MCP: the same tools, in-process ----------------

MCP_TOOLS = diner.mcp_tools(agents.READ_TOOLS + agents.WRITE_TOOLS) + [
    {"name": "get_seat", "description": "Who you are, your sandbox, credits left and price per claim.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "proof", "description": "Your proof packet: pass rate per workflow on the cases you claimed, with 95% CI and the human unit cost.", "inputSchema": {"type": "object", "properties": {}}},
]


def mcp_call(name, a, sandbox, actor, metered):
    a = {k: v for k, v in (a or {}).items() if v not in ("", None)}
    W = lambda fn, *args: _do(sandbox, actor, fn, *args)  # noqa: E731
    if name == "calculate":
        return agents.calculate(str(a.get("expression", "")))
    if name == "get_policy":
        return get_policy()
    if name == "get_exception":
        return get_exception(int(a["exception_id"]), sandbox)
    if name == "get_invoice":
        return get_invoice(int(a["invoice_id"]), sandbox)
    if name == "search_invoices":
        return search_invoices(a.get("vendor_id"), a.get("invoice_number"), a.get("status"), a.get("po_number"), a.get("min_total"), a.get("max_total"),
                               a.get("date_from"), a.get("date_to"), min(int(a.get("limit") or 50), 200), sandbox)
    if name == "get_po":
        return get_po(a["po_number"], sandbox)
    if name == "search_pos":
        return search_pos(int(a["vendor_id"]), a.get("status"), a.get("sku"), min(int(a.get("limit") or 50), 200), sandbox)
    if name == "get_vendor":
        return get_vendor(int(a["vendor_id"]), sandbox)
    if name == "search_vendors":
        return search_vendors(a["name"], sandbox)
    if name == "get_bank_transaction":
        return get_txn(int(a["bank_transaction_id"]), sandbox)
    if name == "list_resolved_examples":
        return list_exceptions(a["kind"], "resolved", min(int(a.get("limit") or 5), 10), sandbox)
    if name == "approve_invoice":
        return W(actions.approve_invoice, int(a["invoice_id"]), a.get("approved_amount"), a.get("note"))
    if name == "hold_invoice":
        return W(actions.hold_invoice, int(a["invoice_id"]), a["reason"], a.get("note"))
    if name == "reject_invoice":
        return W(actions.reject_invoice, int(a["invoice_id"]), a["reason"], a.get("note"))
    if name == "dispute_invoice":
        return W(actions.dispute_invoice, int(a["invoice_id"]), a["reason"], a.get("note"))
    if name == "link_po":
        return W(actions.link_po, int(a["invoice_id"]), a["po_number"])
    if name == "match_bank_transaction":
        return W(actions.match_bank_transaction, int(a["bank_transaction_id"]), a["invoice_ids"])
    if name == "flag_bank_transaction":
        return W(actions.flag_bank_transaction, int(a["bank_transaction_id"]), a["reason"])
    if name == "flag_vendor":
        return W(actions.flag_vendor, int(a["vendor_id"]), a["reason"])
    if name == "resolve_exception":
        return claim(sandbox, actor, metered, actions.resolve_exception, int(a["exception_id"]), a.get("summary", ""))
    if name == "escalate_exception":
        return claim(sandbox, actor, metered, actions.escalate_exception, int(a["exception_id"]), a.get("reason", ""))
    if name == "get_seat":
        return me(actor, "1" if metered else "0")
    if name == "proof":
        return proof(sandbox, actor)
    return {"error": f"unknown tool {name}"}


@app.post("/mcp")
async def mcp(request: Request, x_sandbox: str | None = Header(None), x_actor: str | None = Header(None), x_seat: str | None = Header(None)):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, status_code=400)
    actor = x_actor or "anonymous"

    def call(name, args):
        try:
            return mcp_call(name, args, x_sandbox, actor, x_seat == "1")
        except PaymentRequired as e:
            return {"error": "payment required", **e.body, "pay_via": "POST /seats/<actor>/topup/x402 with a PAYMENT-SIGNATURE header, or retry the REST claim with it"}
        except HTTPException as e:
            return {"error": e.detail}

    status, payload = diner.jsonrpc(body, MCP_TOOLS, call)
    if payload is None:
        return JSONResponse(None, status_code=status)
    return JSONResponse(payload, status_code=status)


@app.get("/mcp")
def mcp_get():
    raise HTTPException(405, "POST JSON-RPC to /mcp (streamable HTTP, JSON responses); SSE is not offered")
