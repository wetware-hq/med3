import base64
import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ref"))
import med3  # noqa: E402

LOCUS = "00000000000000000000000000000001"
ITEM = f"print:{LOCUS}"
SELLER = "0x1111111111111111111111111111111111111111"
BUYER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
SAMPLE = (ROOT / "samples" / "print-1.loop.txt").read_text()


def refuting(text):
    """The sample loop with its last verdict flipped and its head rechained."""
    blocks, _ = med3.loop_blocks(text.replace("verdict=supports", "verdict=refutes").replace("outcome=intact sequence", "outcome=changed sequence"))
    return "".join(blocks) + "%head " + med3.loop_head(blocks) + "\n"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "med3.json").read_text())
        self.state = {"block": 100, "tokens": {LOCUS: {"holder": SELLER, "prints": 0, "rows": []}}}
        self.clock = Clock()
        self.m = med3.Med3(self.cfg, med3.Chain(data=self.state), b"k" * 32, clock=self.clock)

    def pay(self, offer=None, pid="pay-0000000000000001", **over):
        offer = offer or self.m.offer(ITEM)
        p = {
            "x402Version": 2, "resource": offer["resource"], "accepted": copy.deepcopy(offer["accepts"][0]),
            "payload": {"authorization": {"from": BUYER, "to": "0x" + "0" * 40, "value": offer["accepts"][0]["amount"]}, "signature": "0x" + "ab" * 65},
            "extensions": {"med3": dict(offer["extensions"]["med3"]), "payment-identifier": {"info": {"required": True, "id": pid}}},
        }
        for k, v in over.items():
            p[k] = v
        return base64.b64encode(json.dumps(p).encode()).decode()

    def print_row(self, receipt, prints=1, finalized=True, holder=SELLER):
        t = self.state["tokens"][LOCUS]
        t["prints"] = prints
        self.state["block"] += 1
        t["rows"].append({"receipt": receipt, "prints": prints, "holder": holder, "block": self.state["block"], "finalized": finalized})

    def refused(self, code, f, *a):
        with self.assertRaises(med3.Refusal) as cm:
            f(*a)
        self.assertEqual(cm.exception.code, code)


class TestLoop(Base):
    def test_sample_receipt(self):
        receipt, verdict = med3.read_back(SAMPLE)
        self.assertRegex(receipt, med3.BYTES32)
        self.assertEqual(verdict, "supports")

    @unittest.skipUnless((ROOT.parent / "web" / "samples" / "astronaut-64.loop.txt").exists(), "needs wetware-hq/web beside med3")
    def test_head_matches_web(self):
        blocks, head = med3.loop_blocks((ROOT.parent / "web" / "samples" / "astronaut-64.loop.txt").read_text())
        self.assertEqual(med3.loop_head(blocks), head)

    def test_tampered_frame(self):
        self.refused("bad_head", med3.read_back, SAMPLE.replace("used=12", "used=13"))

    def test_not_reads(self):
        blocks, _ = med3.loop_blocks(SAMPLE.replace("op=reads file=0 sha256", "op=lose file=0 oligos=0 sha256"))
        self.refused("not_reads", med3.read_back, "".join(blocks) + "%head " + med3.loop_head(blocks) + "\n")

    def test_incomplete_frame(self):
        for extra in ("%frame 4\n", "%frame 9 think view=cgr k=1\n"):
            blocks, _ = med3.loop_blocks(SAMPLE)
            blocks.append(extra)
            self.refused("bad_loop", med3.read_back, "".join(blocks) + "%head " + med3.loop_head(blocks) + "\n")

    def test_not_a_loop(self):
        self.refused("bad_loop", med3.read_back, "hello\n")


class TestCheckout(Base):
    def test_profile_and_catalog(self):
        u = self.m.profile()["ucp"]
        self.assertIn("xyz.getwetware.med3", u["services"])
        self.assertEqual(u["payment_handlers"]["org.x402.auth-capture"][0]["paymentFlow"], "escrow")
        (it,) = self.m.catalog()["items"]
        self.assertEqual(it["dna"], f"dna:{LOCUS}")
        self.assertEqual(it["token"], f"eip155:84532/erc721:{self.cfg['bases']}/1")

    def test_offer_is_escrow(self):
        a = self.m.offer(ITEM)["accepts"][0]
        self.assertEqual((a["scheme"], a["extra"]["paymentFlow"], a["extra"]["captureMode"]), ("auth-capture", "escrow", "deferred"))
        self.assertLess(a["extra"]["captureDeadline"], a["extra"]["refundDeadline"])

    def test_order(self):
        o = self.m.checkout(ITEM, self.pay())
        self.assertEqual(o["status"], "authorized")
        self.assertEqual(o["id"], o["root"][:32])
        self.assertEqual(o["root"], med3.sha(med3.canon(o["order"])))
        self.assertEqual(self.m.log()["head"], med3.log_head([o["root"]]))
        med3.no_bases(o)

    def test_idempotent_and_replay(self):
        offer = self.m.offer(ITEM)
        h = self.pay(offer)
        self.assertIs(self.m.checkout(ITEM, h), self.m.checkout(ITEM, h))
        self.assertEqual(len(self.m.log()["roots"]), 1)
        self.refused("replay", self.m.checkout, ITEM, self.pay(offer, payload={"authorization": {"from": OTHER}, "signature": "0x01"}))

    def test_log_chains(self):
        a = self.m.checkout(ITEM, self.pay(pid="pay-000000000000000a"))
        b = self.m.checkout(ITEM, self.pay(pid="pay-000000000000000b"))
        self.assertEqual(self.m.log()["head"], med3.log_head([a["root"], b["root"]]))
        self.assertNotEqual(a["head"], b["head"])

    def test_tampered_offer(self):
        offer = self.m.offer(ITEM)
        offer["accepts"][0]["amount"] = "1"
        self.refused("bad_offer", self.m.checkout, ITEM, self.pay(offer))

    def test_offer_for_other_item(self):
        self.refused("bad_item", self.m.checkout, "print:" + "f" * 32, self.pay())

    def test_expired(self):
        h = self.pay()
        self.clock.t += 61
        self.refused("expired", self.m.checkout, ITEM, h)

    def test_bad_payment(self):
        self.refused("bad_payment", self.m.checkout, ITEM, "!!!")
        self.refused("bad_payment", self.m.checkout, ITEM, self.pay(pid="short"))
        self.refused("bad_payment", self.m.checkout, ITEM, self.pay(payload={"authorization": {"from": "me"}, "signature": "0x01"}))

    def test_malformed_metadata(self):
        self.refused("bad_payment", self.m.checkout, ITEM, self.pay(extensions=["x"]))
        self.refused("bad_payment", self.m.checkout, ITEM, self.pay(payload=[1]))
        h = base64.b64encode(json.dumps({"x402Version": 2, "accepted": {}, "memo": "\ud800"}).encode()).decode()
        self.refused("bad_payment", self.m.checkout, ITEM, h)
        self.assertEqual(self.m.log()["roots"], [])

    def test_no_bases(self):
        self.refused("bad_base", self.m.checkout, ITEM, self.pay(memo="ACGTACGTACGTACGT"))

    def test_holder_changed(self):
        h = self.pay()
        self.state["tokens"][LOCUS]["holder"] = OTHER
        self.refused("holder_changed", self.m.checkout, ITEM, h)
        self.refused("holder_changed", self.m.offer, ITEM)

    def test_unknown_token(self):
        del self.state["tokens"][LOCUS]
        self.refused("unknown_token", self.m.offer, ITEM)


class TestClose(Base):
    def setUp(self):
        super().setUp()
        self.o = self.m.checkout(ITEM, self.pay())
        self.receipt = med3.read_back(SAMPLE)[0]

    def test_capture(self):
        self.print_row(self.receipt)
        o = self.m.close(self.o["id"], SAMPLE)
        self.assertEqual((o["status"], o["reason"], o["receipt"]), ("captured", "supports", self.receipt))
        self.refused("closed", self.m.close, self.o["id"], SAMPLE)

    def test_refuted_voids(self):
        text = refuting(SAMPLE)
        self.print_row(med3.read_back(text)[0])
        self.assertEqual(self.m.close(self.o["id"], text)["status"], "voided")

    def test_waits_keep_the_hold(self):
        self.refused("unprinted", self.m.close, self.o["id"], SAMPLE)
        self.print_row(self.receipt, finalized=False)
        self.refused("not_finalized", self.m.close, self.o["id"], SAMPLE)
        self.assertEqual(self.m.get(self.o["id"])["status"], "authorized")

    def test_print_by_new_holder(self):
        self.print_row(self.receipt, holder=OTHER)
        self.refused("holder_changed", self.m.close, self.o["id"], SAMPLE)

    def test_stale_print(self):
        self.state["tokens"][LOCUS]["rows"].append({"receipt": self.receipt, "prints": 0, "holder": SELLER, "block": 1, "finalized": True})
        self.refused("stale_print", self.m.close, self.o["id"], SAMPLE)

    def test_print_between_offer_and_checkout(self):
        offer = self.m.offer(ITEM)
        self.print_row(self.receipt)
        o = self.m.checkout(ITEM, self.pay(offer, pid="pay-0000000000000003"))
        self.refused("stale_print", self.m.close, o["id"], SAMPLE)

    def test_reorged_print_before_checkout(self):
        offer = self.m.offer(ITEM)
        self.print_row("0x" + "ee" * 32, finalized=False)
        o = self.m.checkout(ITEM, self.pay(offer, pid="pay-0000000000000004"))
        self.state["tokens"][LOCUS]["rows"] = []
        self.print_row(self.receipt)
        self.assertEqual(self.m.close(o["id"], SAMPLE)["status"], "captured")

    def test_reorg_reveals_print_before_checkout(self):
        offer = self.m.offer(ITEM)
        self.state["block"] += 1
        o = self.m.checkout(ITEM, self.pay(offer, pid="pay-0000000000000005"))
        self.state["tokens"][LOCUS]["rows"].append(
            {"receipt": self.receipt, "prints": 1, "holder": SELLER, "block": 101, "finalized": True})
        self.refused("stale_print", self.m.close, o["id"], SAMPLE)

    def test_receipt_closes_one_order(self):
        self.print_row(self.receipt)
        self.m.close(self.o["id"], SAMPLE)
        o2 = self.m.checkout(ITEM, self.pay(pid="pay-0000000000000002"))
        self.refused("receipt_used", self.m.close, o2["id"], SAMPLE)

    def test_past_deadline_voids(self):
        self.print_row(self.receipt)
        self.clock.t = self.o["order"]["captureDeadline"] + 1
        o = self.m.close(self.o["id"], SAMPLE)
        self.assertEqual((o["status"], o["reason"]), ("voided", "past_deadline"))

    def test_no_order(self):
        self.refused("no_order", self.m.close, "0" * 32, SAMPLE)


class TestHTTP(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), med3.handler(self.m))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, data=body.encode() if body is not None else (b"" if method == "POST" else None), method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_flow(self):
        s, _, b = self.call("GET", "/.well-known/ucp")
        self.assertEqual(s, 200)
        self.assertIn("ucp", json.loads(b))
        s, h, b = self.call("POST", f"/checkout/{ITEM}")
        self.assertEqual(s, 402)
        offer = json.loads(base64.b64decode(h["PAYMENT-REQUIRED"]))
        self.assertEqual(offer, json.loads(b))
        s, h, b = self.call("POST", f"/checkout/{ITEM}", headers={"PAYMENT-SIGNATURE": self.pay(offer)})
        self.assertEqual(s, 201)
        oid = json.loads(base64.b64decode(h["PAYMENT-RESPONSE"]))["extensions"]["med3"]["order"]
        self.assertEqual(h["Location"], f"/orders/{oid}")
        s, _, b = self.call("POST", f"/orders/{oid}/close", SAMPLE)
        self.assertEqual((s, json.loads(b)["error"]), (409, "unprinted"))
        self.print_row(med3.read_back(SAMPLE)[0])
        s, _, b = self.call("POST", f"/orders/{oid}/close", SAMPLE)
        self.assertEqual((s, json.loads(b)["status"]), (200, "captured"))
        s, _, b = self.call("GET", f"/orders/{oid}")
        self.assertNotIn("body", json.loads(b))
        s, _, b = self.call("GET", "/log")
        self.assertEqual(len(json.loads(b)["roots"]), 1)

    def test_errors(self):
        self.assertEqual(self.call("POST", "/checkout/print:nope")[0], 404)
        s, _, b = self.call("POST", f"/checkout/{ITEM}", headers={"PAYMENT-SIGNATURE": "x"})
        self.assertEqual((s, json.loads(b)["error"]), (400, "bad_payment"))
        self.assertEqual(self.call("GET", "/orders/" + "0" * 32)[0], 404)
        self.assertEqual(self.call("POST", f"/orders/{'0' * 32}/close", "x" * (med3.MAX_BODY + 1))[0], 413)


class TestIndex(unittest.TestCase):
    def test_checksum(self):
        text = (ROOT / "ooc" / "med3.og").read_text()
        self.assertIn("checksum: sha256:" + med3.og_checksum(text) + "\n", text)

    def test_codes_documented(self):
        spec = (ROOT / "med3.md").read_text()
        for code in med3.CODES:
            self.assertIn(f"`{code}`", spec, code)


if __name__ == "__main__":
    unittest.main()
