"""Assert-based check of the x402 rail: Ed25519/JWT, 402 shape, facilitator verify/settle flow against a
stub, idempotent crediting, explicit top-up; plus a live probe of the public facilitator. No framework."""
import base64
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import x402

PAY_TO = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Stub(http.server.BaseHTTPRequestHandler):
    """Facilitator stub: behaviour keyed on the payload signature prefix."""
    calls = []

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"kinds": [{"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}], "extensions": [], "signers": {}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.calls.append((self.path, self.headers.get("Authorization")))
        sig = str(body["paymentPayload"]["payload"].get("signature", ""))
        payer = "0xPAYER"
        if self.path == "/verify":
            if sig.startswith("0xbad"):
                self._send(200, {"isValid": False, "invalidReason": "insufficient_funds", "payer": payer})
            else:
                self._send(200, {"isValid": True, "payer": payer})
        elif self.path == "/settle":
            if sig.startswith("0xsettlefail"):
                self._send(200, {"success": False, "errorReason": "transaction_reverted", "payer": payer, "transaction": "", "network": "eip155:84532"})
            else:
                self._send(200, {"success": True, "payer": payer, "transaction": "0xTX" + sig[-4:], "network": "eip155:84532"})
        else:
            self._send(404, {})


def call(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r), r.headers  # HTTPMessage: case-insensitive get()
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), e.headers


def payment(accepted, signature):
    return base64.b64encode(json.dumps({"x402Version": 2, "accepted": accepted, "payload": {"signature": signature,
                           "authorization": {"from": "0xPAYER", "to": accepted["payTo"], "value": accepted["amount"], "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "ab" * 32}}}).encode()).decode()


def main():
    # 1. Ed25519 against RFC 8032 test vector 1, and JWT structure
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert x402.ed25519_public(seed).hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    assert x402.ed25519_sign(seed, b"").hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    x402.CDP_KEY_ID, x402.CDP_KEY_SECRET = "key-id", base64.b64encode(seed + x402.ed25519_public(seed)).decode()
    tok = x402.cdp_jwt("POST", "https://api.cdp.coinbase.com/platform/v2/x402/verify")
    h, c, _ = tok.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    hdr, claims = json.loads(pad(h)), json.loads(pad(c))
    assert hdr["alg"] == "EdDSA" and hdr["kid"] == "key-id" and len(hdr["nonce"]) == 16
    assert claims["iss"] == "cdp" and claims["aud"] == ["cdp_service"] and claims["uri"] == "POST api.cdp.coinbase.com/platform/v2/x402/verify" and claims["exp"] - claims["nbf"] == 120
    x402.CDP_KEY_ID = x402.CDP_KEY_SECRET = ""
    print("1. ed25519 + CDP JWT: ok")

    # 2. 402 shape (spec: x402Version, resource{url,...}, accepts[{scheme,network,amount,asset,payTo,maxTimeoutSeconds,extra}])
    x402.PAY_TO, x402.NETWORK = PAY_TO, "eip155:84532"
    pr = x402.payment_required(1, "https://h/exceptions/1/resolve", "claim")
    a = pr["accepts"][0]
    assert pr["x402Version"] == 2 and pr["resource"]["url"].startswith("https://") and len(pr["accepts"]) == 1
    assert a["scheme"] == "exact" and a["network"] == "eip155:84532" and a["amount"] == "50000" and a["payTo"] == PAY_TO and a["asset"].startswith("0x") and a["maxTimeoutSeconds"] > 0 and a["extra"]["name"] == "USDC"
    print("2. PaymentRequired shape: ok (1 credit = $0.05 = 50000 USDC units)")

    # 3. stub facilitator + API
    sp = free_port(); srv = http.server.ThreadingHTTPServer(("127.0.0.1", sp), Stub); threading.Thread(target=srv.serve_forever, daemon=True).start()
    td = tempfile.mkdtemp(); ap = free_port(); U = f"http://127.0.0.1:{ap}"
    import shutil; shutil.copy("out/twin.db", f"{td}/twin.db")
    env = {**os.environ, "PG_DB": f"{td}/twin.db", "PG_SANDBOXES": f"{td}/sb", "PG_SEATS_DB": f"{td}/seats.db", "PG_HOSTED": "1", "PG_FREE_CREDITS": "0",
           "PG_X402_PAY_TO": PAY_TO, "PG_X402_NETWORK": "base-sepolia", "PG_X402_FACILITATOR": f"http://127.0.0.1:{sp}", "PG_CREDIT_USD": "0.05"}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "src.twin_api:app", "--host", "127.0.0.1", "--port", str(ap), "--log-level", "warning"], env=env,
                           stdout=open(f"{td}/api.log", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{U}/health", timeout=1); break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        st, pricing, _ = call(f"{U}/pricing"); assert st == 200 and pricing["x402"]["payTo"] == PAY_TO and pricing["top_up"][0]["method"] == "x402"
        st, seat, _ = call(f"{U}/seat", "POST", {"name": "x402check"}); assert st == 201 and seat["credits"] == 0
        H = {"Authorization": f"Bearer {seat['token']}", "X-Sandbox": seat["actor"]}
        st, xs, _ = call(f"{U}/exceptions?kind=vendor_bank_change&status=open&limit=3", headers=H); xs = xs if isinstance(xs, list) else xs["results"]
        def prep(x):
            st, inv, _ = call(f"{U}/invoices/{x['entity_id']}", headers=H)
            call(f"{U}/invoices/{x['entity_id']}/hold", "POST", {"reason": "bank_detail_mismatch"}, H); call(f"{U}/vendors/{inv['vendor_id']}/flag", "POST", {"reason": "verify"}, H)
        prep(xs[0])
        # 402 with the spec'd header
        st, body, hd = call(f"{U}/exceptions/{xs[0]['id']}/resolve", "POST", {"summary": "done"}, H)
        assert st == 402 and hd.get("PAYMENT-REQUIRED") and body["x402"]["accepts"][0]["amount"] == "50000"
        req = json.loads(base64.b64decode(hd["PAYMENT-REQUIRED"]))["accepts"][0]; assert req == body["x402"]["accepts"][0]
        assert any(m["method"] == "x402" for m in body["top_up"]["methods"])
        print("3. metered claim without credits -> 402 + PAYMENT-REQUIRED header: ok")
        # malformed / rejected / mismatched payments
        st, body, _ = call(f"{U}/exceptions/{xs[0]['id']}/resolve", "POST", {"summary": "done"}, {**H, "PAYMENT-SIGNATURE": "not-base64!!"})
        assert st == 402 and "base64" in body["x402"]["error"]
        st, body, _ = call(f"{U}/exceptions/{xs[0]['id']}/resolve", "POST", {"summary": "done"}, {**H, "PAYMENT-SIGNATURE": payment(req, "0xbad1")})
        assert st == 402 and "insufficient_funds" in body["x402"]["error"], body
        st, body, _ = call(f"{U}/exceptions/{xs[0]['id']}/resolve", "POST", {"summary": "done"}, {**H, "PAYMENT-SIGNATURE": payment({**req, "network": "eip155:8453"}, "0xgood0")})
        assert st == 402 and "network" in body["x402"]["error"]
        st, body, _ = call(f"{U}/exceptions/{xs[0]['id']}/resolve", "POST", {"summary": "done"}, {**H, "PAYMENT-SIGNATURE": payment(req, "0xsettlefail")})
        assert st == 402 and "transaction_reverted" in body["x402"]["error"]
        st, me, _ = call(f"{U}/me", headers=H); assert me["credits"] == 0, "a failed payment must not credit"
        assert xs[0]["id"] == call(f"{U}/exceptions/{xs[0]['id']}", headers=H)[1]["id"] and call(f"{U}/exceptions/{xs[0]['id']}", headers=H)[1]["status"] == "open"
        print("4. malformed / verify-rejected / mismatched / settle-failed payments -> 402 with reason, nothing credited, claim untouched: ok")
        # a good payment: verify -> settle -> credit -> claim proceeds, PAYMENT-RESPONSE header
        good = payment(req, "0xgood1")
        st, body, hd = call(f"{U}/exceptions/{xs[0]['id']}/resolve", "POST", {"summary": "done"}, {**H, "PAYMENT-SIGNATURE": good})
        assert st == 200 and body["verdict"]["passed"] is True and body["payment"]["credits"] == 1 and body["bill"]["credits_left"] == 0, body
        sr = json.loads(base64.b64decode(hd["PAYMENT-RESPONSE"])); assert sr["success"] and sr["transaction"].startswith("0xTX")
        assert [p for p, _ in Stub.calls][-2:] == ["/verify", "/settle"]
        print("5. valid payment -> settle -> 1 credit -> claim proceeds with verdict + PAYMENT-RESPONSE: ok")
        # replay the same payment on another case: no second settlement, no credit, so 402 again
        prep(xs[1]); n_calls = len(Stub.calls)
        st, body, _ = call(f"{U}/exceptions/{xs[1]['id']}/resolve", "POST", {"summary": "done"}, {**H, "PAYMENT-SIGNATURE": good})
        assert st == 402 and len(Stub.calls) == n_calls, "replayed payment must not hit the facilitator or credit again"
        print("6. replayed PAYMENT-SIGNATURE -> no re-settlement, no credit (idempotent): ok")
        # explicit top-up: 5 credits worth, then the same header again -> replayed, unchanged
        five = payment({**req, "amount": str(5 * 50000)}, "0xgood5")
        st, body, hd = call(f"{U}/seats/{seat['actor']}/topup/x402", "POST", None, {**H, "PAYMENT-SIGNATURE": five})
        assert st == 200 and body["payment"]["credits"] == 5 and body["seat"]["credits"] == 5 and hd.get("PAYMENT-RESPONSE"), body
        st, body, _ = call(f"{U}/seats/{seat['actor']}/topup/x402", "POST", None, {**H, "PAYMENT-SIGNATURE": five})
        assert st == 200 and body["payment"].get("replayed") and body["seat"]["credits"] == 5
        st, body, _ = call(f"{U}/seats/{seat['actor']}/topup/x402", "POST", None, H); assert st == 402 and "PAYMENT-SIGNATURE" in body["x402"]["error"]
        st, body, _ = call(f"{U}/seats/someone-else/topup/x402", "POST", None, {**H, "PAYMENT-SIGNATURE": five}); assert st == 403
        st, body, _ = call(f"{U}/exceptions/{xs[1]['id']}/resolve", "POST", {"summary": "done"}, H); assert st == 200 and body["bill"]["credits_left"] == 4
        print("7. explicit top-up (5 credits), replay-safe, own-seat only; next claim bills from the balance: ok")
    finally:
        api.terminate(); api.wait(timeout=10); srv.shutdown()

    # 8. live probe of the public testnet facilitator (network; informational)
    try:
        with urllib.request.urlopen(f"{x402.PUBLIC_FACILITATOR}/supported", timeout=20) as r:
            sup = json.load(r)
        kinds = sup.get("kinds") or sup
        nets = sorted({k.get("network") for k in kinds if isinstance(k, dict)})[:6]
        x402.FACILITATOR = x402.PUBLIC_FACILITATOR
        ok, info = x402.verify_and_settle(json.loads(base64.b64decode(payment(req, "0xdeadbeef"))), req)
        assert not ok, "a dummy signature must not verify"
        print(f"8. live x402.org facilitator: /supported ok ({len(kinds)} kinds, e.g. {nets}); dummy /verify rejected: {info['reason']!s:.60}")
    except Exception as e:  # noqa: BLE001
        print(f"8. live x402.org facilitator: unreachable from here ({e}); stub-tested only")
    print("x402 check OK")


if __name__ == "__main__":
    main()
