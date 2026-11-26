# Distil sources

Every med3 rule traces to a source. Open papers are in the R2 bucket `literature`; the Hood paper is paywalled, so only its DOI and metadata are stored.

| Source | Object | Took | Rule |
|---|---|---|---|
| UCP ([ucp.dev](https://ucp.dev/), commit `265bb0a`) | spec | `/.well-known/ucp` profile; reverse-domain services and capabilities; payment handlers; catalog items | §2 |
| x402 v2 | spec | `402` + `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, `PAYMENT-RESPONSE`; `payment-identifier` idempotency | §3, §6 |
| x402 `auth-capture` (EVM) | spec | escrow `authorize`, `capture`, `void`, `refund`, `reclaim`; `captureDeadline` < `refundDeadline`; `operatorType: custom` with deferred capture | §1.4, §3, §4 |
| Nakamoto, *Bitcoin* (2008) | `literature/bitcoin.pdf` | no trusted third party; hash-chained log; accept work anyone can check; finality before acting | §1.3, §4, §5 |
| Wan et al., *Web 3.0: The Future of Internet* (arXiv:2304.06032) | `literature/web3-2304.06032.pdf` | user-held keys as identity; verifiable, permissionless state | §1.2 |
| Hood et al., *Systems Biology and New Technologies Enable Predictive and Preventative Medicine*, Science 306:640 (2004), [10.1126/science.1104635](https://doi.org/10.1126/science.1104635) | `literature/hood2004.json` | predictive, preventive, personalised, participatory | §7 |
| [BIO](https://docs.bio.xyz/bio) | docs | Biofy (commerce for science) and BioAgents (agents as buyers) | §3 |
| `wetware-hq/bases` | repo | token id, `polymerize(id, receipt)`, `Polymerized` event, keccak print head | §3, §4 |
| `wetware-hq/web` | repo | loop1 head chain and read-back shape (§12) | §3, §4 |
| `wetware-hq/helix` | repo | `dna:<token id>`, stable refusal codes, checksummed `.og` index | §2, §6 |

## Gaps against BIO

| BIO | med3 | Why |
|---|---|---|
| IP-Tokens, launchpad, governance | left out | med3 sells a print, not rights in IP |
| Biofy commerce | kept as the print item | an agent pays for one verified piece of wet work |
| BioAgents | kept as the buyer | discovery and checkout are machine-readable end to end |
| funding rounds | left out | escrow pays per proven print, not up front |
