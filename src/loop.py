"""The discovery-to-billing loop: explore/propose -> admit -> prove -> price -> operate/bill -> proof packet."""
import collections
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import pathlib
import shutil
import statistics

from . import agents, llm, policy, seed

OUT = pathlib.Path(os.environ.get("PG_OUT", "out"))
BASE = OUT / "twin.db"
SANDBOXES = OUT / "sandboxes"
RUNS = OUT / "runs"
# thresholds are proposals, per the thesis; every one is visible in the packet
HISTORY_AGREEMENT_MIN = 0.90      # verifier must accept >= 90% of how humans actually resolved cases
BLIND_PASS_MAX = 0.70             # no record-blind strategy may reach 70% on the verifier
CONTRACT_BAR = 0.90               # pass rate the workflow is priced against
PRICE_FRACTION = 0.5              # price per outcome = half the customer's current unit cost
SERVE_OVERHEAD = 0.25             # exception handling / compute overhead on measured model spend
SHADOW_GAP_MAX = 10.0             # stop rule: live pass rate more than 10 pts below replay
ANNUAL_MIN_FRACTION = 0.8         # annual minimum = 80% of volume x lower CI bound of the pass rate, so the floor is defensible
OPS_ALLOWANCE = 0.25              # $/verified outcome for twin hosting, verifier upkeep, audits and support (assumption, not measured)
WORKERS = int(os.environ.get("PG_WORKERS", "6"))
ADMIT_SAMPLE, LLM_BREAK_SAMPLE, OPERATE_PER_KIND = 25, 8, 8


def jdump(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=1, default=str))


def jload(name):
    return json.loads((OUT / name).read_text())


def wilson(p, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    d = 1 + z * z / n
    return (round((c - s) / d, 3), round((c + s) / d, 3))


def sandbox(name, reopen_ids=()):
    SANDBOXES.mkdir(parents=True, exist_ok=True)
    p = SANDBOXES / f"{name}.db"
    shutil.copy(BASE, p)
    if reopen_ids:
        c = policy.connect(p)
        for xid in reopen_ids:
            seed.reopen(c, xid)
        c.close()
    return name


def drop(name):
    for suf in ("", "-journal", "-wal", "-shm"):
        (SANDBOXES / f"{name}.db{suf}").unlink(missing_ok=True)


def card(conn, x):
    if x["entity_type"] == "invoice":
        return {"vendor_id": policy.row(conn, "invoices", x["entity_id"])["vendor_id"]}
    return {}


def outcome_of(kind, state):
    """Collapse a post_state snapshot or a live row into the fields that define the outcome."""
    if kind == "unmatched_payment":
        t = next(iter(state["bank_transactions"].values()))
        return {"status": t["status"], "matched_invoice_ids": sorted(json.loads(t["matched_invoice_ids"] or "[]"))}
    i = next(iter(state["invoices"].values()))
    return {"status": i["status"], "approved_amount": i["approved_amount"], "hold_reason": i["hold_reason"], "po_number": i["po_number"]}


# ---------------- 1-3. explore & propose ----------------

def explore():
    proposal, stats = agents.propose()
    by_kind = {k["kind"]: k for k in stats["kinds"]}
    months = {k["kind"]: len(k["by_month"]) for k in stats["kinds"]}
    checked = []
    for c in proposal.get("candidates", []):
        k = by_kind.get(c.get("kind"))
        if not k:
            checked.append({**c, "evidence_ok": False, "reason": "kind not in records"})
            continue
        vol = k["total"] / max(1, months[c["kind"]])
        ok_vol = abs(float(c.get("monthly_volume", 0)) - vol) <= 0.15 * vol + 0.5
        ok_cost = abs(float(c.get("current_unit_cost_usd", 0)) - k["current_unit_cost_usd"]) <= 0.25
        checked.append({**c, "records": {"total": k["total"], "months": months[c["kind"]], "monthly_volume": round(vol, 2),
                                         "current_unit_cost_usd": k["current_unit_cost_usd"], "avg_handling_minutes": k["avg_handling_minutes"], "open": k["open"]},
                        "evidence_ok": ok_vol and ok_cost, "reason": None if ok_vol and ok_cost else "volume/cost not supported by records"})
    out = {"model": proposal.get("model"), "usd": proposal.get("usd"), "parse_error": proposal.get("parse_error"),
           "candidates": checked, "stats": stats}
    jdump("candidates.json", out)
    return out


# ---------------- 4. admit ----------------

def admit(candidates=None):
    cands = candidates or jload("candidates.json")
    conn = policy.connect(BASE)
    pol = agents.api("GET", "/policy")["policy"]
    report = {"thresholds": {"history_agreement_min": HISTORY_AGREEMENT_MIN, "blind_pass_max": BLIND_PASS_MAX}, "workflows": {}}
    for c in cands["candidates"]:
        kind = c["kind"]
        if not c.get("evidence_ok") or kind not in policy.KINDS:
            report["workflows"][kind] = {"admitted": False, "reason": c.get("reason") or "unknown kind"}
            continue
        # (a) verifier soundness: does the meter agree with how humans actually resolved the past cases?
        resolved = [dict(r) for r in conn.execute("select * from exceptions where kind=? and status='resolved'", (kind,))]
        agree = sum(policy.verify(conn, kind, r["entity_id"])["passed"] for r in resolved)
        agreement = agree / len(resolved)
        # (b) record-blind breaker: scripted shortcuts, then an LLM with write-only tools
        sample = [r for r in resolved if not r["held_out"]][:ADMIT_SAMPLE]
        strategies = {}
        for s in agents.STRATEGY_NAMES:
            sb = sandbox(f"admit-{kind}-{s}", [r["id"] for r in sample])
            cs = policy.connect(SANDBOXES / f"{sb}.db")
            passed = 0
            for r in sample:
                actor = f"breaker:{s}:{r['id']}"
                acts = agents.blind_strategies(kind, r, card(cs, r), sb, actor)[s]()
                if acts:
                    passed += policy.verify(cs, kind, r["entity_id"], actor=actor, exception_id=r["id"])["passed"]
            cs.close()
            drop(sb)
            strategies[s] = round(passed / len(sample), 3)
        llm_sample = sample[:LLM_BREAK_SAMPLE]
        sb = sandbox(f"admit-{kind}-llm", [r["id"] for r in llm_sample])
        cs = policy.connect(SANDBOXES / f"{sb}.db")
        llm_pass, llm_runs = 0, []
        for r in llm_sample:
            actor = f"breaker:llm:{r['id']}"
            t = agents.breaker_llm(r, card(cs, r), policy.VERIFIER_TEXT[kind], pol, sb, actor)
            v = policy.verify(cs, kind, r["entity_id"], actor=actor, exception_id=r["id"])
            llm_pass += v["passed"]
            llm_runs.append({"exception_id": r["id"], "passed": v["passed"], "failures": v["failures"],
                             "actions": [s["tool"] for s in t["steps"]], "usd": t["usd"]})
        cs.close()
        drop(sb)
        llm_rate = llm_pass / max(1, len(llm_sample))
        # the blind pass rate is an estimate; screen only when its lower 95% bound clears the threshold
        rates = {**{k: (v, len(sample)) for k, v in strategies.items()}, "llm_breaker": (llm_rate, len(llm_sample))}
        best_name, (best, best_n) = max(rates.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        best_lo = wilson(best, best_n)[0]
        admitted = agreement >= HISTORY_AGREEMENT_MIN and best_lo <= BLIND_PASS_MAX
        reason = None
        if agreement < HISTORY_AGREEMENT_MIN:
            reason = f"verifier agrees with human history only {agreement:.0%}"
        elif best_lo > BLIND_PASS_MAX:
            reason = (f"record-blind shortcut '{best_name}' passes {best:.0%} (95% lower bound {best_lo:.0%}) without reading the records: "
                      "the outcome is a fixed rule, not a judgement task; route it to deterministic automation / a hard control instead of outcome pricing")
        report["workflows"][kind] = {"admitted": admitted, "reason": reason, "verifier": policy.VERIFIER_TEXT[kind],
                                     "history_agreement": round(agreement, 3), "history_n": len(resolved),
                                     "blind_strategies": strategies, "best_blind": {"strategy": best_name, "pass_rate": round(best, 3), "n": best_n, "ci95": wilson(best, best_n)},
                                     "llm_breaker": {"model": llm.MODELS["breaker"], "n": len(llm_sample), "pass_rate": round(llm_rate, 3), "runs": llm_runs}}
    conn.close()
    jdump("admission.json", report)
    return report


# ---------------- 5. prove by replay ----------------

def _replay_case(kind, x):
    sb = sandbox(f"replay-{x['id']}", [x["id"]])
    actor = f"agent:solver:{x['id']}"
    t = agents.solve({k: x[k] for k in ("id", "kind", "entity_type", "entity_id", "opened_at")}, sb, actor)
    cs = policy.connect(SANDBOXES / f"{sb}.db")
    claimed = t["ended_by"] == "resolve_exception"
    v = policy.verify(cs, kind, x["entity_id"], actor=actor, exception_id=x["id"]) if claimed else None
    live = seed.snapshot(cs, kind, x["entity_id"])
    cs.close()
    rec = {"exception_id": x["id"], "kind": kind, "entity_id": x["entity_id"], "ended_by": t["ended_by"], "claimed": claimed,
           "passed": bool(v and v["passed"]), "failures": (v or {}).get("failures", []), "expected": (v or {}).get("expected"),
           "actual": (v or {}).get("actual"), "agrees_with_history": outcome_of(kind, live) == outcome_of(kind, json.loads(x["post_state"])),
           "history_was_override": "override" in (x["resolution"] or ""), "steps": len(t["steps"]), "usd": t["usd"], "tokens": t["tokens"],
           "tools": [s["tool"] for s in t["steps"]], "final": t["final"]}
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"replay-{x['id']}.json").write_text(json.dumps({"case": rec, "transcript": t}, indent=1, default=str))
    drop(sb)
    return rec


def slug(model):
    return model.replace("/", "-").replace(":", "-")


def prove(admission=None, alt=False):
    """Replay held-out cases with the configured solver. alt=True records a comparison tier only."""
    adm = admission or jload("admission.json")
    conn = policy.connect(BASE)
    cases = []
    for kind, w in adm["workflows"].items():
        if w.get("admitted"):
            cases += [(kind, dict(r)) for r in conn.execute("select * from exceptions where kind=? and held_out=1 order by id", (kind,))]
    conn.close()
    results = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for rec in ex.map(lambda kx: _replay_case(*kx), cases):
            results.append(rec)
            print(f"  replay {rec['kind']:20s} #{rec['exception_id']:<5} {'PASS' if rec['passed'] else 'FAIL':4s} {rec['ended_by']:18s} {','.join(rec['failures'])[:60]}", flush=True)
    out = {"model": llm.MODELS["solver"], "cases": results, "by_kind": {}}
    for kind in {r["kind"] for r in results}:
        rs = [r for r in results if r["kind"] == kind]
        n, p = len(rs), sum(r["passed"] for r in rs)
        modes = collections.Counter()
        for r in rs:
            if r["passed"]:
                continue
            if not r["claimed"]:
                modes[f"not_finished:{r['ended_by']}"] += 1
            for f in r["failures"]:
                modes[f.split(":")[0]] += 1
        out["by_kind"][kind] = {"n": n, "passed": p, "pass_rate": round(p / n, 3), "ci95": wilson(p / n, n),
                                "agrees_with_history": round(sum(r["agrees_with_history"] for r in rs) / n, 3),
                                "history_overrides_in_sample": sum(r["history_was_override"] for r in rs),
                                "failure_modes": dict(modes.most_common()), "mean_usd": round(statistics.mean(r["usd"] for r in rs), 4),
                                "mean_steps": round(statistics.mean(r["steps"] for r in rs), 1)}
    jdump(f"replay_{slug(llm.MODELS['solver'])}.json", out)
    if not alt:
        jdump("replay.json", out)
    return out


# ---------------- 6. operate & bill (live backlog) ----------------

def operate(admission=None, replay=None, pricing=None):
    adm, rep, pr = admission or jload("admission.json"), replay or jload("replay.json"), pricing or jload("pricing.json")
    conn = policy.connect(BASE)
    cases = []
    for kind, w in adm["workflows"].items():
        if w.get("admitted"):
            cases += [(kind, dict(r)) for r in conn.execute("select * from exceptions where kind=? and status='open' order by opened_at limit ?", (kind, OPERATE_PER_KIND))]
    conn.close()
    live = sandbox("live")
    lp = SANDBOXES / "live.db"

    def run(kx):
        kind, x = kx
        actor = f"agent:solver:{x['id']}"
        model = pr["workflows"][kind].get("solver_model")
        t = agents.solve({k: x[k] for k in ("id", "kind", "entity_type", "entity_id", "opened_at")}, live, actor, model=model)
        cs = policy.connect(lp)
        claimed = t["ended_by"] == "resolve_exception"
        v = policy.verify(cs, kind, x["entity_id"], actor=actor, exception_id=x["id"]) if claimed else None
        billed = bool(v and v["passed"])
        price = pr["workflows"][kind]["price_per_outcome_usd"]
        if billed:
            cs.execute("insert into billing_ledger(ts,exception_id,kind,outcome,price,evidence) values(?,?,?,?,?,?)",
                       (dt.datetime.now(dt.timezone.utc).isoformat(), x["id"], kind, "verified", price, json.dumps(v)))
        else:
            if claimed:  # the agent claimed done but the meter disagrees: reopen for a human, unbilled
                cs.execute("update exceptions set status='escalated', resolution=? where id=?",
                           (json.dumps({"escalation_reason": "verifier_failed", "failures": v["failures"]}), x["id"]))
            cs.execute("insert into billing_ledger(ts,exception_id,kind,outcome,price,evidence) values(?,?,?,?,?,?)",
                       (dt.datetime.now(dt.timezone.utc).isoformat(), x["id"], kind, "routed_to_human", 0.0,
                        json.dumps({"ended_by": t["ended_by"], "failures": (v or {}).get("failures"), "final": t["final"]})))
        cs.commit()
        cs.close()
        return {"exception_id": x["id"], "kind": kind, "model": model, "ended_by": t["ended_by"], "billed": billed, "price": price if billed else 0.0,
                "failures": (v or {}).get("failures", []), "escalation_reason": t["final"] if t["ended_by"] == "escalate_exception" else None,
                "usd": t["usd"], "steps": len(t["steps"])}

    results = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(run, cases):
            results.append(r)
            print(f"  live   {r['kind']:20s} #{r['exception_id']:<5} {'BILLED $%.2f' % r['price'] if r['billed'] else 'HUMAN  ':12s} {r['ended_by']}", flush=True)
    out = {"sandbox": "live", "cases": results, "by_kind": {}, "stop_rule": {"shadow_gap_max_pts": SHADOW_GAP_MAX, "tripped": []}}
    for kind in {r["kind"] for r in results}:
        rs = [r for r in results if r["kind"] == kind]
        n, b = len(rs), sum(r["billed"] for r in rs)
        live_rate = b / n
        replay_rate = pr["workflows"][kind]["pass_rate"]  # the priced tier's replay rate
        gap = (replay_rate - live_rate) * 100
        if gap > SHADOW_GAP_MAX:
            out["stop_rule"]["tripped"].append(kind)
        out["by_kind"][kind] = {"n": n, "billed": b, "model": pr["workflows"][kind].get("solver_model"), "metered_pass_rate": round(live_rate, 3),
                                "billed_usd": round(sum(r["price"] for r in rs), 2), "replay_pass_rate": replay_rate, "gap_pts": round(gap, 1),
                                "routed_to_human": [{"exception_id": r["exception_id"], "why": r["escalation_reason"] or ",".join(r["failures"]) or r["ended_by"]} for r in rs if not r["billed"]],
                                "model_usd": round(sum(r["usd"] for r in rs), 4)}
    # return path: what the agents could not finish comes back as candidate work
    returned = collections.Counter()
    for r in results:
        if r["billed"]:
            continue
        if r["escalation_reason"]:
            why = "escalated by agent"
        elif r["failures"]:
            why = "verifier rejected: " + ", ".join(sorted({f.split(":")[0] for f in r["failures"]}))
        else:
            why = f"not finished ({r['ended_by']})"
        returned[f"{r['kind']}: {why}"] += 1
    out["new_candidate_tasks"] = [{"pattern": k, "count": v} for k, v in returned.most_common()]
    out["billing_total_usd"] = round(sum(r["price"] for r in results), 2)
    jdump("operate.json", out)
    return out


# ---------------- pricing ----------------

def tiers():
    """All solver tiers that have replayed the held-out cases: model -> by_kind."""
    out = {}
    for f in sorted(OUT.glob("replay_*.json")):
        d = json.loads(f.read_text())
        out[d["model"]] = d["by_kind"]
    return out


def choose_tier(kind, default_model, all_tiers):
    """Cheapest tier whose replay pass rate meets the contract bar; else the best-passing tier."""
    ok = [(t[kind]["mean_usd"], m) for m, t in all_tiers.items() if kind in t and t[kind]["pass_rate"] >= CONTRACT_BAR]
    if ok:
        return min(ok)[1]
    best = [(t[kind]["pass_rate"], -t[kind]["mean_usd"], m) for m, t in all_tiers.items() if kind in t]
    return max(best)[2] if best else default_model


def price(candidates=None, admission=None, replay=None):
    cands, adm, rep = candidates or jload("candidates.json"), admission or jload("admission.json"), replay or jload("replay.json")
    all_tiers = tiers() or {rep["model"]: rep["by_kind"]}
    rec = {c["kind"]: c for c in cands["candidates"] if c.get("records")}
    out = {"assumptions": {"price_fraction_of_current_cost": PRICE_FRACTION, "serve_overhead_on_model_spend": SERVE_OVERHEAD, "contract_bar": CONTRACT_BAR,
                           "annual_minimum_fraction": ANNUAL_MIN_FRACTION, "ops_allowance_per_outcome_usd": OPS_ALLOWANCE,
                           "discovery_run": "fixed fee, credited against first-year outcome fees (not counted as revenue)"}, "workflows": {}}
    for kind, w in adm["workflows"].items():
        if not w.get("admitted"):
            continue
        model = choose_tier(kind, rep["model"], all_tiers)
        r, rp = rec[kind]["records"], all_tiers.get(model, rep["by_kind"])[kind]
        cost_now = r["current_unit_cost_usd"]
        p = round(cost_now * PRICE_FRACTION, 2)
        # model spend is paid on every attempt but only verified outcomes bill, so cost to serve is per verified outcome
        per_verified = rp["mean_usd"] / max(rp["pass_rate"], 0.05)
        serve = round(per_verified * (1 + SERVE_OVERHEAD) + OPS_ALLOWANCE, 4)
        vol = r["monthly_volume"]
        out["workflows"][kind] = {
            "solver_model": model, "tiers_considered": {m: t[kind]["pass_rate"] for m, t in all_tiers.items() if kind in t},
            "monthly_volume": vol, "avg_handling_minutes": r["avg_handling_minutes"], "current_unit_cost_usd": cost_now,
            "price_per_outcome_usd": p, "pass_rate": rp["pass_rate"], "pass_rate_ci95": rp["ci95"],
            "measured_model_cost_per_attempt_usd": rp["mean_usd"], "model_cost_per_verified_outcome_usd": round(per_verified, 4), "cost_to_serve_usd": serve,
            "gross_margin": round((p - serve) / p, 3) if p else None,
            "projected_monthly_revenue_usd": round(vol * rp["pass_rate"] * p, 2),
            "projected_monthly_customer_saving_usd": round(vol * rp["pass_rate"] * (cost_now - p), 2),
            "proposed_annual_minimum_outcomes": int(12 * vol * rp["ci95"][0] * ANNUAL_MIN_FRACTION),
            "meets_contract_bar": rp["pass_rate"] >= CONTRACT_BAR}
    jdump("pricing.json", out)
    return out


# ---------------- proof packet ----------------

def packet():
    cands, adm, rep, pr, op = (jload(n) for n in ("candidates.json", "admission.json", "replay.json", "pricing.json", "operate.json"))
    settings = agents.api("GET", "/settings")
    rec = {c["kind"]: c for c in cands["candidates"]}
    workflows = []
    for kind, w in adm["workflows"].items():
        wf = {"kind": kind, "title": rec.get(kind, {}).get("title", kind), "description": rec.get(kind, {}).get("description"),
              "needs_judgement": rec.get(kind, {}).get("needs_judgement"), "evidence": rec.get(kind, {}).get("records"),
              "verifier": w.get("verifier"), "admission": {k: v for k, v in w.items() if k not in ("verifier",)}}
        if w.get("admitted"):
            model = pr["workflows"][kind].get("solver_model", rep["model"])
            wf["replay"] = tiers().get(model, rep["by_kind"])[kind]
            wf["replay"]["model"] = model
            wf["pricing"] = pr["workflows"][kind]
            wf["live"] = op["by_kind"].get(kind)
        workflows.append(wf)
    admitted = [w for w in workflows if w["admission"]["admitted"]]
    spend = (cands.get("usd") or 0.0) + sum(r["usd"] for w in adm["workflows"].values() for r in (w.get("llm_breaker") or {}).get("runs", []))
    spend += sum(c["usd"] for f in OUT.glob("replay_*.json") for c in json.loads(f.read_text())["cases"]) + sum(r["usd"] for r in op["cases"])
    tier_table = {m: {k: {"pass_rate": v["pass_rate"], "ci95": v["ci95"], "mean_usd": v["mean_usd"], "mean_steps": v["mean_steps"], "failure_modes": v["failure_modes"]}
                      for k, v in by_kind.items()} for m, by_kind in tiers().items()}
    p = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "company": settings.get("company"), "as_of": settings.get("as_of"),
         "models": llm.MODELS, "thresholds": {**adm["thresholds"], **pr["assumptions"], "shadow_gap_max_pts": SHADOW_GAP_MAX},
         "gates": {"five_verifiers_survive_breaker": len(admitted) >= 5, "admitted_count": len(admitted), "stop_rule_tripped": op["stop_rule"]["tripped"]},
         "workflows": workflows, "solver_tiers": tier_table, "new_candidate_tasks": op["new_candidate_tasks"],
         "totals": {"projected_monthly_revenue_usd": round(sum(w["pricing"]["projected_monthly_revenue_usd"] for w in admitted), 2),
                    "live_billed_usd": op["billing_total_usd"], "discovery_model_spend_usd": round(spend, 4)}}
    jdump("proof_packet.json", p)
    (OUT / "proof_packet.md").write_text(render_md(p))
    return p


def render_md(p):
    L = [f"# Proof packet — {p['company']} (as of {p['as_of']})", "",
         f"Generated {p['generated_at'][:19]}Z. Proposer `{p['models']['proposer']}`, solver `{p['models']['solver']}`, "
         f"breaker `{p['models']['breaker']}`, verifier = code keyed to the system of record.", "",
         f"**Gate — at least five verifiers survive the breaker: {'PASS' if p['gates']['five_verifiers_survive_breaker'] else 'FAIL'}** "
         f"({p['gates']['admitted_count']} admitted). Stop rule tripped: {p['gates']['stop_rule_tripped'] or 'none'}.", "",
         "| Workflow | Monthly vol | Unit cost now | Admitted | Priced tier | Replay pass (n=20) | Price/outcome | Cost to serve | Margin | Live metered | Proj. monthly rev |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for w in p["workflows"]:
        a, e = w["admission"], w.get("evidence") or {}
        if a["admitted"]:
            r, pr, lv = w["replay"], w["pricing"], w.get("live") or {}
            L.append(f"| {w['kind']} | {e.get('monthly_volume')} | ${e.get('current_unit_cost_usd')} | yes | `{pr['solver_model'].split('/')[-1]}` | {r['pass_rate']:.0%} {r['ci95']} | ${pr['price_per_outcome_usd']} | "
                     f"${pr['cost_to_serve_usd']:.3f} | {pr['gross_margin']:.0%} | {lv.get('metered_pass_rate', 0):.0%} ({lv.get('billed')}/{lv.get('n')}) | ${pr['projected_monthly_revenue_usd']} |")
        else:
            L.append(f"| {w['kind']} | {e.get('monthly_volume')} | ${e.get('current_unit_cost_usd')} | **no** | — | — | — | — | — | — | — |")
    L.append("")
    for w in p["workflows"]:
        a = w["admission"]
        L += [f"## {w['kind']} — {w['title']}", "", w.get("description") or "", "",
              f"*Why it needs judgement (proposer):* {w.get('needs_judgement')}", "",
              f"**Verifier.** {w['verifier']}", "",
              f"**Admission.** Verifier agrees with human history on {a['history_agreement']:.1%} of {a['history_n']} resolved cases. "
              f"Record-blind shortcuts (n={a['best_blind'].get('n', 25)}): " + ", ".join(f"{k} {v:.0%}" for k, v in a["blind_strategies"].items()) +
              f". LLM breaker ({a['llm_breaker']['model']}, write-only tools): {a['llm_breaker']['pass_rate']:.0%} of {a['llm_breaker']['n']}. "
              f"Best blind {a['best_blind']['pass_rate']:.0%} (95% CI {a['best_blind']['ci95'][0]:.0%}–{a['best_blind']['ci95'][1]:.0%}) vs. screen-out threshold {p['thresholds']['blind_pass_max']:.0%} on the lower bound. "
              f"→ **{'ADMITTED' if a['admitted'] else 'NOT ADMITTED'}**" + (f": {a['reason']}" if a["reason"] else "") + "", ""]
        if a["admitted"]:
            r, pr, lv = w["replay"], w["pricing"], w.get("live") or {}
            L += [f"**Replay on {r['n']} held-out past cases** (priced tier `{r.get('model', p['models']['solver'])}`). Pass rate {r['pass_rate']:.0%} (95% CI {r['ci95'][0]:.0%}–{r['ci95'][1]:.0%}); "
                  f"agrees with the clerk's recorded outcome {r['agrees_with_history']:.0%} ({r['history_overrides_in_sample']} clerk overrides in sample). "
                  f"Mean {r['mean_steps']} tool calls, ${r['mean_usd']:.4f} model spend per case.", "",
                  "Known failure modes: " + (", ".join(f"{k} ×{v}" for k, v in r["failure_modes"].items()) or "none observed") + ".", "",
                  f"**Price.** Current unit cost ${pr['current_unit_cost_usd']} ({pr['avg_handling_minutes']} clerk-minutes). Price per verified outcome "
                  f"${pr['price_per_outcome_usd']}. Cost to serve ${pr['cost_to_serve_usd']:.3f} (measured ${pr['measured_model_cost_per_attempt_usd']:.4f} model spend per attempt "
                  f"÷ {pr['pass_rate']:.0%} verified = ${pr['model_cost_per_verified_outcome_usd']:.4f}, +{p['thresholds']['serve_overhead_on_model_spend']:.0%} overhead, "
                  f"+${p['thresholds']['ops_allowance_per_outcome_usd']} operations allowance) → gross margin {pr['gross_margin']:.0%}. "
                  f"At {pr['monthly_volume']}/month and {pr['pass_rate']:.0%} verified: ${pr['projected_monthly_revenue_usd']}/month revenue, "
                  f"${pr['projected_monthly_customer_saving_usd']}/month customer saving. Contract bar {p['thresholds']['contract_bar']:.0%}: "
                  f"{'met' if pr['meets_contract_bar'] else 'NOT met'}."
                  + (f" Proposed annual minimum: {pr['proposed_annual_minimum_outcomes']:,} verified outcomes." if "proposed_annual_minimum_outcomes" in pr else ""), ""]
            if lv:
                L += [f"**Live backlog (metered).** {lv['billed']}/{lv['n']} verified and billed (${lv['billed_usd']}); gap to replay {lv['gap_pts']:+.1f} pts. "
                      f"Routed to a human unbilled: " + ("; ".join(f"#{h['exception_id']} {h['why']}" for h in lv["routed_to_human"]) or "none") + ".", ""]
    if len(p.get("solver_tiers", {})) > 1:
        kinds = [w["kind"] for w in p["workflows"] if w["admission"]["admitted"]]
        L += ["## Solver tiers on the same held-out cases", "", "| Model | " + " | ".join(kinds) + " |", "|---|" + "---|" * len(kinds)]
        for m, t in p["solver_tiers"].items():
            L.append(f"| `{m}` | " + " | ".join(f"{t[k]['pass_rate']:.0%} @ ${t[k]['mean_usd']:.4f}" if k in t else "—" for k in kinds) + " |")
        L += ["", "Each workflow is priced on the cheapest tier that meets the contract bar (else the best-passing tier). Pass rate @ measured model spend per attempt.", ""]
    L += ["## Return path — what came back as candidate work", ""]
    L += [f"- {t['pattern']} (×{t['count']})" for t in p["new_candidate_tasks"]] or ["- nothing routed to humans"]
    t = p["totals"]
    L += ["", "## Totals", "", f"- Projected monthly revenue across admitted workflows: **${t['projected_monthly_revenue_usd']}**",
          f"- Billed on the live backlog sample: ${t['live_billed_usd']}",
          f"- Model spend for this discovery run (proposer + breaker + both solver tiers + live): ${t['discovery_model_spend_usd']}", "",
          "Thresholds (proposals, not findings): " + json.dumps(p["thresholds"]), ""]
    return "\n".join(L)


def run_all():
    print("explore/propose"); c = explore()
    print(f"  {len(c['candidates'])} candidates, {sum(x['evidence_ok'] for x in c['candidates'])} with evidence from records (${c['usd']})")
    print("admit"); a = admit(c)
    for k, w in a["workflows"].items():
        print(f"  {k:20s} {'ADMITTED' if w['admitted'] else 'screened'}  history {w.get('history_agreement', 0):.0%}  best blind {w.get('best_blind', {}).get('pass_rate', 0):.0%}  llm breaker {w.get('llm_breaker', {}).get('pass_rate', 0):.0%}")
    print("prove (replay)"); r = prove(a)
    for k, w in r["by_kind"].items():
        print(f"  {k:20s} pass {w['pass_rate']:.0%} {w['ci95']}  ${w['mean_usd']:.4f}/case")
    print("price"); pz = price(c, a, r)
    print("operate (live backlog)"); o = operate(a, r, pz)
    print(f"  billed ${o['billing_total_usd']}  stop-rule tripped: {o['stop_rule']['tripped'] or 'none'}")
    p = packet()
    print(f"packet -> {OUT/'proof_packet.md'}  spend ${llm.COST.snapshot()['usd']}")
    return p
