# Proving Ground — demo loop

A working build of the Phase 2 "demo loop" from *Proving Ground: Design Thesis* (Oct 4, 2026):
agents explore a sealed replica of a customer's finance-ops system, prove which exception workflows
they can run, and the verifier that admits each task becomes the meter that bills for it.

```
1. Twin ─────▶ 2. Explore ─────▶ 3. Propose
                                      │  exceptions become new tasks
4. Admit ─────▶ 5. Prove & price ──▶ 6. Operate & bill  ◀──────────┘
```

Output: `out/proof_packet.md` — one proof packet per workflow (monthly volume, current unit cost,
pass rate on held-out past cases with 95% CI, known failure modes, per-outcome price, live metered
results), plus the admission report showing which verifiers survived the breaker.

## The three design rules, as code

| Rule (thesis) | Where |
|---|---|
| **Tasks come from the customer's records.** A candidate is admitted only with evidence of real volume. | `agents.propose` must cite volume + unit cost; `loop.explore` checks every number against `/stats/exceptions`. |
| **No agent grades its own work.** Proposer, solver, verifier, breaker are separate, from different model families. | Proposer `google/gemini-2.5-flash`, solver `openai/gpt-4.1-mini` (tier `openai/gpt-4.1` compared), breaker `deepseek/deepseek-chat-v3-0324`, verifier = code (`policy.py`). |
| **Proof is replay, not a judge's opinion.** | `loop.prove` reopens 20 held-out historical cases per workflow in sandbox copies; `policy.verify` recomputes the required end state from the system of record and checks the audit log for out-of-scope writes. No LLM judges anything. |

The breaker is **record-blind**: scripted shortcut strategies plus an LLM with write-only tools. A task is
admitted only if (a) the verifier agrees with ≥90% of how humans actually resolved past cases and (b) no
blind strategy reaches 70% on it. `vendor_bank_change` is deliberately a fixed-rule control ("hold + flag"):
the breaker passes it blind, so the gate screens it out of outcome pricing — that is the gate working.

## Run it

```
pip install -r requirements.txt          # fastapi, uvicorn (everything else is stdlib)
python -m src.cli seed                   # deterministic synthetic AP twin: out/twin.db (12 months, ~8.7k invoices, 3,960 exceptions)
python -m src.cli selfcheck              # asserts: verifier agrees with history, truth stable, reopen/replay round-trips
python -m src.cli run-all                # explore → admit → prove → price → operate → packet (starts the API if needed)
python -m src.cli packet                 # re-render out/proof_packet.md
```

Model access: OpenRouter key in `$OPENROUTER_API_KEY` or `~/.config/agent-integrity/openrouter.key`.
Models are env-configurable (`PG_PROPOSER_MODEL`, `PG_SOLVER_MODEL`, `PG_BREAKER_MODEL`). A full run
costs about $2–3 of model spend; every call's real cost is recorded and flows into "cost to serve".

Individual steps: `serve`, `explore`, `admit`, `prove [--alt]`, `price`, `operate`, `packet`, `cost`.

## The twin

`twin_api.py` is the sealed boundary: agents only see HTTP. `X-Sandbox: <name>` selects an isolated
copy (`out/sandboxes/<name>.db`); `X-Actor` is recorded on every write. Harness-only columns
(`pre_state`, `post_state`, `truth`, `held_out`) are never served.

Six exception kinds, each with mixed outcomes so no constant answer passes: `price_mismatch`,
`quantity_mismatch`, `possible_duplicate`, `missing_po`, `unmatched_payment` (bank reconciliation break),
`vendor_bank_change`. History is produced through the same `actions.py` the agents use, with a 3% rate of
clerk "manager overrides" so verifier-vs-history agreement is realistic rather than 100%.

`ponytail:` the twin is a synthetic stand-in for an open-source ERP; the API surface is the adapter
boundary, so an Odoo/ERPNext backend would implement the same endpoints.

## Files

```
src/schema.sql    tables            src/policy.md   the customer's AP policy (served at /policy)
src/seed.py       synthetic twin    src/policy.py   expected outcomes + verifiers (code keyed to the system of record)
src/actions.py    all state changes src/twin_api.py sealed API
src/llm.py        OpenRouter + tool loop + $ tally     src/agents.py  proposer / solver / breaker
src/loop.py       admit / prove / price / operate / packet            src/cli.py  entry point
src/selfcheck.py  the one runnable check
```
