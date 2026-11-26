# med3

A cybernetic commerce engine for wetware. med3 sells prints of [Bases](https://github.com/wetware-hq/bases) chromosomes to agents over HTTP 402. Payment is an x402 `auth-capture` escrow hold that is captured only when Bases records a print whose receipt is the head of a read-back loop that reproduces the sequence, and voided when the read-back refutes it or the deadline passes.

- [`med3.md`](med3.md) is the contract.
- [`ref/med3.py`](ref/med3.py) is the reference server and checker, Python standard library only.
- [`med3.json`](med3.json) is the catalog and escrow config (Base Sepolia).
- [`docs/distil-sources.md`](docs/distil-sources.md) maps UCP, x402, Bitcoin, Web 3.0, P4 medicine and BIO to the rules they gave.
- [`ooc/med3.og`](ooc/med3.og) is the route and rule index; `GET /index` serves it.

Research and testnet use only; see [`DISCLAIMER`](DISCLAIMER).

## Run

```sh
python3 -m unittest discover -s tests -v
python3 ref/med3.py serve --chain samples/chain.json     # http://127.0.0.1:8402
python3 ref/med3.py check samples/print-1.loop.txt       # receipt and verdict of a read-back loop
python3 ref/med3.py index                                # checksum for ooc/med3.og
```

A full sale against the reference:

```sh
curl -s localhost:8402/.well-known/ucp
curl -si -X POST localhost:8402/checkout/print:00000000000000000000000000000001   # 402 + PAYMENT-REQUIRED
# sign the auth-capture authorization, echo `accepted` and `extensions.med3`, retry with PAYMENT-SIGNATURE -> 201 order
# print, then polymerize(id, receipt) on Bases; append the row to samples/chain.json
curl -s -X POST --data-binary @samples/print-1.loop.txt localhost:8402/orders/<id>/close   # captured
```

## What the reference does not do

- It moves no money: checkout records the buyer's authorization instead of relaying it to a facilitator, and close returns the decision instead of calling `capture` or `void`.
- It reads no chain: `--chain` is an indexer dump of Bases `Polymerized` rows and holders, reloaded on every request.
- It does not replay loops: it checks the head, the frame shape and the written verdict. A live operator replays with web's `ref/loop.py verify`.
- Offer signatures are HMAC-SHA256 with `MED3_OFFER_KEY` (random per run if unset), so only the issuing server can check them.
- State is in memory.
- `bases` and `captureAuthorizer` in `med3.json` are zero placeholders until the Bases deployment and the med3 operator contract are on Base Sepolia.

`samples/print-1.loop.txt` is a structural sample: its head chains correctly but its frames are not replayable data.
