"""x402 seller side (V2): the diner's top-up rail. Credits stay the internal unit; this module turns a
settled USDC payment into credits and nothing else knows how money moved.

Spec used: github.com/coinbase/x402 specs/x402-specification-v2.md (PaymentRequired / PaymentPayload /
facilitator verify+settle shapes) and specs/transports-v2/http.md (headers PAYMENT-REQUIRED,
PAYMENT-SIGNATURE, PAYMENT-RESPONSE carry base64 JSON; 402 on required, 200 on success).
Facilitator REST: CDP  https://api.cdp.coinbase.com/platform/v2/x402/{verify,settle,supported}
(JWT bearer per docs.cdp.coinbase.com/api-reference/v2/authentication: EdDSA, kid, nonce, sub, iss=cdp,
aud=[cdp_service], nbf, exp=nbf+120, uri="METHOD host/path"); public testnet facilitator
https://x402.org/facilitator/{verify,settle,supported} (no auth). Pure-Python Ed25519 (RFC 8032) so the
CDP JWT needs no dependency.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

PAY_TO = os.environ.get("PG_X402_PAY_TO", "")
CREDIT_USD = float(os.environ.get("PG_CREDIT_USD", "0.05"))
NETWORKS = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}
NETWORK = NETWORKS.get(os.environ.get("PG_X402_NETWORK", "base"), os.environ.get("PG_X402_NETWORK", "base"))
# USDC (6 decimals). Base mainnet address matches the example in the V2 spec; Base Sepolia is Circle's testnet USDC.
ASSETS = {"eip155:8453": ("0x833589fCD6eDb6E08f4c7C32D4f71b1566469c18", {"name": "USDC", "version": "2"}),
          "eip155:84532": ("0x036CbD53842c5426634e7929541eC2318f3dCF7e", {"name": "USDC", "version": "2"})}
CDP_FACILITATOR = "https://api.cdp.coinbase.com/platform/v2/x402"
PUBLIC_FACILITATOR = "https://x402.org/facilitator"
FACILITATOR = os.environ.get("PG_X402_FACILITATOR") or (PUBLIC_FACILITATOR if NETWORK == "eip155:84532" else CDP_FACILITATOR)
CDP_KEY_ID, CDP_KEY_SECRET = os.environ.get("PG_CDP_API_KEY_ID", ""), os.environ.get("PG_CDP_API_KEY_SECRET", "")
MAX_TIMEOUT = 300


def enabled():
    return bool(PAY_TO) and NETWORK in ASSETS


def usd_to_atomic(usd):
    return str(int(round(usd * 1_000_000)))


def b64(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def unb64(s):
    return json.loads(base64.b64decode(s + "=" * (-len(s) % 4)))


def requirements(price_credits, resource_url, description):
    asset, extra = ASSETS[NETWORK]
    return {"scheme": "exact", "network": NETWORK, "amount": usd_to_atomic(price_credits * CREDIT_USD), "asset": asset,
            "payTo": PAY_TO, "maxTimeoutSeconds": MAX_TIMEOUT, "extra": extra,
            # not part of the spec'd schema but harmless for clients and useful for humans reading the 402
            }


# Bazaar / x402scan discovery shapes (docs.x402.org/extensions/bazaar.md: PaymentRequired.extensions.bazaar.info with
# input{type,...}/output; x402scan docs (nirholas/cryptocurrency.cv docs/x402scan-discovery.md): accepts[].outputSchema
# {input, output} and extensions.bazaar.schema). Both are emitted so either indexer can catalogue the resource.
BAZAAR_INFO = {
    "input": {"type": "http", "method": "POST", "bodyType": "json", "discoverable": True,
              "bodyFields": {"summary": {"type": "string", "description": "one-line summary of the resolution (resolve) or reason (escalate)"}},
              "headerFields": {"Authorization": "Bearer <seat token from POST /seat>", "X-Sandbox": "<your actor>"}},
    "output": {"type": "json", "example": {"ok": True, "exception_id": 123, "status": "resolved",
                                           "verdict": {"passed": True, "failure_classes": []},
                                           "bill": {"charged_credits": 1, "credits_left": 9}}},
}


def payment_required(price_credits, resource_url, description, error=None, bazaar=None):
    info = bazaar or BAZAAR_INFO
    req = {**requirements(price_credits, resource_url, description), "outputSchema": {"input": info["input"], "output": info["output"]}}
    body = {"x402Version": 2, "resource": {"url": resource_url, "description": description, "mimeType": "application/json"},
            "accepts": [req], "extensions": {"bazaar": {"info": info, "schema": {"properties": {"input": {"properties": {"bodyFields": info["input"].get("bodyFields", {})}},
                                                                                                 "output": {"properties": {"example": info["output"]["example"]}}}}}}}
    if error:
        body["error"] = error
    return body


def topup_method(base_url, price_credits):
    return {"method": "x402", "protocol": "x402 v2 (HTTP 402, USDC)", "network": NETWORK, "asset": ASSETS[NETWORK][0], "payTo": PAY_TO,
            "price_usd_per_credit": CREDIT_USD, "price_usd_per_claim": round(price_credits * CREDIT_USD, 4),
            "how": "retry the claim with a PAYMENT-SIGNATURE header, or buy credits ahead: POST {b}/seats/<actor>/topup/x402 with PAYMENT-SIGNATURE "
                   "(amount in the PAYMENT-REQUIRED header of the 402; any multiple buys more credits)".format(b=base_url)}


# ---------------- payload handling ----------------

class PaymentError(Exception):
    pass


def parse_payment(header_value):
    try:
        p = unb64(header_value)
    except Exception as e:  # noqa: BLE001
        raise PaymentError(f"PAYMENT-SIGNATURE is not base64 JSON: {e}")
    if not isinstance(p, dict) or p.get("x402Version") != 2 or not isinstance(p.get("accepted"), dict) or not isinstance(p.get("payload"), dict):
        raise PaymentError("payment payload must be x402 v2 with 'accepted' and 'payload'")
    return p


def check_accepted(payload, req):
    a = payload["accepted"]
    for k in ("scheme", "network", "asset", "payTo"):
        if str(a.get(k, "")).lower() != str(req[k]).lower():
            raise PaymentError(f"accepted.{k} does not match this seller's requirements")
    try:
        amount = int(a.get("amount", "0"))
    except (TypeError, ValueError):
        raise PaymentError("accepted.amount must be an integer string")
    if amount < int(req["amount"]):
        raise PaymentError("accepted.amount is below the price")
    return amount


def payment_key(payload):
    """Idempotency key: the payer's signature if present (unique per authorization nonce), else the payload hash."""
    sig = payload.get("payload", {}).get("signature") or payload.get("payload", {}).get("transaction")
    return hashlib.sha256((str(sig) if sig else json.dumps(payload, sort_keys=True)).encode()).hexdigest()


# ---------------- facilitator ----------------

def _facilitator_auth(method, url):
    if not (CDP_KEY_ID and CDP_KEY_SECRET) or not url.startswith(CDP_FACILITATOR):
        return {}
    return {"Authorization": "Bearer " + cdp_jwt(method, url)}


def facilitator(path, body=None, method="POST"):
    url = FACILITATOR.rstrip("/") + path
    # x402.org sits behind Cloudflare, which rejects urllib's default User-Agent with 403
    headers = {"Content-Type": "application/json", "User-Agent": "proving-ground/0.3 (x402 seller)", **_facilitator_auth(method, url)}
    req = urllib.request.Request(url, method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.load(e)
        except Exception:  # noqa: BLE001
            d = {}
        return {"_http": e.code, **(d if isinstance(d, dict) else {"body": d})}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"_error": str(e)}


def verify_and_settle(payload, req):
    """Returns (ok, info). info carries payer/transaction/network or the reason it failed."""
    body = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": req}
    v = facilitator("/verify", body)
    if not v.get("isValid"):
        return False, {"stage": "verify", "reason": v.get("invalidReason") or v.get("invalidMessage") or v.get("_error") or v.get("errorMessage") or f"facilitator HTTP {v.get('_http')}", "payer": v.get("payer")}
    s = facilitator("/settle", body)
    if not s.get("success"):
        return False, {"stage": "settle", "reason": s.get("errorReason") or s.get("_error") or f"facilitator HTTP {s.get('_http')}", "payer": s.get("payer") or v.get("payer")}
    return True, {"payer": s.get("payer") or v.get("payer"), "transaction": s.get("transaction"), "network": s.get("network") or NETWORK}


def settlement_header(info, ok=True):
    return b64({"success": ok, "transaction": info.get("transaction", ""), "network": info.get("network", NETWORK), "payer": info.get("payer")})


# ---------------- CDP JWT (EdDSA) without dependencies ----------------
# Ed25519 per RFC 8032 section 6 reference code. Slow (pure Python) but a token lasts 2 minutes and is cached.
_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p


def _inv(x):
    return pow(x, _p - 2, _p)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = x * pow(2, (_p - 1) // 4, _p) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = 4 * _inv(5) % _p
_B = (_xrecover(_By), _By)


def _add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return x3 % _p, y3 % _p


def _mul(P, e):
    Q = (0, 1)
    while e:
        if e & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        e >>= 1
    return Q


def _enc(P):
    x, y = P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_public(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    return _enc(_mul(_B, a))


def ed25519_sign(seed, msg):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    A = _enc(_mul(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % _q
    R = _enc(_mul(_B, r))
    k = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % _q
    return R + ((r + k * a) % _q).to_bytes(32, "little")


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_jwt_cache, _jwt_lock = {}, threading.Lock()


def cdp_jwt(method, url):
    """EdDSA JWT for the CDP v2 REST API. Secret = base64 of 64 bytes (seed||pubkey) or 32-byte seed."""
    host_path = url.split("://", 1)[1].split("?", 1)[0]
    uri = f"{method} {host_path}"
    with _jwt_lock:
        tok, exp = _jwt_cache.get(uri, (None, 0))
        if tok and time.time() < exp - 15:
            return tok
    raw = base64.b64decode(CDP_KEY_SECRET + "=" * (-len(CDP_KEY_SECRET) % 4))
    if len(raw) not in (32, 64):
        raise RuntimeError("PG_CDP_API_KEY_SECRET must be an Ed25519 key (base64 of 32 or 64 bytes); EC (ES256) keys are not supported here")
    seed = raw[:32]
    now = int(time.time())
    header = {"alg": "EdDSA", "typ": "JWT", "kid": CDP_KEY_ID, "nonce": secrets.token_hex(8)}
    claims = {"sub": CDP_KEY_ID, "iss": "cdp", "aud": ["cdp_service"], "nbf": now, "exp": now + 120, "uri": uri}
    signing = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    tok = signing + "." + _b64url(ed25519_sign(seed, signing.encode()))
    with _jwt_lock:
        _jwt_cache[uri] = (tok, now + 120)
    return tok
