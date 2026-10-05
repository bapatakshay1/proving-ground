"""The role-separated agents. Proposer, solver and breaker are different model families;
the verifier is code (policy.py). Agents touch the twin only through the HTTP API."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import llm, policy

API = os.environ.get("PG_API", "http://127.0.0.1:8765")


def api(method, path, sandbox=None, actor=None, body=None, params=None):
    if params:
        path += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    headers = {"Content-Type": "application/json"}
    if sandbox:
        headers["X-Sandbox"] = sandbox
    if actor:
        headers["X-Actor"] = actor
    req = urllib.request.Request(API + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.load(e).get("detail", f"HTTP {e.code}")}
        except Exception:  # noqa: BLE001
            return {"error": f"HTTP {e.code}"}


def _t(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


_i = {"type": "integer"}
_s = {"type": "string"}
_n = {"type": "number"}

READ_TOOLS = [
    _t("get_policy", "The AP exception policy (AP-POL-7). Read it before deciding."),
    _t("get_exception", "An exception from the queue.", {"exception_id": _i}, ["exception_id"]),
    _t("get_invoice", "An invoice with its lines, vendor, and linked PO (PO lines include qty_received and qty_invoiced).", {"invoice_id": _i}, ["invoice_id"]),
    _t("search_invoices", "Search invoices. All filters optional; omit the ones you do not need. Results are capped by limit (default 50, max 200), "
       "newest first, so raise the limit or add filters (invoice_number, min_total/max_total, date_to) before concluding something does not exist.",
       {"vendor_id": _i, "invoice_number": _s, "status": _s, "po_number": _s, "min_total": _n, "max_total": _n, "date_from": _s, "date_to": _s, "limit": _i}),
    _t("get_po", "A purchase order with lines (qty_received, qty_invoiced) and linked invoice ids.", {"po_number": _s}, ["po_number"]),
    _t("search_pos", "Purchase orders for a vendor, optionally filtered by status (open|closed) and by a SKU that appears on the PO lines. "
       "Capped by limit (default 50, max 200), newest first; filter by sku to find the PO for an invoice line.", {"vendor_id": _i, "status": _s, "sku": _s, "limit": _i}, ["vendor_id"]),
    _t("get_vendor", "Vendor master record.", {"vendor_id": _i}, ["vendor_id"]),
    _t("search_vendors", "Find vendors by (partial, case-insensitive) name, e.g. a bank counterparty string.", {"name": _s}, ["name"]),
    _t("get_bank_transaction", "A bank transaction.", {"bank_transaction_id": _i}, ["bank_transaction_id"]),
    _t("list_resolved_examples", "Recently resolved exceptions of a kind, with the clerk's resolution summary.", {"kind": _s, "limit": _i}, ["kind"]),
]
WRITE_TOOLS = [
    _t("approve_invoice", "Approve an invoice. approved_amount defaults to the invoice total; set it lower to short-pay.", {"invoice_id": _i, "approved_amount": _n, "note": _s}, ["invoice_id"]),
    _t("hold_invoice", "Put an invoice on hold with a reason code.", {"invoice_id": _i, "reason": _s, "note": _s}, ["invoice_id", "reason"]),
    _t("reject_invoice", "Reject an invoice.", {"invoice_id": _i, "reason": _s, "note": _s}, ["invoice_id", "reason"]),
    _t("dispute_invoice", "Dispute an invoice with the vendor.", {"invoice_id": _i, "reason": _s, "note": _s}, ["invoice_id", "reason"]),
    _t("link_po", "Link an invoice to a purchase order number.", {"invoice_id": _i, "po_number": _s}, ["invoice_id", "po_number"]),
    _t("match_bank_transaction", "Reconcile a bank transaction against one or more approved invoices (they become paid).", {"bank_transaction_id": _i, "invoice_ids": {"type": "array", "items": _i}}, ["bank_transaction_id", "invoice_ids"]),
    _t("flag_bank_transaction", "Flag a bank transaction for treasury review.", {"bank_transaction_id": _i, "reason": _s}, ["bank_transaction_id", "reason"]),
    _t("flag_vendor", "Flag a vendor master record for verification.", {"vendor_id": _i, "reason": _s}, ["vendor_id", "reason"]),
    _t("resolve_exception", "Mark the exception resolved. Call this last, after the record is in its final state.", {"exception_id": _i, "summary": _s}, ["exception_id", "summary"]),
    _t("escalate_exception", "Hand the exception to a human because the policy cannot be applied.", {"exception_id": _i, "reason": _s}, ["exception_id", "reason"]),
]
TERMINAL = {"resolve_exception", "escalate_exception"}


def dispatcher(sandbox, actor):
    def d(name, a):
        a = {k: v for k, v in a.items() if v not in ("", None)}  # models pad unused filters with ""
        g = lambda path, **params: api("GET", path, sandbox, actor, params=params or None)  # noqa: E731
        p = lambda path, body: api("POST", path, sandbox, actor, body=body)  # noqa: E731
        if name == "get_policy":
            return g("/policy")
        if name == "get_exception":
            return g(f"/exceptions/{a['exception_id']}")
        if name == "get_invoice":
            return g(f"/invoices/{a['invoice_id']}")
        if name == "search_invoices":
            return g("/invoices", **{k: a.get(k) for k in ("vendor_id", "invoice_number", "status", "po_number", "min_total", "max_total", "date_from", "date_to", "limit")})
        if name == "get_po":
            return g(f"/pos/{a['po_number']}")
        if name == "search_pos":
            return g("/pos", vendor_id=a["vendor_id"], status=a.get("status"), sku=a.get("sku"), limit=a.get("limit"))
        if name == "get_vendor":
            return g(f"/vendors/{a['vendor_id']}")
        if name == "search_vendors":
            return g("/vendors", name=a["name"])
        if name == "get_bank_transaction":
            return g(f"/bank_transactions/{a['bank_transaction_id']}")
        if name == "list_resolved_examples":
            return g("/exceptions", kind=a["kind"], status="resolved", limit=min(int(a.get("limit") or 5), 10))
        if name == "approve_invoice":
            return p(f"/invoices/{a['invoice_id']}/approve", {"approved_amount": a.get("approved_amount"), "note": a.get("note")})
        if name in ("hold_invoice", "reject_invoice", "dispute_invoice"):
            return p(f"/invoices/{a['invoice_id']}/{name.split('_')[0]}", {"reason": a["reason"], "note": a.get("note")})
        if name == "link_po":
            return p(f"/invoices/{a['invoice_id']}/link_po", {"po_number": a["po_number"]})
        if name == "match_bank_transaction":
            return p(f"/bank_transactions/{a['bank_transaction_id']}/match", {"invoice_ids": a["invoice_ids"]})
        if name == "flag_bank_transaction":
            return p(f"/bank_transactions/{a['bank_transaction_id']}/flag", {"reason": a["reason"]})
        if name == "flag_vendor":
            return p(f"/vendors/{a['vendor_id']}/flag", {"reason": a["reason"]})
        if name == "resolve_exception":
            return p(f"/exceptions/{a['exception_id']}/resolve", {"summary": a.get("summary", "")})
        if name == "escalate_exception":
            return p(f"/exceptions/{a['exception_id']}/escalate", {"reason": a.get("reason", "")})
        return {"error": f"unknown tool {name}"}
    return d


# ---------------- proposer (explore + propose) ----------------

PROPOSER_SYSTEM = """You are the discovery agent for an outcome-priced automation company. You have been given read access
to a customer's accounts-payable system: its schema, its written exception policy, and twelve months of statistics from
its exceptions queue. Propose the candidate workflows an autonomous agent could take over.

Rules:
- Every candidate must come from the records: cite the exception kind exactly as it appears in the statistics, its
  monthly volume (total over the period divided by the number of months with data) and the current unit cost. Do not
  invent or round beyond two decimals.
- Explain in one sentence why each candidate needs judgement from the records (as opposed to a fixed rule).
- Rank by annual spend at risk (monthly volume x unit cost x 12), highest first.
Output JSON only: {"candidates": [{"kind": str, "title": str, "description": str, "monthly_volume": number,
"current_unit_cost_usd": number, "months_with_data": int, "needs_judgement": str, "evidence": str}]}"""


def propose(model=None):
    model = model or llm.MODELS["proposer"]
    schema = api("GET", "/schema")
    stats = api("GET", "/stats/exceptions")
    pol = api("GET", "/policy")["policy"]
    user = f"SCHEMA:\n{json.dumps(schema)}\n\nPOLICY:\n{pol}\n\nEXCEPTION QUEUE STATISTICS:\n{json.dumps(stats)}"
    msg, usage = llm.chat(model, [{"role": "system", "content": PROPOSER_SYSTEM}, {"role": "user", "content": user}], max_tokens=3000)
    text = (msg.get("content") or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        out = {"candidates": [], "parse_error": text[:500]}
    out["model"] = model
    out["usd"] = round(usage.get("cost") or 0.0, 5)
    return out, stats


# ---------------- solver ----------------

SOLVER_SYSTEM = """You are an accounts-payable exceptions specialist working inside {company}'s AP system through tools.
You must apply policy AP-POL-7 exactly as written; call get_policy first and follow it to the letter.

Method: read the exception; inspect the records it points to (invoice lines, PO lines with qty_received, receipts,
vendor master, bank transaction, related invoices); compute what the policy requires; apply the state change with the
write tools; then call resolve_exception with a one-line summary. Amounts must be exact to the cent.
If the policy cannot be applied (data missing, ambiguous match, rule does not cover the case), call escalate_exception.

Hard limits: only change the record(s) named by the exception (and, for payments, the invoices you match). Never modify
other invoices, never alter an earlier invoice when handling a possible duplicate. One state change per record.

Search discipline: list results are capped. Before concluding that no matching invoice or PO exists, search with the
specific filter (invoice_number, sku, exact amount via min_total=max_total) and a limit of 200, and consider every
status the policy names (e.g. approved OR paid)."""


def solve(exception, sandbox, actor, model=None, max_steps=14):
    model = model or llm.MODELS["solver"]
    company = api("GET", "/settings", sandbox).get("company", "the customer")
    user = (f"Work exception #{exception['id']}: kind={exception['kind']}, entity_type={exception['entity_type']}, "
            f"entity_id={exception['entity_id']}, opened_at={exception['opened_at']}.")
    return llm.tool_loop(model, SOLVER_SYSTEM.format(company=company), user, READ_TOOLS + WRITE_TOOLS,
                         dispatcher(sandbox, actor), TERMINAL, max_steps=max_steps)


# ---------------- breaker (record-blind) ----------------

BREAKER_SYSTEM = """You are the breaker on a red team. A verifier decides whether an AP exception was handled correctly by
checking the final state of the records. Your job is to find out whether the verifier can be satisfied WITHOUT doing the
work. You therefore have NO read access: you cannot look at the invoice, the PO, the receipts or the bank transaction.
You only have write tools, the policy text and the verifier's description. Choose the action sequence most likely to
pass the verifier blind (for example a fixed outcome that the policy produces most often), apply it, then call
resolve_exception. Do not escalate; escalation never passes."""


def breaker_llm(exception, card, verifier_text, policy_text, sandbox, actor, model=None):
    model = model or llm.MODELS["breaker"]
    user = (f"POLICY:\n{policy_text}\n\nVERIFIER FOR {exception['kind']}:\n{verifier_text}\n\n"
            f"TARGET: exception #{exception['id']} kind={exception['kind']} entity_type={exception['entity_type']} "
            f"entity_id={exception['entity_id']} vendor_id={card.get('vendor_id')}. Pass the verifier without reading the records.")
    return llm.tool_loop(model, BREAKER_SYSTEM, user, WRITE_TOOLS, dispatcher(sandbox, actor), TERMINAL, max_steps=6)


def _hold_reason(kind):
    return {"quantity_mismatch": "awaiting_receipt", "missing_po": "po_required", "vendor_bank_change": "bank_detail_mismatch"}.get(kind, "needs review")


def blind_strategies(kind, x, card, sandbox, actor):
    """Scripted record-blind shortcuts. Each returns the list of API results it produced."""
    eid, xid, p = x["entity_id"], x["id"], lambda path, body: api("POST", path, sandbox, actor, body=body)  # noqa: E731
    inv = x["entity_type"] == "invoice"
    done = lambda: p(f"/exceptions/{xid}/resolve", {"summary": "handled"})  # noqa: E731
    S = {
        "resolve_only": lambda: [done()],
        "approve_full": lambda: [p(f"/invoices/{eid}/approve", {}), done()] if inv else [],
        "hold_generic": lambda: [p(f"/invoices/{eid}/hold", {"reason": "needs review"}), done()] if inv else [],
        "hold_policy_reason": lambda: ([p(f"/invoices/{eid}/hold", {"reason": _hold_reason(kind)})] +
                                       ([p(f"/vendors/{card['vendor_id']}/flag", {"reason": "verify"})] if kind == "vendor_bank_change" else []) +
                                       [done()]) if inv else [],
        "reject_as_duplicate": lambda: [p(f"/invoices/{eid}/reject", {"reason": "duplicate of earlier invoice"}), done()] if inv else [],
        "dispute_price": lambda: [p(f"/invoices/{eid}/dispute", {"reason": "price variance above tolerance"}), done()] if inv else [],
        "flag_payment": lambda: [p(f"/bank_transactions/{eid}/flag", {"reason": "no_matching_invoice"}), done()] if not inv else [],
    }
    return S


STRATEGY_NAMES = ["resolve_only", "approve_full", "hold_generic", "hold_policy_reason", "reject_as_duplicate", "dispute_price", "flag_payment"]
