"""Reference med3: a print of a Bases chromosome, sold over HTTP 402 and held in escrow until the
print's read-back receipt is on chain (contract `med3.md`).

The reference moves no money and calls no chain. A chain view (an indexer's rows, as JSON) stands in
for Bases, and checkout records the buyer's x402 `auth-capture` authorization instead of relaying it.
Every decision it makes (offer, order, capture, void, refusal) is the one a live operator must make.
"""

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = b"med3"
LOOP_TAG = b"loop1"
LOCUS = re.compile(r"^[0-9a-f]{32}$")
BYTES32 = re.compile(r"^0x[0-9a-f]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
PAYMENT_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
BASES_RUN = re.compile(r"[ACGT]{12,}")  # an order never carries a sequence
WAIT = ("unprinted", "not_finalized", "holder_changed")  # close refusals that leave the hold in place
CODES = (
    "bad_request", "bad_item", "unknown_token", "holder_changed", "bad_payment", "bad_offer", "expired",
    "replay", "bad_base", "no_order", "closed", "bad_receipt", "unprinted", "not_finalized", "stale_print",
    "receipt_used", "bad_loop", "bad_head", "not_reads",
)


class Refusal(Exception):
    def __init__(self, code, detail=""):
        assert code in CODES, code
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def canon(obj):
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def b64(obj):
    return base64.b64encode(canon(obj)).decode()


def unb64(s):
    try:
        return json.loads(base64.b64decode(s, validate=True))
    except (ValueError, TypeError) as e:
        raise Refusal("bad_payment", f"not base64 JSON: {e}") from None


def no_bases(obj):
    """Refuse any string holding a run of 12 or more uppercase ACGT: med3 never moves a sequence."""
    if isinstance(obj, str):
        if BASES_RUN.search(obj):
            raise Refusal("bad_base", "a string holds a run of bases")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            no_bases(k)
            no_bases(v)
    elif isinstance(obj, list):
        for v in obj:
            no_bases(v)


def log_head(roots):
    """h_0 = sha256("med3"); h_(j+1) = sha256(h_j raw || order root j raw)."""
    h = hashlib.sha256(TAG).digest()
    for r in roots:
        h = hashlib.sha256(h + bytes.fromhex(r)).digest()
    return h.hex()


def loop_blocks(text):
    """loop1 text -> (blocks, written head), split as web's ref/loop.py does."""
    if not isinstance(text, str) or not text.endswith("\n") or "\r" in text:
        raise Refusal("bad_loop", "lines must end with LF")
    lines = text[:-1].split("\n")
    if len(lines) < 2 or not lines[0].startswith("%loop1 ") or not lines[-1].startswith("%head "):
        raise Refusal("bad_loop", "not a loop1 file")
    blocks, cur = [lines[0] + "\n"], None
    for ln in lines[1:-1]:
        if ln.startswith("%frame "):
            cur = [ln]
            blocks.append(cur)
        elif cur is None:
            raise Refusal("bad_loop", "text before the first frame")
        else:
            cur.append(ln)
    return blocks[:1] + ["\n".join(b) + "\n" for b in blocks[1:]], lines[-1][6:]


def loop_head(blocks):
    h = hashlib.sha256(LOOP_TAG).digest()
    for b in blocks:
        h = hashlib.sha256(h + b.encode("ascii")).digest()
    return h.hex()


def read_back(text):
    """Check a print's read-back loop without replaying it: the head is the chain over its blocks,
    and its last experiment is `hypothesis expect=intact`, `act op=reads`, `observe`.

    Returns (head, written verdict). Replaying the loop (web `ref/loop.py verify`) is the operator's
    job and needs the sequence and the reads, which med3 never holds.
    """
    blocks, written = loop_blocks(text)
    if written != loop_head(blocks):
        raise Refusal("bad_head", f"head is {loop_head(blocks)}")
    heads = [dict(p.split("=", 1) for p in b.split("\n", 1)[0].split()[3:] if "=" in p) | {"kind": b.split()[2]}
             for b in blocks[1:] if len(b.split()) > 2]
    if len(heads) < 3 or [f["kind"] for f in heads[-3:]] != ["hypothesis", "act", "observe"]:
        raise Refusal("not_reads", "the loop does not end in a hypothesis, act, observe")
    hyp, act, obs = heads[-3:]
    if hyp.get("expect") != "intact" or act.get("op") != "reads":
        raise Refusal("not_reads", "the last experiment is not expect=intact on op=reads")
    if obs.get("verdict") not in ("supports", "refutes"):
        raise Refusal("bad_loop", "the last observe has no verdict")
    return "0x" + written, obs["verdict"]


class Chain:
    """A Bases chain view: holder, print count and Polymerized rows per locus, read from an indexer
    dump `{"tokens": {locus: {"holder", "prints", "rows": [{"receipt", "prints", "holder", "block",
    "finalized"}]}}}`. Reloaded on every call so a test or an indexer can append rows."""

    def __init__(self, path=None, data=None):
        self.path, self.data = path, data

    def token(self, locus):
        data = json.loads(Path(self.path).read_text()) if self.path else (self.data or {})
        t = data.get("tokens", {}).get(locus)
        if t is None:
            raise Refusal("unknown_token", f"dna:{locus} is not minted on this Bases")
        return t


class Med3:
    def __init__(self, config, chain, key, clock=time.time, base="http://127.0.0.1:8402"):
        self.c, self.chain, self.key, self.clock, self.base = config, chain, key, clock, base
        self.items = {}
        for it in config["items"]:
            if it.get("kind") != "print" or not LOCUS.match(it.get("locus", "")) or not ADDRESS.match(it.get("seller", "")):
                raise ValueError(f"bad catalog item {it}")
            no_bases(it)
            self.items[f"print:{it['locus']}"] = it
        self.orders, self.roots, self.payments, self.receipts = {}, [], {}, {}
        self.lock = threading.Lock()

    def token_id(self, locus):
        return f"{self.c['network']}/erc721:{self.c['bases']}/{int(locus, 16)}"

    def profile(self):
        """`/.well-known/ucp`: UCP discovery, with med3 as a vendor service and x402 as its handler."""
        v = self.c["med3"]
        return {
            "ucp": {
                "version": "draft",
                "services": {"xyz.getwetware.med3": [{"version": v, "spec": "https://github.com/wetware-hq/med3/blob/main/med3.md",
                                                       "transport": "rest", "endpoint": self.base}]},
                "capabilities": {"xyz.getwetware.med3.print": [{"version": v, "spec": "https://github.com/wetware-hq/med3/blob/main/med3.md#3-print"}]},
                "payment_handlers": {"org.x402.auth-capture": [{"version": "2", "network": self.c["network"],
                                                                "asset": self.c["asset"]["address"], "paymentFlow": "escrow"}]},
            }
        }

    def catalog(self):
        return {"items": [{"id": k, "kind": "print", "dna": f"dna:{it['locus']}", "token": self.token_id(it["locus"]),
                           "title": it.get("title", ""), "price": {"amount": it["price"], "asset": self.c["asset"]["address"],
                                                                  "network": self.c["network"]}} for k, it in self.items.items()]}

    def item(self, item_id):
        it = self.items.get(item_id)
        if it is None:
            raise Refusal("bad_item", f"{item_id} is not in the catalog")
        return it

    def sign(self, accepted, ext):
        return hmac.new(self.key, canon({"accepted": accepted, "med3": ext}), hashlib.sha256).hexdigest()

    def offer(self, item_id):
        """The 402 body: one `auth-capture` escrow requirement, plus the med3 terms it binds."""
        it, now = self.item(item_id), int(self.clock())
        t = self.chain.token(it["locus"])
        if t["holder"].lower() != it["seller"].lower():
            raise Refusal("holder_changed", f"dna:{it['locus']} is held by {t['holder']}, not the seller")
        e = self.c["escrow"]
        accepted = {
            "scheme": "auth-capture", "network": self.c["network"], "amount": it["price"],
            "asset": self.c["asset"]["address"], "payTo": it["seller"], "maxTimeoutSeconds": e["maxTimeoutSeconds"],
            "extra": {
                "name": self.c["asset"]["name"], "version": self.c["asset"]["version"],
                "authCaptureEscrow": e["authCaptureEscrow"], "captureAuthorizer": e["captureAuthorizer"],
                "operatorType": e["operatorType"], "captureMode": e["captureMode"], "paymentFlow": "escrow",
                "policy": "0x" + "0" * 40, "captureDeadline": now + e["captureWindowSeconds"],
                "refundDeadline": now + e["refundWindowSeconds"], "minFeeBps": e["feeBps"], "maxFeeBps": e["feeBps"],
                "feeRecipient": it["seller"], "assetTransferMethod": e["assetTransferMethod"],
            },
        }
        ext = {"item": item_id, "token": self.token_id(it["locus"]), "prints": t["prints"], "issued": now}
        return {
            "x402Version": 2, "error": "PAYMENT-SIGNATURE header is required",
            "resource": {"url": f"{self.base}/checkout/{item_id}", "description": it.get("title", item_id), "mimeType": "application/json"},
            "accepts": [accepted],
            "extensions": {"med3": ext | {"sig": self.sign(accepted, ext)}, "payment-identifier": {"info": {"required": True}}},
        }

    def checkout(self, item_id, header):
        """PAYMENT-SIGNATURE -> an `authorized` order. Same payment id and body: the same order."""
        it, now = self.item(item_id), int(self.clock())
        p = unb64(header)
        if not isinstance(p, dict) or p.get("x402Version") != 2 or not isinstance(p.get("accepted"), dict):
            raise Refusal("bad_payment", "not an x402 v2 PaymentPayload")
        no_bases(p)
        ext = (p.get("extensions") or {}).get("med3") or {}
        pid = (((p.get("extensions") or {}).get("payment-identifier") or {}).get("info") or {}).get("id", "")
        if not PAYMENT_ID.match(str(pid)):
            raise Refusal("bad_payment", "payment-identifier info.id is required")
        terms = {k: v for k, v in ext.items() if k != "sig"}
        if not hmac.compare_digest(str(ext.get("sig", "")), self.sign(p["accepted"], terms)) or terms.get("item") != item_id:
            raise Refusal("bad_offer", "accepted is not an offer this server made for this item")
        with self.lock:
            if pid in self.payments:
                o = self.orders[self.payments[pid]]
                if o["body"] != sha(canon(p)):
                    raise Refusal("replay", "payment id reused with a different payload")
                return o
            if now > terms["issued"] + p["accepted"]["maxTimeoutSeconds"]:
                raise Refusal("expired", "the offer timed out")
            auth = (p.get("payload") or {}).get("authorization") or {}
            buyer = auth.get("from", "")
            if not ADDRESS.match(buyer) or not (p.get("payload") or {}).get("signature"):
                raise Refusal("bad_payment", "payload.authorization.from and payload.signature are required")
            t = self.chain.token(it["locus"])
            if t["holder"].lower() != it["seller"].lower():
                raise Refusal("holder_changed", f"dna:{it['locus']} changed hands since the offer")
            a = p["accepted"]
            order = {
                "med3": self.c["med3"], "item": item_id, "dna": f"dna:{it['locus']}", "token": terms["token"],
                "buyer": buyer, "seller": a["payTo"], "amount": a["amount"], "asset": a["asset"], "network": a["network"],
                "prints": terms["prints"], "captureDeadline": a["extra"]["captureDeadline"],
                "refundDeadline": a["extra"]["refundDeadline"], "payment": pid, "offer": ext["sig"],
            }
            root = sha(canon(order))
            self.roots.append(root)
            o = {"id": root[:32], "root": root, "order": order, "status": "authorized", "body": sha(canon(p)),
                 "log": len(self.roots) - 1, "head": log_head(self.roots), "receipt": None, "reason": None}
            self.orders[o["id"]] = o
            self.payments[pid] = o["id"]
            return o

    def get(self, order_id):
        o = self.orders.get(order_id)
        if o is None:
            raise Refusal("no_order", order_id)
        return o

    def close(self, order_id, loop_text):
        """Capture when Bases records a finalized print, after the order, by the seller, whose receipt
        is the head of a read-back loop that supports `expect=intact`; void when it refutes or the
        capture deadline passes. Refusals in WAIT leave the hold in place."""
        with self.lock:
            o, now = self.get(order_id), int(self.clock())
            if o["status"] != "authorized":
                raise Refusal("closed", f"order is {o['status']}")
            od = o["order"]
            if now > od["captureDeadline"]:
                return self._final(o, "voided", "past_deadline", None)
            receipt, verdict = read_back(loop_text)
            if not BYTES32.match(receipt):
                raise Refusal("bad_receipt", receipt)
            if receipt in self.receipts and self.receipts[receipt] != order_id:
                raise Refusal("receipt_used", f"receipt closed order {self.receipts[receipt]}")
            rows = [r for r in self.chain.token(od["dna"][4:]).get("rows", []) if r.get("receipt", "").lower() == receipt]
            if not rows:
                raise Refusal("unprinted", "no Polymerized row carries this receipt")
            row = rows[0]
            if row["prints"] <= od["prints"]:
                raise Refusal("stale_print", f"print {row['prints']} is not after the order (prints={od['prints']})")
            if not row.get("finalized"):
                raise Refusal("not_finalized", f"block {row.get('block')} is not final")
            if row.get("holder", "").lower() != od["seller"].lower():
                raise Refusal("holder_changed", "the print was made while someone else held the token")
            return self._final(o, "captured" if verdict == "supports" else "voided", verdict, receipt)

    def _final(self, o, status, reason, receipt):
        o.update(status=status, reason=reason, receipt=receipt)
        if receipt:
            self.receipts[receipt] = o["id"]
        return o

    def log(self):
        return {"head": log_head(self.roots), "roots": list(self.roots)}

    def index(self):
        return (ROOT / "ooc" / "med3.og").read_text()


def public(o):
    return {k: v for k, v in o.items() if k != "body"}


def handler(m):
    class H(BaseHTTPRequestHandler):
        def send(self, code, body, headers=()):
            raw = body.encode() if isinstance(body, str) else canon(body)
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8" if isinstance(body, str) else "application/json")
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def refuse(self, r):
            status = 404 if r.code in ("bad_item", "no_order", "unknown_token") else 409 if r.code in WAIT + ("closed", "receipt_used", "replay", "stale_print") else 400
            self.send(status, {"error": r.code, "detail": r.detail})

        def do_GET(self):
            try:
                path = self.path.split("?", 1)[0]
                if path == "/.well-known/ucp":
                    return self.send(200, m.profile())
                if path == "/catalog":
                    return self.send(200, m.catalog())
                if path == "/log":
                    return self.send(200, m.log())
                if path == "/index":
                    return self.send(200, m.index())
                if path.startswith("/orders/"):
                    return self.send(200, public(m.get(path[8:])))
                self.send(404, {"error": "bad_request", "detail": path})
            except Refusal as r:
                self.refuse(r)

        def do_POST(self):
            try:
                path = self.path.split("?", 1)[0]
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                if path.startswith("/checkout/"):
                    item = path[10:]
                    sig = self.headers.get("PAYMENT-SIGNATURE")
                    if not sig:
                        offer = m.offer(item)
                        return self.send(402, offer, [("PAYMENT-REQUIRED", b64(offer))])
                    o = m.checkout(item, sig)
                    resp = {"success": True, "transaction": "", "network": o["order"]["network"], "payer": o["order"]["buyer"],
                            "extensions": {"med3": {"order": o["id"], "status": o["status"]}}}
                    return self.send(201, public(o), [("PAYMENT-RESPONSE", b64(resp)), ("Location", f"/orders/{o['id']}")])
                if path.startswith("/orders/") and path.endswith("/close"):
                    return self.send(200, public(m.close(path[8:-6], body)))
                self.send(404, {"error": "bad_request", "detail": path})
            except Refusal as r:
                self.refuse(r)

        def log_message(self, *a):
            pass

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(prog="med3", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the reference HTTP server")
    s.add_argument("--config", default=str(ROOT / "med3.json"))
    s.add_argument("--chain", default=str(ROOT / "samples" / "chain.json"), help="Bases chain view (indexer dump)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8402)
    v = sub.add_parser("check", help="check a read-back loop file and print its receipt and verdict")
    v.add_argument("loop")
    sub.add_parser("index", help="print the ooc/med3.og checksum")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        import os
        key = os.environ.get("MED3_OFFER_KEY", "").encode() or hashlib.sha256(os.urandom(32)).digest()
        m = Med3(json.loads(Path(a.config).read_text()), Chain(a.chain), key, base=f"http://{a.host}:{a.port}")
        print(f"med3 on http://{a.host}:{a.port}", flush=True)
        ThreadingHTTPServer((a.host, a.port), handler(m)).serve_forever()
    elif a.cmd == "check":
        try:
            receipt, verdict = read_back(Path(a.loop).read_text())
        except Refusal as r:
            print(r.code, r.detail, file=sys.stderr)
            return 1
        print(json.dumps({"receipt": receipt, "verdict": verdict}))
    else:
        print("sha256:" + og_checksum((ROOT / "ooc" / "med3.og").read_text()))
    return 0


def og_checksum(text):
    """SHA-256 over the index with its checksum line removed, as in helix."""
    return sha(re.sub(r"checksum: sha256:[0-9a-f]*\n", "", text).encode())


if __name__ == "__main__":
    sys.exit(main())
