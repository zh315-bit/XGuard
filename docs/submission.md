# AgentGuard — Submission Draft

> Draft for the AI & Fintech XRPL Hackathon (Agentic Finance track). Everything below runs on **XRPL Testnet**.

## One-liner

**x402 lets AI agents pay; AgentGuard makes sure they don't drain your wallet.** A payment guard between an AI agent and its XRPL wallets: every x402 quote is checked by policy before any money moves — small payments go through automatically, large or suspicious ones need a human to approve on a Ledger device, and malicious ones are refused. The agent never holds a private key.

## The problem

- Agents can now pay for APIs on their own (x402 on XRPL), but the 402 quote comes from a **remote, untrusted server**: the price, the payee address and even the data you get back are all controlled by the other side.
- Fully automatic payment means one malicious or compromised merchant can empty the wallet (quote swaps, overpricing, phishing payees, garbage data after payment).
- Requiring a human for every payment defeats the point of an autonomous agent.
- x402 is pay-first, deliver-later: once paid, there is no recourse if the merchant returns garbage.

## The solution

AgentGuard sits between the agent and the money:

- **Policy engine** (`policy.yaml`): network/asset allow-list, payee blacklist, on-chain merchant credentials, per-payment auto limit, daily budget, hard cap, rate limit, quote-swap detection. Verdict: **AUTO / HOLD / DENY**.
- **Two wallets, two trust levels**: a capped hot wallet (≤ 20 XRP, key on the server) for small automatic payments, and a treasury whose only signing key is a **Ledger device** (Regular Key; master key disabled on-chain). In this demo the device is the Speculos emulator; see *Security boundaries* below.
- **Human in the loop on the device**: HOLD payments are reviewed in a web console with an LLM-written risk explanation, then signed on the Ledger (Speculos) — the device screen shows the payee and amount.
- **Verify before release**: large purchases use an XRPL conditional **Escrow** instead of pay-first; AgentGuard only releases the funds after the delivered data passes deterministic checks (for the 7-day DEX dataset: dates inside the week frozen in the approved quote, no duplicates, 7 consecutive days, matching unit, finite non-negative values, total = sum of days) plus an LLM semantic review, otherwise the escrow is refunded.
- **Audit everywhere**: every decision is logged; every transaction AgentGuard signs carries an on-chain memo (`agentguard/decision`, `agentguard/escrow`), escrow memos include the hash of the verification verdict.
- **Agent-facing interface**: an MCP server with four tools (`fetch_paid_resource`, `check_budget`, `approval_status`, `wait_for_approval`) and a Python SDK (`Guard(...).fetch_paid(...)`). The demo agent is built with the Strands Agents SDK.

## Security boundaries

> ⚠ **The demo uses the Speculos emulator instead of a Ledger: the key is on this machine, for demo only.**

- **Hot wallet**: its key is on the server; the balance is capped at 20 XRP. That cap is the most the server can spend directly.
- **Treasury**: every payment needs a device signature. The approver must check the payee and amount on the device screen.
- **Escrow**: funds that are locked are bounded by the locked amount. The guard holds the release preimage.
- **Precondition**: these boundaries require a real hardware device. In this demo Speculos runs on the same computer as the server, so the boundaries are simulated.

## Workflow (6 steps)

1. **Probe** — the agent calls `fetch_paid_resource(url, purpose, max_price, request_id)`. AgentGuard requests the URL with a plain HTTP client (never an auto-paying x402 client) and parses the 402 quote.
2. **Evaluate** — the policy engine checks network/asset, hard cap, blacklist, on-chain credential (XLS-70) of the payee, auto limit, daily budget and rate limit → AUTO / HOLD / DENY. Budget is reserved atomically; `request_id` makes the request idempotent.
3. **Explain** — for HOLD/DENY, a read-only LLM risk explainer summarises the facts and suggests review questions (it cannot change the verdict; falls back to a rule template).
4. **Approve & sign** — HOLD requests appear in the console; the approver signs on the Ledger device. AUTO payments are signed by the capped hot wallet.
5. **Pay safely** — right before signing, AgentGuard re-fetches the quote and refuses if its terms hash changed (anti quote-swap). Every signed transaction carries `LastLedgerSequence`; the hash is stored before submission, and if the response is lost the outcome is resolved by looking the hash up on-chain — never by re-sending.
6. **Deliver or refund** — for x402 `exact` the resource is returned; for escrow purchases the delivered data is verified, then the escrow is finished (released) or cancelled (refunded) after `CancelAfter`. Everything is shown in the console and in the audit log.

## XRPL native features used

| Feature | Where |
|---|---|
| Payment + Memos | Every guard-signed payment carries an audit memo (`agentguard/decision`) |
| x402 on XRPL (`exact`, presigned Payment, InvoiceID, SourceTag) | Paying merchants through the T54 testnet facilitator |
| Credentials (XLS-70: CredentialCreate / CredentialAccept, `ledger_entry`) | Merchant allow-list (rule R3) |
| Escrow (EscrowCreate / EscrowFinish / EscrowCancel, PREIMAGE-SHA-256 condition, CancelAfter) | Verify-before-release purchases |
| SetRegularKey + AccountSet `asfDisableMaster` | Treasury controlled only by the Ledger device |
| LastLedgerSequence + hash lookup (`tx`) | Safe submission / reconciliation without re-sending |
| AccountSet (no-op, device-signed) | Proving the device's Regular Key works without moving funds |

## Representative transactions (testnet)

| Scenario | Transaction |
|---|---|
| AUTO: hot wallet pays merchant A (0.01 XRP) with audit memo | [4517D6D5…14CA](https://testnet.xrpl.org/transactions/4517D6D5BD72A0AB0BBADAAEA45A8845BFC1A05B04649BF9C0E08620B81114CA) |
| AUTO through the **Quicknode** RPC endpoint | [4495148B…1FC3](https://testnet.xrpl.org/transactions/4495148B0522AE5D0C50BF56266393D5D5CC9998E2C51A1368E3C7B6BCCB1FC3) |
| HOLD → approved in the console → treasury pays 8 XRP, **signed on Ledger (Speculos)** | [C5938125…4FC9](https://testnet.xrpl.org/transactions/C5938125CDCC5B8E2EFDD1990E0E330848360DA8FACE2627159FBD5B04864FC9) |
| Quote swap (payee replaced after evaluation) → DENY R8 | no transaction — nothing was paid |
| Escrow lock (device-signed EscrowCreate, 6 XRP) | [2713547C…8A20](https://testnet.xrpl.org/transactions/2713547C0444EC38194E8AF77C6D4180B10E22C71A207D40E6C33E07143F8A20) |
| Verification passed → EscrowFinish (release, memo has verdict hash) | [3B8DEC03…8E64](https://testnet.xrpl.org/transactions/3B8DEC03D397D417344F16094D36F3C84DDEB67838B202A343B10899D79E8E64) |
| Escrow lock for a merchant returning garbage | [0F6E6D2D…7D3E](https://testnet.xrpl.org/transactions/0F6E6D2D63A846F21B3F513DD9F917FDC96B2728911B693540516CB3B2937D3E) |
| Verification failed → EscrowCancel after CancelAfter (refund to treasury) | [C9B1220C…1606](https://testnet.xrpl.org/transactions/C9B1220CF2A2E82DAB4B3B59DF0CD639BBE73F16B7960B0C5F8476E188C31606) |
| Merchant credential issued (CredentialCreate) | [2F4DCBB4…F837](https://testnet.xrpl.org/transactions/2F4DCBB4FE3228EA69242EF829D312DD9496EBF648B872F448083C270AB5F837) |
| Merchant accepted credential (CredentialAccept) | [15CB5C65…FFFC](https://testnet.xrpl.org/transactions/15CB5C658CB21EE53546EC3802DC039A50DF5125F504DC81DD3C2717D45FFFFC) |
| Treasury Regular Key set to the Ledger device (SetRegularKey) | [20321C99…4ACF](https://testnet.xrpl.org/transactions/20321C999731A6B975793A5F7424F7EBC3DEDD0E68240A4C91263FA8CB4F4ACF) |
| Device signs for the treasury, no funds moved (AccountSet) | [CDB85AE7…4DFD](https://testnet.xrpl.org/transactions/CDB85AE797C85F593716B1DAA88CF86DDB9561BE8511FFD1D9E3B33CEDC24DFD) |
| Treasury master key disabled (AccountSet `asfDisableMaster`) | [645ABA15…12DB](https://testnet.xrpl.org/transactions/645ABA15659B8DCAECD246D8197B66B55065A85AE361A22CFA3ABFB473B612DB) |
| Hot wallet capped: excess returned to treasury | [57214F70…2D1B](https://testnet.xrpl.org/transactions/57214F7046FCA4532425B5BD331C922F0F0A28D79B6D23898AE4CA1B4C282D1B) |
| Hot wallet top-up: approved + device-signed, never above the cap | [D6DE8838…EFDD](https://testnet.xrpl.org/transactions/D6DE883884AE7F770D6D0583F7235E662F836B28E00E38C0053A342284A1EFDD) |

Accounts: treasury [rwzHwgxw…kUB](https://testnet.xrpl.org/accounts/rwzHwgxw5UYVgaq2dWctCKGKcCppKnPkUB) (Regular Key = device, master disabled), hot wallet [rfFjxCXW…Y6D](https://testnet.xrpl.org/accounts/rfFjxCXW5FgSLXzwQbVxVhfzin9zmuwY6D) (≤ 20 XRP).

## Known limitations

- **Speculos is an emulator, not a hardware wallet.** Its seed is a random seed stored in plain text on the demo machine (`speculos/.seed`); any emulator seed must be treated as public. The architecture is the same with a real Ledger (WebHID transport instead of the Speculos transport), but the demo itself provides no hardware-level key protection.
- **The escrow flow is a custom extension of x402, not standard x402.** x402-xrpl 0.3.4 and the T54 testnet facilitator only support the `exact` and `upto` schemes. AgentGuard defines an `escrow` scheme in the 402 quote and an "escrow payment proof" (`X-ESCROW-OWNER` / `X-ESCROW-SEQUENCE` / `X-ESCROW-TX` headers); a standard x402 client or merchant cannot use it. The merchant has to trust the buyer's verification; the verdict hash is published on-chain for accountability.
- **The merchant-side on-chain settlement fallback is not part of x402.** In standard x402 the merchant hands the buyer's signed transaction to the facilitator and delivers only after the facilitator confirms settlement. The T54 testnet facilitator was intermittently unstable: `/settle` timed out, the x402 middleware answered 409 `settlement_status_unknown`, and the payment could already be on-ledger without delivery. Our demo merchants therefore add a fallback (`merchants/settle_fallback.py`): only on that 409, the merchant looks up the transaction hash on-ledger and delivers only if it is validated with `tesSUCCESS`, pays the right destination, delivers the full amount, carries the InvoiceID of an unexpired invoice it issued, and has not been redeemed before. This is a fallback around the standard flow, not a protocol change, and other x402 merchants may not have it. The guard does not depend on it: on a failed delivery it reconciles by tx hash and never pays twice.
- **Acceptance terms are part of the quote hash, by design strictly.** `quote_hash` covers the payment terms plus the merchant's description, `dataSchema` and delivery terms (`mimeType`, `maxTimeoutSeconds`, `extra.escrow`), canonicalized before hashing (sorted keys, NFC, whitespace-collapsed strings). The approval stores a full snapshot of the quote the approver saw; delivery verification only uses that snapshot. Any change to the acceptance terms between approval and payment, even a single word in the description, is denied as R8 and requires a new purchase and approval.
- **Testnet only.** Accounts are funded from the faucet; no mainnet configuration or audit has been done.
- LLM steps are advisory: the risk explainer cannot change verdicts, and delivery verification fails closed (no release) when the LLM is unavailable — except for the 7-day DEX dataset, whose acceptance terms are fully covered by the deterministic checks: it is released with the verdict marked "semantic check not completed". Deterministic checks prove shape and period, not that the numbers are real.
- x402-xrpl pins `fastapi<0.116` / `uvicorn<0.31`, which conflicts with MCP 2.x; installation uses a dependency override (tested working).

## Partner prizes

- **Ledger** — The treasury's only signing key is a Ledger device: Regular Key set to the device address (`44'/144'/0'/0/0`) and master key disabled on-chain. Approvals are signed with the **Device Management Kit + `@ledgerhq/device-signer-kit-xrp` + `@ledgerhq/device-transport-kit-speculos`**, both in the browser console and from Node scripts. Payments, EscrowCreate, top-ups and AccountSet were all signed on Speculos (Stax, XRP app 2.8.1); the backend verifies the device signature, payee, amount and quote hash before submitting. We documented 14 integration issues and workarounds in [`docs/ledger-feedback.md`](ledger-feedback.md).
- **Quicknode** — All of AgentGuard's XRPL access (quote validation, autofill, submission, settlement reconciliation, credential and escrow lookups, balances) goes through a Quicknode XRPL Testnet endpoint, switchable with one `.env` variable (`XRPL_RPC_URL`).
- **AWS** — *To be confirmed.* The demo agent is built with the **Strands Agents SDK** and its model provider can be switched to **Amazon Bedrock** via `.env` (`AGENT_MODEL_PROVIDER=bedrock`). Deployment to Bedrock AgentCore has not been done yet.
