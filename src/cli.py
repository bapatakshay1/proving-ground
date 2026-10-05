"""python -m src.cli <command>  — the whole loop is driven from here."""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.request

from . import billing, llm, loop, seed, selfcheck

PORT = int(os.environ.get("PG_PORT", "8765"))
PID = pathlib.Path("out/api.pid")


def up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def serve(background=False):
    if up():
        print(f"twin API already up on :{PORT}")
        return None
    cmd = [sys.executable, "-m", "uvicorn", "src.twin_api:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"]
    if not background:
        os.execvp(cmd[0], cmd)
    pathlib.Path("out").mkdir(exist_ok=True)
    p = subprocess.Popen(cmd, stdout=open("out/api.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)
    PID.write_text(str(p.pid))
    for _ in range(60):
        if up():
            print(f"twin API up on :{PORT} (pid {p.pid})")
            return p
        time.sleep(0.25)
    raise SystemExit("twin API failed to start; see out/api.log")


def stop():
    if PID.exists():
        try:
            os.kill(int(PID.read_text()), signal.SIGTERM)
            print("twin API stopped")
        except ProcessLookupError:
            pass
        PID.unlink()


def main():
    ap = argparse.ArgumentParser(prog="proving-ground")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="build the synthetic twin (out/twin.db)").add_argument("--seed", type=int, default=7)
    sub.add_parser("selfcheck", help="assert-based check of seeder + verifiers + replay round-trip")
    sub.add_parser("serve", help="run the sealed twin API (foreground)")
    sub.add_parser("stop", help="stop a background twin API")
    sub.add_parser("explore", help="explorer/proposer -> out/candidates.json")
    sub.add_parser("admit", help="verifier soundness + record-blind breaker -> out/admission.json")
    sub.add_parser("prove", help="solver replays held-out cases -> out/replay.json").add_argument("--alt", action="store_true", help="comparison tier only (PG_SOLVER_MODEL)")
    sub.add_parser("price", help="-> out/pricing.json")
    sub.add_parser("operate", help="live backlog, metered and billed -> out/operate.json")
    sub.add_parser("packet", help="render out/proof_packet.{json,md}")
    sub.add_parser("run-all", help="the whole loop; starts the API if needed")
    sub.add_parser("cost", help="print model spend for this process")
    st = sub.add_parser("statement", help="monthly customer statement + our cost side, from the billing ledger")
    st.add_argument("--db", default="out/sandboxes/live.db"); st.add_argument("--period", default=None, help="YYYY-MM (default: latest in ledger)")
    st.add_argument("--discovery-credit", type=float, default=0.0, help="unused discovery-fee credit to apply")
    a = ap.parse_args()
    if a.cmd == "seed":
        print(json.dumps(seed.build("out/twin.db", a.seed), indent=1))
    elif a.cmd == "selfcheck":
        selfcheck.main("out/twin.db")
    elif a.cmd == "serve":
        serve()
    elif a.cmd == "stop":
        stop()
    elif a.cmd == "statement":
        print(billing.statement(a.db, a.period, a.discovery_credit)["markdown"])
    elif a.cmd == "run-all":
        started = serve(background=True)
        try:
            loop.run_all()
        finally:
            if started:
                started.terminate()
                PID.unlink(missing_ok=True)
    else:
        if not up():
            raise SystemExit(f"twin API is not up on :{PORT}; run `python -m src.cli serve` first")
        fn = {"explore": loop.explore, "admit": loop.admit, "prove": lambda: loop.prove(alt=a.alt), "price": loop.price, "operate": loop.operate, "packet": loop.packet,
              "cost": lambda: llm.COST.snapshot()}[a.cmd]
        out = fn()
        if a.cmd in ("cost", "price"):
            print(json.dumps(out, indent=1))
        elif a.cmd == "packet":
            print(open("out/proof_packet.md").read())
        print(f"model spend this process: ${llm.COST.snapshot()['usd']}")


if __name__ == "__main__":
    main()
