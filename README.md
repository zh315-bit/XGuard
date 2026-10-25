# AgentGuard

A payment guard between AI agents and their wallets (XRPL Testnet). When an agent calls a paid API over x402, the guard
first fetches the quote and decides, according to policy, **AUTO** (paid automatically from the hot wallet) /
**HOLD** (human approval + Ledger signature) / **DENY** (refused). Every decision is recorded in an audit log, and every
transaction the guard signs carries an on-chain Memo. The agent never gets access to private keys.

## Security boundaries

> ⚠ **This demo uses the Speculos emulator instead of a real Ledger: the private key lives on this machine and is for demo purposes only.**

- **Hot wallet**: its private key is on the server, and its balance is capped at 20 XRP (`hot_wallet_max_balance_xrp`). This is the most the server can spend directly.
- **Treasury**: every payment needs a device signature, and the approver must check the payee address and amount on the device screen.
- **Escrow**: locked funds are limited to the locked amount; the preimage used to release them is held by the guard.
- **Precondition**: these boundaries require a real hardware device. In this demo Speculos runs on the same computer as the server and the emulator seed is stored in plain text in `speculos/.seed`, so the boundaries are only simulated.

## Quick start

Requirements: Python 3.12 (`.venv`), Node 22+, Docker (on macOS use Colima: `brew install colima docker`).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[agent,dev]" --override overrides.txt
(cd frontend && npm install)
.venv/bin/python scripts/setup_wallets.py      # create 6 accounts from the faucet and generate .env
.venv/bin/python scripts/issue_creds.py        # issue on-chain credentials to merchant_a / merchant_b
scripts/start_all.sh                           # Speculos + merchants + guard API + console
.venv/bin/python scripts/lock_treasury.py      # hand the treasury Regular Key to the Ledger (Speculos); optionally disable the master key
```

Console: http://127.0.0.1:5173. Run the demo:

```bash
curl -X POST http://127.0.0.1:8100/api/demo/reset    # available when DEMO_MODE=true
.venv/bin/python agent/agent.py
```

Live demo (narrated step by step in the terminal; B and E pause until you sign with the Ledger in the console):

```bash
.venv/bin/python scripts/demo_run.py            # merchant E defaults to garbage: verification fails, auto refund after 75 s
.venv/bin/python scripts/demo_run.py --e-mode normal
```

Stop: `scripts/stop_all.sh`. Tests: `.venv/bin/python -m pytest tests/`.

Signing must be done by a human on the device: neither `start_all.sh` nor `demo_run.py` confirms anything on the device.
`scripts/speculos_autoconfirm.py` is for tests only and refuses to run unless `SPECULOS_AUTOCONFIRM=1` is set explicitly.

## Three-line integration: let your agent pay through AgentGuard

```python
from agentguard import Guard

guard = Guard(policy_path="policy.yaml", agent_id="my-research-agent")
result = guard.fetch_paid("https://api.example.com/report", purpose="weekly report data", request_id="report-42")
```

`result["status"]` is one of `paid` (`result["content"]` is the data), `pending_approval` (`result["approval_id"]`;
check the outcome with `guard.approval(id)`), `denied`, `delivery_rejected`, `fetch_failed`, etc. Repeating a call with
the same `request_id` only returns the existing result and never pays twice.
Wallet, RPC and other settings are read from `.env` (set the directory with `Guard(env_file=...)` or the `AGENTGUARD_HOME` environment variable).

Integration over MCP (Claude Desktop, Strands, any MCP client):

```json
{"mcpServers": {"agentguard": {"command": "agentguard-mcp", "args": ["--agent-id", "my-research-agent"]}}}
```

Install: `uv pip install -e . --override overrides.txt`. The override is required: x402-xrpl 0.3.4 pins `fastapi<0.116`
and `uvicorn<0.31`, which conflict with mcp 2.x, so dependency resolution fails without it (we tested the x402 merchant
middleware on the newer versions and it works fine). Plain `pip` cannot override upper bounds of transitive dependencies,
which is why uv is used here.

## Components

| Part | Location |
|---|---|
| Policy (R1–R8, budgets, TTL, hot wallet cap, escrow threshold) | `policy.yaml`, `guard/policy.py` |
| Payment state machine, idempotency, budget reservations, reconciliation after submit timeouts | `guard/state_machine.py`, `payments.py`, `budget.py`, `settlement.py` |
| x402 interception and payment | `guard/x402_gate.py`, `guard/payment_executor.py` |
| Approvals, Ledger (Speculos) signing | `guard/approval_service.py`, `guard/signers.py`, `guard/signing_requests.py` |
| Verify before release | `guard/escrow.py`, `guard/delivery_verifier.py`, `merchants/escrow_merchant.py` |
| Hot wallet cap / top-ups | `guard/hot_wallet.py`, `scripts/cap_hot_wallet.py` |
| MCP server / agent | `guard/mcp_server.py`, `agent/agent.py` |
| Console | `frontend/` |

## Probing a resource

Before paying, the guard requests the resource once (no redirects followed):

- **2xx**: the resource is free; its content is returned to the agent.
- **402**: the quote is parsed and goes through policy evaluation.
- **3xx, 4xx, 5xx, connection failure, timeout**: a clear error with the status code and a short reason (`fetch_failed`).
  Nothing is paid, the response body is not passed to the agent, and the audit log records the request as failed.

## Hot wallet balance cap

The hot wallet's private key is on the server (it is needed for small AUTO payments), so its balance is the most the
server can spend directly.

- `hot_wallet_max_balance_xrp` (default 20 XRP): `scripts/cap_hot_wallet.py` sends anything above the cap back to the treasury, with the audit Memo `agentguard/hot-wallet`.
- Top-ups can only come from the treasury: started from the Treasury page in the console → an approval request is created → human approval + Ledger signature. The latest on-chain balance is checked both when the top-up is requested and before signing; the balance after the top-up may not exceed the cap.
- The treasury's Regular Key is the Ledger device and its master key is disabled, so every treasury payment needs a device signature (in this demo the device is Speculos running on the same machine; see "Security boundaries").

## Verify before release: an escrow extension to x402 (not standard x402)

x402 is "pay first, then receive": after getting paid, a merchant may return garbage. For large purchases AgentGuard
uses XRPL's native conditional Escrow instead: the funds are locked first, released to the merchant only if the delivery
passes verification, and refunded to the treasury otherwise.

**This is an extension defined by AgentGuard, not part of the x402 standard.** Neither x402-xrpl 0.3.4 nor the testnet
facilitator (whose `/supported` only lists `exact` and `upto`) supports escrow, so standard x402 clients and merchants
cannot use this flow.

### When escrow is used

Purchases that are approved, exceed `escrow_threshold_xrp` (default 5 XRP), and whose merchant offers an `escrow`
option in the 402 quote. Merchants that only support `exact` (e.g. `/data/b`) are unaffected and are still paid directly after approval.

### Quote format

The 402 response keeps the x402 v2 `PaymentRequired` structure, but the `scheme` in `accepts` is the custom `"escrow"`
(the demo merchant's actual description is in Chinese; it is translated here):

```json
{
  "scheme": "escrow", "network": "xrpl:1", "asset": "XRP",
  "amount": "6000000", "payTo": "r...", "maxTimeoutSeconds": 600,
  "extra": {
    "invoiceId": "…",
    "description": "XRPL DEX weekly volume: the 7 days of the ISO week (Mon–Sun) declared in dataSchema.coverage, one volume value per day (in XRP), plus the 7-day total",
    "dataSchema": {"profile": "xrpl_dex_7day",
                   "required": ["dataset", "week", "unit", "daily", "total_volume_xrp"],
                   "series": {"field": "daily", "min_items": 7, "date_field": "date", "value_field": "volume_xrp"},
                   "dataset": "xrpl_weekly_dex_volume", "unit": "XRP", "total_field": "total_volume_xrp",
                   "coverage": {"week": "2026-W42", "start": "2026-10-12", "end": "2026-10-18", "days": 7}},
    "escrow": {"condition": "PREIMAGE-SHA-256", "minSecondsBeforeCancel": 60,
               "proofHeaders": ["X-ESCROW-OWNER", "X-ESCROW-SEQUENCE", "X-ESCROW-TX"]}
  }
}
```

`description` and `dataSchema` are the merchant's commitments about the delivery, and verification checks against them.

**Acceptance terms are bound into the quote hash.** `quote_hash` covers the payment terms (scheme, network, asset,
amount, payTo) and the acceptance terms: the merchant's description (`extra.description` and `resource.description`),
`dataSchema`, and the delivery terms (`mimeType`, `maxTimeoutSeconds`, `extra.escrow`). Before hashing, the data is
normalized (sorted keys, NFC strings with collapsed whitespace), which removes only formatting differences. The approval
stores a full snapshot of the quote the approver saw. Before paying or locking funds the guard fetches the quote again,
and if the recomputed hash differs from the approved one, the payment is denied under R8. Verification uses only the
description and schema from the approval snapshot, never a version the merchant sends at payment time.

**Any change to the acceptance terms causes a denial. This strictness is intentional**: even if the merchant only
changes one sentence of the description or relaxes one field, what the approver approved is no longer the same
commitment. The guard will not pay; the purchase has to be started and approved again.

### Flow

1. **Lock**: the treasury submits `EscrowCreate` (destination = merchant, amount = quote, `Condition` = PREIMAGE-SHA-256
   condition, `CancelAfter` = ledger time + `escrow_cancel_after_seconds`), signed by the Ledger. The preimage is stored
   only in the guard's database and never goes through any API.
2. **Proof of payment**: the guard requests the resource again with these headers (instead of x402's `PAYMENT-SIGNATURE`):
   - `X-ESCROW-OWNER`: the locking account
   - `X-ESCROW-SEQUENCE`: the Sequence of the EscrowCreate
   - `X-ESCROW-TX`: the transaction hash of the EscrowCreate
3. **Merchant checks, then delivers**: the merchant looks up the escrow on chain: it is the destination, the amount equals the quote, there is a condition, there is enough time left before `CancelAfter`, and the escrow has not been used before. Only then does it return the data.
4. **Verification** (`guard/delivery_verifier.py`):
   - Generic deterministic checks: JSON object, non-empty, required fields present, at least 7 `daily` entries, valid date and non-negative value in each
   - Checks specific to 7-day DEX data (`dataSchema.profile = "xrpl_dex_7day"`, used only for data like merchant E's):
     `dataset`, `week` and unit match the quote; every date falls within `coverage`, with no duplicates and 7 consecutive days;
     every value is a finite non-negative number; `total_volume_xrp` equals the sum of the daily values.
     The acceptance period comes only from the `coverage` frozen in the approval snapshot, never from the current date.
     Writing a different week into the delivery does not help the merchant, and changing the period in the quote changes
     `quote_hash` (R8). The period is "the last complete ISO week", so the quote is the same on every day of a week and
     runs that cross midnight are not misjudged (e.g. approved on Oct 24 and delivered on Oct 25 is still 2026-W42).
     Garbage mode (sample data from 2019) is stopped at this step, without relying on the LLM
   - LLM semantic check (does the topic match, is it placeholder data): an extra layer that can only reject more. The LLM is not called when the deterministic checks fail

   **When the LLM is unavailable** (error, timeout, refusal):
   - 7-day DEX data: if all deterministic checks pass, the funds are released and the verdict notes "semantic check not completed" (`semantic_check: "incomplete"`).
     All acceptance terms for this data (period, dates, unit, values, total) are checked deterministically; the LLM is a bonus
   - Other schemas without specific checks: the deterministic checks cannot cover the date range or content, so the verdict is still a conservative fail (no release, refund)
5. **Release**: if verification passes, the guard submits `EscrowFinish` (with the preimage) and the merchant gets paid.
6. **Refund**: if verification fails (or the merchant does not deliver), the guard submits `EscrowCancel` once ledger time
   passes `CancelAfter`, and the funds go back to the treasury. The ledger does not allow cancelling before `CancelAfter`,
   and the guard also blocks it early.

Any account can submit `EscrowFinish` / `EscrowCancel`; the hot wallet submits them (paying only the fee), so the
treasury keys are not needed. All three transactions carry audit Memos; the Finish / Cancel Memo contains the sha256 of
the verification verdict (`verification_hash`), so anyone can recompute it from the verdict shown in the console and compare.

### Known limitations

- The merchant only gets paid after `EscrowFinish`, so it has to trust the buyer to verify honestly; in return, the buyer's verdict and its hash are published on chain.
- Deterministic checks only prove that the data has the right shape and period, not that the values are real on-chain volume.
  The LLM semantic check covers part of that; when the LLM is unavailable, 7-day DEX data is released based on the deterministic checks alone (and the verdict says so).
- The demo merchant E switches its delivery with `POST /data/e/mode {"mode": "normal" | "garbage" | "malformed"}`.

## Known limitations

- **The merchant's on-chain fallback is not part of the x402 protocol.** In the standard x402 flow, the merchant hands
  the buyer's signed transaction to the facilitator for settlement and delivers only after the facilitator confirms.
  When the testnet facilitator is unstable, `/settle` times out and the x402 middleware returns 409
  `settlement_status_unknown` (the money may already be on chain, but nothing is delivered). The demo merchant adds a
  fallback for this (`merchants/settle_fallback.py`): only on that 409, it looks up the transaction by hash and delivers
  only if the transaction is validated and successful, the destination, delivered amount and InvoiceID match an invoice
  it issued, the invoice has not expired, and the transaction has not been used before. This is the demo merchant's
  fallback for the standard flow; it does not change the protocol and other x402 merchants may not have it. The guard
  does not depend on it (if delivery fails, the guard reconciles by transaction hash and never pays twice).
