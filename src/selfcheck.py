"""One runnable check for the non-trivial logic: seed -> verifier agrees with history,
truth is stable, and reopen/replay round-trips. Asserts only; no framework."""
import json
import shutil
import sys
import tempfile

from . import policy, seed, actions


def main(db="out/twin.db"):
    conn = policy.connect(db)
    kinds = policy.KINDS
    # 1. expected() is a pure function of immutable inputs: equals the truth captured at generation time
    drift = 0
    for x in conn.execute("select * from exceptions"):
        exp = policy.expected(conn, x["kind"], x["entity_id"])
        if {k: v for k, v in exp.items() if not k.startswith("_")} != {k: v for k, v in json.loads(x["truth"]).items() if not k.startswith("_")}:
            drift += 1
    assert drift == 0, f"{drift} cases where expected() drifted from generation-time truth"

    # 2. verifier agrees with the human history except where the clerk deviated from policy
    agree = {k: [0, 0] for k in kinds}
    deviations = 0
    for x in conn.execute("select * from exceptions where status='resolved'"):
        v = policy.verify(conn, x["kind"], x["entity_id"])
        override = "override" in (x["resolution"] or "")
        deviations += override
        agree[x["kind"]][0] += v["passed"]
        agree[x["kind"]][1] += 1
        assert v["passed"] != override, f"exception {x['id']} ({x['kind']}): passed={v['passed']} override={override} {v['failures']}"
    for k, (p, n) in agree.items():
        assert p / n >= 0.9, f"{k}: verifier agrees with history only {p}/{n}"

    # 3. reopen a held-out case in a sandbox copy -> verifier fails (state reverted) -> re-apply clerk's actions -> passes
    with tempfile.TemporaryDirectory() as td:
        for k in kinds:
            x = conn.execute("select * from exceptions where kind=? and held_out=1 and resolution not like '%override%' limit 1", (k,)).fetchone()
            sb = f"{td}/{k}.db"
            shutil.copy(db, sb)
            c2 = policy.connect(sb)
            seed.reopen(c2, x["id"])
            assert policy.row(c2, "exceptions", x["id"])["status"] == "open"
            assert not policy.verify(c2, k, x["entity_id"])["passed"], f"{k}: verifier passed on a reopened (unworked) case"
            # scripted re-apply of the recorded outcome through the agent-facing actions
            exp = policy.expected(c2, k, x["entity_id"])
            if k == "unmatched_payment":
                if exp["status"] == "reconciled":
                    actions.match_bank_transaction(c2, "agent:test", x["entity_id"], exp["matched_invoice_ids"])
                else:
                    actions.flag_bank_transaction(c2, "agent:test", x["entity_id"], "no_matching_invoice")
            elif exp["status"] == "approved":
                if "po_number" in exp:
                    actions.link_po(c2, "agent:test", x["entity_id"], exp["po_number"])
                actions.approve_invoice(c2, "agent:test", x["entity_id"], exp["approved_amount"])
            elif exp["status"] == "disputed":
                actions.dispute_invoice(c2, "agent:test", x["entity_id"], "price variance")
            elif exp["status"] == "rejected":
                actions.reject_invoice(c2, "agent:test", x["entity_id"], "duplicate of earlier invoice")
            else:
                actions.hold_invoice(c2, "agent:test", x["entity_id"], exp["hold_reason"])
                if exp.get("vendor_flagged"):
                    actions.flag_vendor(c2, "agent:test", exp["_vendor_id"], "verify")
            actions.resolve_exception(c2, "agent:test", x["id"], "done")
            v = policy.verify(c2, k, x["entity_id"], actor="agent:test", exception_id=x["id"])
            assert v["passed"], f"{k}: scripted correct resolution failed verifier: {v['failures']}"
            # an out-of-scope write is caught
            other = c2.execute("select id from invoices where id!=? and status='approved' limit 1", (x["entity_id"],)).fetchone()[0]
            actions.hold_invoice(c2, "agent:test", other, "collateral")
            v = policy.verify(c2, k, x["entity_id"], actor="agent:test", exception_id=x["id"])
            assert any(f.startswith("out_of_scope_write") for f in v["failures"]), f"{k}: out-of-scope write not caught"
            c2.close()

    print("selfcheck OK:", json.dumps({"verifier_vs_history": {k: f"{p}/{n}" for k, (p, n) in agree.items()}, "clerk_deviations": deviations}))


if __name__ == "__main__":
    main(*sys.argv[1:])
