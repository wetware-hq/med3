# `med3`

med3 is the commerce layer between [`bases`](https://github.com/wetware-hq/bases), where a chromosome is an ERC-721 token whose prints are recorded on chain, and [`helix`](https://github.com/wetware-hq/helix), where that chromosome is the page `dna:<token id>`. An agent buys a print of a chromosome over plain HTTP. It pays with x402 `auth-capture`, which holds the money in escrow. The money goes to the seller only when Bases records a print whose receipt is the head of a read-back loop that reproduces the sequence. This page is the contract; `ref/med3.py` is the reference and its tests are the predicates.

```toml
[med3]
media_type = "application/json"
version    = "1.0.0"
spec       = "med3.md"
reference  = "ref/med3.py"
config     = "med3.json"
network    = "eip155:84532"              # Base Sepolia; testnet only
discovery  = "/.well-known/ucp"          # UCP profile
payment    = "x402 v2 auth-capture, paymentFlow = escrow, captureMode = deferred"

[pipeline]
input   = "intent --one dna:<token id> { an agent's plan, a helix page }"
process = "discover { /.well-known/ucp; GET /catalog } | offer { 402 auth-capture } | hold { PAYMENT-SIGNATURE -> order } | print { Bases polymerize(id, loop1 head) } | close { capture or void }"
output  = "order --fileformat json { root = sha256(canon(order)); log head = sha256(h || root) }"
```

Research and testnet use only; see [`DISCLAIMER`](DISCLAIMER). med3 sells no clinical, diagnostic or regulated goods.

## §1 Axioms

1. **No bases travel.** An order, offer, payment or receipt never holds a sequence. med3 names a chromosome by token id and roots; any string with a run of 12 or more uppercase `ACGT` is refused `bad_base`.
2. **Keys, not accounts.** The buyer is the payer address in the x402 authorization; the seller is the token holder. Identities are CAIP-2 networks, CAIP-10 accounts and CAIP-19 tokens (`eip155:84532/erc721:<bases>/<id>`).
3. **Proof, not trust.** Money moves on a chain fact (a finalized `Polymerized` row) and a replayable loop, not on the seller's word. Bitcoin's rule carries over: work is accepted when anyone can check it.
4. **Hold until proven.** Every sale is an escrow hold. It ends in exactly one of `captured` or `voided`. If the operator does neither by `captureDeadline`, the buyer reclaims.
5. **Append-only.** Orders are never edited. Each order root is chained into the order log, so rewriting any past order changes the head.
6. **Refuse, never guess.** Every rejection is one stable code from §6. med3 never repairs a payload, a loop or a chain row.

## §2 Discovery

`GET /.well-known/ucp` returns a UCP business profile ([ucp.dev](https://ucp.dev/)). med3 is a vendor service in its own reverse-domain namespace, because a print is not a UCP shopping cart:

| Member | Value |
|---|---|
| `ucp.version` | the UCP version the profile was written against (`draft`) |
| `ucp.services["xyz.getwetware.med3"]` | `{version, spec, transport: "rest", endpoint}` |
| `ucp.capabilities["xyz.getwetware.med3.print"]` | the print capability of §3 |
| `ucp.payment_handlers["org.x402.auth-capture"]` | `{version: "2", network, asset, paymentFlow: "escrow"}` |

`GET /catalog` lists items. An item is `{id: "print:<locus>", kind: "print", dna: "dna:<locus>", token, title, price: {amount, asset, network}}`, where `<locus>` is the 32-hex Bases token id helix uses. `amount` is in the asset's atomic units (USDC has 6 decimals).

## §3 Print

A print item sells one new print of one chromosome by its holder, with the read-back loop that proves it.

1. **Offer.** `POST /checkout/print:<locus>` without `PAYMENT-SIGNATURE` returns `402` and a `PaymentRequired` body, also base64 in `PAYMENT-REQUIRED`. It has one `accepts` entry: `scheme: "auth-capture"`, `payTo` the seller, and `extra` per the x402 EVM binding (`authCaptureEscrow`, `captureAuthorizer`, `operatorType: "custom"`, `captureMode: "deferred"`, `paymentFlow: "escrow"`, `captureDeadline`, `refundDeadline`, fees). `extensions.med3` binds the terms `{item, token, prints, issued, sig}`, where `prints` is the token's print count at the offer and `sig` is the server's signature over `accepted` and the terms. `extensions["payment-identifier"]` is required.
2. **Hold.** The buyer signs the authorization and retries with `PAYMENT-SIGNATURE`: a `PaymentPayload` that echoes `accepted` and `extensions.med3` unchanged and sets `extensions["payment-identifier"].info.id`. The server checks the signature on the terms, the offer's `maxTimeoutSeconds`, the payer and the holder, relays the authorization to its facilitator (`settle` = authorize), and answers `201` with the order, `Location: /orders/<id>` and `PAYMENT-RESPONSE`. The order is `authorized`: funds are held, not paid.
3. **Print.** The seller prints the sequence, sequences the print and writes a `loop1` read-back ([`web` §12](https://github.com/wetware-hq/web/blob/main/docs/follow-agent-contract.md)) whose last experiment is `hypothesis expect=intact`, `act op=reads`, `observe`. The seller calls Bases `polymerize(id, receipt)` with the loop's head as `receipt`.
4. **Close.** `POST /orders/<id>/close` with the loop text as the body. The operator decides by §4 and calls the escrow's `capture` or `void`.

The order is the canonical JSON (sorted keys, no whitespace, UTF-8) of `{med3, item, dna, token, buyer, seller, amount, asset, network, prints, captureDeadline, refundDeadline, payment, offer}`. `prints` is the larger of the offer's count and the chain's count at checkout, so a print made before the order cannot close it. Its root is `sha256` of those bytes and its id is the first 32 hex of the root, the same shape as a Bases token id. `GET /orders/<id>` returns `{id, root, order, status, log, head, receipt, reason}`.

## §4 Close

Checked in this order; the first that applies decides.

| Step | Condition | Result |
|---|---|---|
| 1 | order is not `authorized` | refuse `closed` |
| 2 | now > `captureDeadline` | `voided`, reason `past_deadline` |
| 3 | the loop is not loop1 (LF lines, frames numbered from 0, each with a kind), its head is not the chain over its blocks, or it does not end in an `expect=intact` read-back | refuse `bad_loop`, `bad_head` or `not_reads` |
| 4 | the head already closed another order | refuse `receipt_used` |
| 5 | no `Polymerized` row for the token carries the head as `receipt` | refuse `unprinted` (hold stays) |
| 6 | the row's `prints` is not greater than the order's `prints` | refuse `stale_print` |
| 7 | the row's block is not finalized | refuse `not_finalized` (hold stays) |
| 8 | the token's holder at the row is not the order's seller | refuse `holder_changed` (hold stays) |
| 9 | the replayed verdict is `supports` | `captured`, reason `supports` |
| 10 | the replayed verdict is `refutes` | `voided`, reason `refutes` |

- **Replay.** A live operator MUST replay the loop (web `ref/loop.py verify`) from the sequence and the reads before step 9, since the verdict is computed, never written. The sequence and reads stay with the seller and the operator; med3 never stores them. `ref/med3.py` checks the head, the frame shape and the written verdict only, and says so.
- **Chain view.** Bases chains print heads with keccak256, so rows come from an indexer over `Polymerized(id, by, receipt, prints, head)` events, with `ownerOf(id)` at the row's block as `holder`. "Finalized" is the chain's finalized tag, not a confirmation count.
- **Deadlines.** `captureDeadline` < `refundDeadline`. Between them the operator may `refund` a captured order the buyer disputes; after `captureDeadline` the buyer may `reclaim` an uncaptured hold directly from the escrow, so a dead operator cannot freeze funds.
- **One receipt, one order.** A print closes at most one order, so a seller cannot sell one print twice.

## §5 Order log

The server appends every new order root to a log: `h_0 = sha256("med3")`, `h_(j+1) = sha256(h_j raw || root_j raw)`. `GET /log` returns `{head, roots}` and each order records its `log` index and the `head` after it. Publishing the head (in a helix page, a commit or a chain) timestamps every order before it.

## §6 Refusal codes

Errors are `{error, detail}` with one code. A body over 1 MiB is `413 bad_request`. `close` is permissionless: it acts only on chain facts and the loop, so who calls it does not matter. `404`: `bad_item`, `no_order`, `unknown_token`. `409`: `holder_changed`, `unprinted`, `not_finalized`, `stale_print`, `receipt_used`, `replay`, `closed`. `400`: the rest.

| Code | Meaning |
|---|---|
| `bad_request` | no such route |
| `bad_item` | the item id is not in the catalog |
| `unknown_token` | the chromosome is not minted on this Bases |
| `holder_changed` | the seller no longer holds the token, or someone else printed it |
| `bad_payment` | not a base64 x402 v2 PaymentPayload with payer, signature and payment id |
| `bad_offer` | `accepted` or the med3 terms are not an offer this server signed for this item |
| `expired` | the offer's `maxTimeoutSeconds` passed |
| `replay` | a payment id reused with a different payload |
| `bad_base` | a string holds a run of bases |
| `no_order` | no order with that id |
| `closed` | the order is already captured or voided |
| `bad_receipt` | the receipt is not a 32-byte hex |
| `unprinted` | no print carries the receipt yet |
| `not_finalized` | the print's block is not final yet |
| `stale_print` | the print is not newer than the order |
| `receipt_used` | the receipt already closed another order |
| `bad_loop` | not a loop1 file, or no verdict |
| `bad_head` | the loop's head is not the chain over its blocks |
| `not_reads` | the loop does not end in `expect=intact` on `op=reads` |

The same payment id with the same payload returns the same order: retries are safe.

## §7 Stack

```text
tesseract   .tess plan            what to build
web         .dna, loop1           the sequence, and the read-back that proves a print
bases       token, prints, head   who holds it, and each print's receipt on chain
med3        offer, hold, close    who pays for a print, and when the money moves
helix       dna:<token id>        what is known about it, pinned to the chain
```

med3 writes nothing to helix or Bases. Helix pins a print through the chain row med3 closed on, so a helix `printed` claim and a med3 capture name the same receipt. The P4 loop of Hood (2004) maps onto the stack: predict (a helix hypothesis), personalise (a plan for one holder's chromosome), prevent a bad print from being paid for (a refuting read-back voids), participate (anyone can replay). [`docs/distil-sources.md`](docs/distil-sources.md) maps every source to the rule it gave.

## §8 Predicates

| Id | Predicate |
|---|---|
| M1 | every offer is `auth-capture` with `paymentFlow = escrow` and `captureDeadline < refundDeadline` |
| M2 | an order's root is `sha256(canon(order))` and its id is the root's first 32 hex |
| M3 | the log head equals the §5 chain over every order root, in order |
| M4 | the same payment id and payload return the same order; a different payload is `replay` |
| M5 | a tampered `accepted` or terms is `bad_offer`; a timed-out offer is `expired` |
| M6 | no order, offer or payment holds a run of bases |
| M7 | an order is captured only by a finalized, newer print by its seller whose receipt is the head of a supporting `expect=intact` read-back |
| M8 | a refuting read-back or a passed `captureDeadline` voids; a wait leaves the order `authorized` |
| M9 | one receipt closes at most one order |
| M10 | the loop head is computed exactly as web §12, so a web loop's `%head` checks under med3 |
