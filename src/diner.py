"""The diner layer: self-serve seats, a credits meter, the public menu, and MCP plumbing.
Rail-agnostic - a payment rail tops up credits through the admin endpoint; nothing here knows how money moves."""
import hashlib
import json
import os
import pathlib
import secrets
import sqlite3
import threading
import time

PRICE_CREDITS = int(os.environ.get("PG_PRICE_CREDITS", "1"))
FREE_CREDITS = int(os.environ.get("PG_FREE_CREDITS", "10"))
MAX_SEATS = int(os.environ.get("PG_MAX_SEATS", "500"))
SEATS_DB = pathlib.Path(os.environ.get("PG_SEATS_DB", "out/seats.db"))
SEAT_RATE = (5, 3600)  # seats per IP per window
try:
    TOPUP = json.loads(os.environ.get("PG_TOPUP", "[]"))
except json.JSONDecodeError:
    TOPUP = []

DDL = ["create table if not exists seats(token_hash text primary key, actor text unique, created_at text, credits integer, spent integer, plan text)",
       "create table if not exists charges(id integer primary key autoincrement, actor text, ts text, exception_id integer, credits integer, passed integer)",
       "create table if not exists payments(payment_key text primary key, actor text, ts text, rail text, amount_usd real, credits integer, payer text, transaction_id text, network text)"]
_lock = threading.Lock()
_recent = {}


def _conn():
    SEATS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(SEATS_DB, timeout=30)
    c.row_factory = sqlite3.Row
    for d in DDL:
        c.execute(d)
    return c


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _now():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_actor(name):
    name = "".join(ch for ch in (name or "").lower() if ch.isalnum() or ch in "-_")[:32]
    return name if name and name.replace("-", "").replace("_", "").isalnum() else None


def rate_ok(ip):
    n, window = SEAT_RATE
    now = time.time()
    with _lock:
        hits = [t for t in _recent.get(ip, []) if now - t < window]
        if len(hits) >= n:
            _recent[ip] = hits
            return False
        hits.append(now)
        _recent[ip] = hits
        return True


def create_seat(name, reserved=()):
    """Returns (token, actor, credits). The token is shown once; only its hash is stored."""
    with _conn() as c:
        if c.execute("select count(*) from seats").fetchone()[0] >= MAX_SEATS:
            raise ValueError("no seats left; try later")
        actor = safe_actor(name)
        if not actor or actor in reserved or c.execute("select 1 from seats where actor=?", (actor,)).fetchone():
            while True:
                actor = f"guest-{secrets.token_hex(3)}"
                if actor not in reserved and not c.execute("select 1 from seats where actor=?", (actor,)).fetchone():
                    break
        token = secrets.token_urlsafe(24)
        c.execute("insert into seats(token_hash,actor,created_at,credits,spent,plan) values(?,?,?,?,0,'free')",
                  (_hash(token), actor, _now(), FREE_CREDITS))
        return token, actor, FREE_CREDITS


def lookup(token):
    with _conn() as c:
        r = c.execute("select actor from seats where token_hash=?", (_hash(token),)).fetchone()
        return r["actor"] if r else None


def seat(actor):
    with _conn() as c:
        r = c.execute("select actor, credits, spent, plan, created_at from seats where actor=?", (actor,)).fetchone()
        return dict(r) if r else None


def can_pay(actor):
    s = seat(actor)
    return s is not None and s["credits"] >= PRICE_CREDITS


def payment_required(actor, terms_url=None):
    s = seat(actor) or {"credits": 0}
    out = {"detail": "out of credits", "credits": s["credits"], "price": PRICE_CREDITS, "unit": "credit per metered claim (resolve/escalate)",
           "top_up": {"methods": TOPUP, "note": "reads are free; each claim costs price credits; top up by one of the listed methods"}}
    if terms_url:
        out["terms_url"] = terms_url
    return out


def charge(actor, exception_id, passed):
    with _conn() as c:
        c.execute("update seats set credits=credits-?, spent=spent+? where actor=? and credits>=?", (PRICE_CREDITS, PRICE_CREDITS, actor, PRICE_CREDITS))
        c.execute("insert into charges(actor,ts,exception_id,credits,passed) values(?,?,?,?,?)", (actor, _now(), exception_id, PRICE_CREDITS, int(bool(passed))))
        return c.execute("select credits, spent from seats where actor=?", (actor,)).fetchone()


def credit_for_payment(actor, payment_key, rail, amount_usd, credits, meta):
    """Credit a seat for a settled payment exactly once. Returns (credited_now, seat_row)."""
    with _conn() as c:
        if c.execute("select 1 from payments where payment_key=?", (payment_key,)).fetchone():
            return False, dict(c.execute("select actor, credits, spent, plan from seats where actor=?", (actor,)).fetchone())
        c.execute("insert into payments(payment_key,actor,ts,rail,amount_usd,credits,payer,transaction_id,network) values(?,?,?,?,?,?,?,?,?)",
                  (payment_key, actor, _now(), rail, amount_usd, int(credits), meta.get("payer"), meta.get("transaction"), meta.get("network")))
        c.execute("update seats set credits=credits+?, plan='paid' where actor=?", (int(credits), actor))
        return True, dict(c.execute("select actor, credits, spent, plan from seats where actor=?", (actor,)).fetchone())


def credit(actor, credits, plan=None):
    with _conn() as c:
        if not c.execute("select 1 from seats where actor=?", (actor,)).fetchone():
            return None
        c.execute("update seats set credits=credits+? where actor=?", (int(credits), actor))
        if plan:
            c.execute("update seats set plan=? where actor=?", (plan, actor))
        return dict(c.execute("select actor, credits, spent, plan from seats where actor=?", (actor,)).fetchone())


# ---------------- the menu ----------------

TAGLINE = ("Proving Ground is an accounts-payable exception environment with a pass/fail verifier and proof packet, for AI agents: "
           "a sealed replica of a company's AP system (twelve months of synthetic records, a live exceptions queue) reachable as an MCP server "
           "or plain HTTP. An agent works real exceptions; a code verifier keyed to the system of record returns a pass/fail per case at the "
           "moment of the claim; GET /proof returns the proof packet (pass rate per workflow with 95% CI next to what a human clerk costs per "
           "case today). Reads are free; each verified claim is metered in credits, topped up by x402 (USDC) with no human in the loop.")

WORKFLOWS = {
    "price_mismatch": "Invoice unit price differs from the PO: approve within tolerance, dispute above it.",
    "quantity_mismatch": "Invoice quantity exceeds goods received: hold, or approve the received portion.",
    "possible_duplicate": "Another invoice matches on number or amount: reject true duplicates, approve the rest.",
    "missing_po": "No valid PO reference: find the one open PO that covers the lines, or hold.",
    "unmatched_payment": "Bank debit with no reconciled invoice: match one invoice or one pair, else flag.",
    "vendor_bank_change": "Remit-to account differs from vendor master: hold and flag the vendor.",
}


def menu(base_url, stats):
    costs = {k["kind"]: k for k in stats.get("kinds", [])}
    return {
        "name": "Proving Ground",
        "what": TAGLINE,
        "menu": [{"kind": k, "description": v, "human_unit_cost_usd": costs.get(k, {}).get("current_unit_cost_usd"),
                  "open_cases": costs.get(k, {}).get("open"), "price_credits_per_claim": PRICE_CREDITS} for k, v in WORKFLOWS.items()],
        "pricing": {"reads": "free", "claim": f"{PRICE_CREDITS} credit per metered claim (POST /exceptions/{{id}}/resolve or /escalate)",
                    "free_credits_per_seat": FREE_CREDITS, "top_up": TOPUP or "not yet enabled - email the owner"},
        "get_a_seat": {"request": f"POST {base_url}/seat", "body": {"name": "optional actor name"},
                       "returns": "a bearer token (shown once), your actor name, a private sandbox copied from the base twin, and credits"},
        "client_contract": {"headers": {"Authorization": "Bearer <token>", "X-Sandbox": "<your actor> on every call (writes refused without it)"},
                            "reads": ["GET /me", "GET /exceptions?status=open&kind=", "GET /exceptions/{id}", "GET /invoices/{id}", "GET /invoices?vendor_id=&invoice_number=&min_total=&max_total=&date_to=&limit=",
                                      "GET /pos/{po_number}", "GET /pos?vendor_id=&status=open&sku=", "GET /vendors/{id}", "GET /vendors?name=", "GET /bank_transactions/{id}", "GET /policy", "GET /schema"],
                            "writes": ["POST /invoices/{id}/approve|hold|reject|dispute|link_po", "POST /bank_transactions/{id}/match|flag", "POST /vendors/{id}/flag"],
                            "claim": ["POST /exceptions/{id}/resolve {summary}", "POST /exceptions/{id}/escalate {reason}", "-> the verdict rides in the response"],
                            "proof": ["GET /proof", "POST /exceptions/{id}/verify (recorded once)", "POST /sandbox/reset"]},
        "mcp": {"url": f"{base_url}/mcp", "transport": "streamable-http (JSON responses)", "auth": "same bearer token; no X-Sandbox needed"},
        "links": {"openapi": f"{base_url}/openapi.json", "llms_txt": f"{base_url}/llms.txt", "agent_card": f"{base_url}/.well-known/agent-card.json",
                  "policy": f"{base_url}/policy", "schema": f"{base_url}/schema", "terms": f"{base_url}/terms", "privacy": f"{base_url}/privacy"},
        "terms": f"{base_url}/terms",
    }


def llms_txt(m):
    L = [f"# {m['name']}", "", f"> {m['what']}", "", "## Menu (price in credits per metered claim; reads are free)", ""]
    for it in m["menu"]:
        L.append(f"- **{it['kind']}** — {it['description']} Human unit cost today: ${it['human_unit_cost_usd']}; open cases: {it['open_cases']}; price: {it['price_credits_per_claim']} credit.")
    L += ["", "## Get a seat", "", f"`{m['get_a_seat']['request']}` with JSON `{json.dumps(m['get_a_seat']['body'])}` → {m['get_a_seat']['returns']}.",
          f"Free credits per seat: {m['pricing']['free_credits_per_seat']}. Top up: {json.dumps(m['pricing']['top_up'])}.", "",
          "## Client contract", "", f"Headers: `Authorization: Bearer <token>` and `X-Sandbox: <your actor>`.", ""]
    for sec in ("reads", "writes", "claim", "proof"):
        L.append(f"- {sec}: " + "; ".join(m["client_contract"][sec]))
    L += ["", f"## MCP", "", f"POST `{m['mcp']['url']}` ({m['mcp']['transport']}); {m['mcp']['auth']}.", "", "## Links", ""]
    L += [f"- {k}: {v}" for k, v in m["links"].items()]
    L += ["", f"Terms: {m['terms']} (taking a seat or sending a paid request is acceptance; draft, not legal advice)."]
    return "\n".join(L) + "\n"


def html(m):
    rows = "".join(f"<tr><td>{it['kind']}</td><td>{it['description']}</td><td>${it['human_unit_cost_usd']}</td><td>{it['open_cases']}</td><td>{it['price_credits_per_claim']}</td></tr>" for it in m["menu"])
    topup = m["pricing"]["top_up"]
    pay = ", ".join(f"{t.get('method')} ({t.get('network')})" if isinstance(t, dict) else str(t) for t in topup) if isinstance(topup, list) else str(topup)
    links = "".join(f'<a href="{v}">{k}</a>' + (" · " if i < len(m["links"]) - 1 else "") for i, (k, v) in enumerate(m["links"].items()))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Proving Ground — accounts-payable exception environment with a pass/fail verifier and proof packet for AI agents (MCP server, x402)</title>
<meta name="description" content="{TAGLINE}">
<style>body{{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #ddd;padding:.3rem .5rem;text-align:left;vertical-align:top}}code,pre{{background:#f4f4f4;padding:.1rem .3rem;font-size:13px}}pre{{padding:.5rem;overflow:auto}}h1{{font-size:1.5rem;margin:.2rem 0}}h2{{font-size:1.1rem;margin:1rem 0 .3rem}}</style></head>
<body><h1>Proving Ground</h1>
<p><strong>An accounts-payable exception environment with a pass/fail verifier and proof packet, for AI agents.</strong> {TAGLINE.split(': ', 1)[1]}</p>
<h2>Menu</h2><table><tr><th>Workflow</th><th>What</th><th>Human cost/case</th><th>Open</th><th>Credits/claim</th></tr>{rows}</table>
<p>Reads are free. Each claim costs {PRICE_CREDITS} credit; every seat starts with {FREE_CREDITS} free. Top up by: {pay} — details at <a href="{m['links'].get('pricing', m['links']['openapi'].replace('/openapi.json', '/pricing'))}">/pricing</a>.</p>
<h2>Walk in</h2><pre>curl -s -X POST {m['get_a_seat']['request']} -H 'Content-Type: application/json' -d '{{"name":"my-agent"}}'</pre>
<p>Then send <code>Authorization: Bearer &lt;token&gt;</code> and <code>X-Sandbox: &lt;actor&gt;</code> on every call — or point any MCP client at <code>{m['mcp']['url']}</code> (streamable HTTP; with the token, or with no auth: <code>initialize</code> seats you and returns <code>Mcp-Session-Id</code>).</p>
<p>{links}</p>
<p style="color:#666;font-size:13px">By taking a seat or sending a paid request you agree to the <a href="{m['terms']}">terms</a>. Draft terms; not legal advice.</p>
</body></html>"""


def agent_card(base_url):
    return {"name": "Proving Ground", "description": "Accounts-payable exception environment with a pass/fail verifier and proof packet for AI agents: a sealed AP twin with a live exceptions queue; a code verifier meters each claim; GET /proof returns the proof packet. MCP server at /mcp; x402 top-ups.",
            "url": base_url, "version": "0.4", "provider": {"organization": "Proving Ground"}, "termsOfService": f"{base_url}/terms", "privacyPolicy": f"{base_url}/privacy",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "authentication": {"schemes": ["bearer"], "credentials": f"POST {base_url}/seat returns a bearer token"},
            "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"],
            "skills": [{"id": k, "name": k, "description": v, "tags": ["accounts-payable", "exceptions", "finance-ops"]} for k, v in WORKFLOWS.items()],
            "interfaces": {"rest": f"{base_url}/openapi.json", "mcp": f"{base_url}/mcp", "llms_txt": f"{base_url}/llms.txt"}}


# ---------------- MCP (streamable HTTP, JSON responses only) ----------------

PROTOCOL = "2025-06-18"


TOOL_ORDER = ["get_policy", "list_open_exceptions", "get_exception", "get_invoice", "search_invoices", "get_po", "search_pos", "get_vendor",
              "search_vendors", "get_bank_transaction", "calculate", "list_resolved_examples",
              "approve_invoice", "hold_invoice", "reject_invoice", "dispute_invoice", "link_po", "match_bank_transaction", "flag_bank_transaction", "flag_vendor",
              "resolve_exception", "escalate_exception", "get_seat", "proof"]
TITLES = {"get_policy": "Get the AP exception policy", "list_open_exceptions": "List open exceptions", "get_exception": "Get an exception",
          "get_invoice": "Get an invoice", "search_invoices": "Search invoices", "get_po": "Get a purchase order", "search_pos": "Search purchase orders",
          "get_vendor": "Get a vendor", "search_vendors": "Search vendors", "get_bank_transaction": "Get a bank transaction", "calculate": "Calculate exactly",
          "list_resolved_examples": "List resolved examples", "approve_invoice": "Approve invoice", "hold_invoice": "Hold invoice", "reject_invoice": "Reject invoice",
          "dispute_invoice": "Dispute invoice", "link_po": "Link invoice to PO", "match_bank_transaction": "Match payment to invoices",
          "flag_bank_transaction": "Flag bank transaction", "flag_vendor": "Flag vendor", "resolve_exception": "Resolve exception (metered claim)",
          "escalate_exception": "Escalate exception (metered claim)", "get_seat": "My seat and credits", "proof": "My proof packet"}
WRITES = {"approve_invoice", "hold_invoice", "reject_invoice", "dispute_invoice", "link_po", "match_bank_transaction", "flag_bank_transaction", "flag_vendor",
          "resolve_exception", "escalate_exception"}
NON_IDEMPOTENT = {"match_bank_transaction", "resolve_exception", "escalate_exception"}


def annotate(name):
    write = name in WRITES
    return {"title": TITLES.get(name, name.replace("_", " ")), "readOnlyHint": not write, "destructiveHint": write,
            "idempotentHint": name not in NON_IDEMPOTENT, "openWorldHint": False}


def mcp_tools(tool_defs, extra=()):
    """MCP tool list: titles + annotations, ordered so the obvious first doors come first."""
    defs = {t["function"]["name"]: {"name": t["function"]["name"], "description": t["function"]["description"], "inputSchema": t["function"]["parameters"]} for t in tool_defs}
    for t in extra:
        defs[t["name"]] = t
    out = []
    for n in TOOL_ORDER + [n for n in defs if n not in TOOL_ORDER]:
        if n in defs:
            out.append({**defs[n], "title": TITLES.get(n, n.replace("_", " ")), "annotations": annotate(n)})
    return out


def jsonrpc(body, tools, call):
    """Handle one JSON-RPC message. `call(name, args)` returns a JSON-able result or raises.
    Returns (status, payload-or-None)."""
    if isinstance(body, list):
        out = [r for r in (jsonrpc(b, tools, call)[1] for b in body) if r is not None]
        return (200, out) if out else (202, None)
    mid, method, params = body.get("id"), body.get("method"), body.get("params") or {}
    if method is None:
        return 400, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32600, "message": "method required"}}
    if mid is None:  # notification
        return 202, None
    if method == "initialize":
        return 200, {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                     "serverInfo": {"name": "proving-ground", "version": "0.3"},
                     "instructions": "Work accounts-payable exceptions in your private sandbox. Call get_policy first. Each resolve/escalate is metered and returns a verdict; call proof for your pass rates."}}
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            res = call(name, args)
            err = isinstance(res, dict) and bool(res.get("error"))
            return 200, {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(res, default=str)}], "isError": err}}
        except Exception as e:  # noqa: BLE001
            return 200, {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True}}
    return 200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def md_html(md, title):
    """Just enough markdown for the legal pages: headings, paragraphs, lists, tables, bold, code."""
    import html as _h
    import re
    def inline(t):
        t = _h.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
        return t
    out, para, lst, table = [], [], False, []
    def flush():
        nonlocal para, lst, table
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>"); para = []
        if lst:
            out.append("</ul>"); lst = False
        if table:
            head, *rows = [r for r in table if not set(r.replace("|", "").strip()) <= set("-: ")]
            cells = lambda r: [inline(c.strip()) for c in r.strip().strip("|").split("|")]  # noqa: E731
            out.append("<table><tr>" + "".join(f"<th>{c}</th>" for c in cells(head)) + "</tr>" +
                       "".join("<tr>" + "".join(f"<td>{c}</td>" for c in cells(r)) + "</tr>" for r in rows) + "</table>"); table = []
    for line in md.splitlines():
        if line.startswith("|"):
            table.append(line); continue
        if table:
            flush()
        if line.startswith("#"):
            flush(); n = len(line) - len(line.lstrip("#")); out.append(f"<h{n}>{inline(line.lstrip('#').strip())}</h{n}>")
        elif line.startswith("- "):
            if para: out.append("<p>" + inline(" ".join(para)) + "</p>"); para = []
            if not lst: out.append("<ul>"); lst = True
            out.append(f"<li>{inline(line[2:])}</li>")
        elif not line.strip():
            flush()
        else:
            para.append(line)
    flush()
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_h.escape(title)}</title><style>body{{font:15px/1.5 system-ui,sans-serif;max-width:860px;"
            f"margin:2rem auto;padding:0 1rem;color:#222}}table{{border-collapse:collapse}}td,th{{border-bottom:1px solid #ddd;padding:.3rem .5rem;text-align:left;vertical-align:top}}"
            f"code{{background:#f4f4f4;padding:.1rem .3rem}}</style></head><body>" + "".join(out) + "</body></html>")
