# AgentGuard

AI agent 和钱包之间的支付守卫（XRPL Testnet）。agent 通过 x402 调用付费 API，守卫先拿到报价，按策略判定
**AUTO**（热钱包自动付）/ **HOLD**（人工审批 + Ledger 签名）/ **DENY**（拒绝），每笔判定都有审计记录，
守卫签的交易都带链上 Memo。agent 永远拿不到私钥。

## 安全边界

> ⚠ **本演示用 Speculos 模拟器代替 Ledger：私钥在本机，仅用于演示。**

- **热钱包**：私钥在服务器上，余额封顶 20 XRP（`hot_wallet_max_balance_xrp`），这是服务器能直接动用的上限。
- **金库**：每笔付款都需要设备签名，审批人必须核对设备屏幕上的收款地址和金额。
- **Escrow**：锁定中的资金以锁定金额为限，放款用的原像由守卫保存。
- **前提**：以上边界需要真实的硬件设备。本演示中 Speculos 与服务器在同一台电脑上，模拟器的 seed 以明文保存在 `speculos/.seed`，所以这些边界只是模拟出来的。

## 一键启动

需要：Python 3.12（`.venv`）、Node 22+、Docker（macOS 上用 Colima：`brew install colima docker`）。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[agent,dev]" --override overrides.txt
(cd frontend && npm install)
.venv/bin/python scripts/setup_wallets.py      # 水龙头建 6 个账户，生成 .env
.venv/bin/python scripts/issue_creds.py        # 给 merchant_a / merchant_b 签发链上凭证
scripts/start_all.sh                           # Speculos + 商户 + 守卫 API + 控制台
.venv/bin/python scripts/lock_treasury.py      # 金库 Regular Key 交给 Ledger（Speculos），可选禁用主密钥
```

控制台：http://127.0.0.1:5173 。跑 demo：

```bash
curl -X POST http://127.0.0.1:8100/api/demo/reset    # DEMO_MODE=true 时可用
.venv/bin/python agent/agent.py
```

现场演示（终端分段讲解，B 和 E 会暂停等你在控制台用 Ledger 签名）：

```bash
.venv/bin/python scripts/demo_run.py            # 商户 E 默认 garbage：验货失败、75 秒后自动退款
.venv/bin/python scripts/demo_run.py --e-mode normal
```

停止：`scripts/stop_all.sh`。测试：`.venv/bin/python -m pytest tests/`。

签名必须由人在设备上完成：`start_all.sh` / `demo_run.py` 都不会自动确认设备。
`scripts/speculos_autoconfirm.py` 只用于测试，必须显式设置 `SPECULOS_AUTOCONFIRM=1` 才能运行。

## 三行接入：让你的 agent 通过 AgentGuard 付款

```python
from agentguard import Guard

guard = Guard(policy_path="policy.yaml", agent_id="my-research-agent")
result = guard.fetch_paid("https://api.example.com/report", purpose="weekly report data", request_id="report-42")
```

`result["status"]` 是 `paid`（`result["content"]` 就是数据）、`pending_approval`（`result["approval_id"]`，
用 `guard.approval(id)` 查结果）、`denied`、`delivery_rejected` 等之一；同一个 `request_id` 重复调用只会返回已有结果，不会再付款。
钱包、RPC 地址等配置从 `.env` 读取（`Guard(env_file=...)` 或环境变量 `AGENTGUARD_HOME` 指定目录）。

用 MCP 接入（Claude Desktop、Strands、任何 MCP 客户端）：

```json
{"mcpServers": {"agentguard": {"command": "agentguard-mcp", "args": ["--agent-id", "my-research-agent"]}}}
```

安装：`uv pip install -e . --override overrides.txt`。必须加 override：x402-xrpl 0.3.4 锁了 `fastapi<0.116`、
`uvicorn<0.31`，与 mcp 2.x 冲突，不加的话依赖解析会失败（我们实测 x402 商户中间件在新版本上工作正常）。
普通 `pip` 不支持覆盖传递依赖的版本上限，所以这里用 uv。

## 组成

| 部分 | 位置 |
|---|---|
| 策略（R1–R8、预算、TTL、热钱包上限、escrow 阈值） | `policy.yaml`、`guard/policy.py` |
| 支付状态机、幂等、预算预留、提交超时对账 | `guard/state_machine.py`、`payments.py`、`budget.py`、`settlement.py` |
| x402 拦截与付款 | `guard/x402_gate.py`、`guard/payment_executor.py` |
| 审批、Ledger（Speculos）签名 | `guard/approval_service.py`、`guard/signers.py`、`guard/signing_requests.py` |
| 先验货再放款 | `guard/escrow.py`、`guard/delivery_verifier.py`、`merchants/escrow_merchant.py` |
| 热钱包封顶 / 补款 | `guard/hot_wallet.py`、`scripts/cap_hot_wallet.py` |
| MCP server / agent | `guard/mcp_server.py`、`agent/agent.py` |
| 控制台 | `frontend/` |

## 热钱包余额封顶

热钱包的私钥在服务器上（AUTO 小额付款要用），所以它的余额是服务器能直接动用的上限。

- `hot_wallet_max_balance_xrp`（默认 20 XRP）：`scripts/cap_hot_wallet.py` 把超出部分转回金库，带审计 Memo `agentguard/hot-wallet`。
- 补款只能由金库付：控制台 Treasury 页发起 → 生成审批单 → 人工批准 + Ledger 签名；发起时和签名前都用链上最新余额检查，补完不得超过上限。
- 金库的 Regular Key 是 Ledger 设备，主密钥已禁用：金库的每笔付款都要设备签名（本演示的设备是同机运行的 Speculos，见“安全边界”）。

## 先验货再放款：x402 的 escrow 扩展（不是标准 x402）

x402 是“先付款后拿货”：商户收到钱之后可能返回垃圾数据。对大额购买，AgentGuard 改用 XRPL 原生的条件式 Escrow：
钱先锁住，验货通过才放给商户，不通过就退回金库。

**这是 AgentGuard 自己定义的扩展，不是 x402 标准的一部分。** x402-xrpl 0.3.4 和 testnet facilitator
（`/supported` 只声明 `exact`、`upto`）都不支持 escrow，标准 x402 客户端和商户无法使用这个流程。

### 什么时候走 escrow

审批通过、金额超过 `escrow_threshold_xrp`（默认 5 XRP）、且商户在 402 报价里提供了 `escrow` 选项的购买。
只支持 `exact` 的商户（例如 `/data/b`）不受影响，仍是审批后直接付款。

### 报价格式

402 响应沿用 x402 v2 的 `PaymentRequired` 结构，但 `accepts` 里的 `scheme` 是自定义的 `"escrow"`：

```json
{
  "scheme": "escrow", "network": "xrpl:1", "asset": "XRP",
  "amount": "6000000", "payTo": "r...", "maxTimeoutSeconds": 600,
  "extra": {
    "invoiceId": "…",
    "description": "XRPL DEX 周成交量：dataSchema.coverage 声明的那一个 ISO 周（周一至周日）7 天，每天一个成交量数值（单位 XRP），附 7 天合计",
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

`description` 和 `dataSchema` 是商户对交付内容的承诺，验货就按它们检查。

**验收条件绑定在报价哈希里。** `quote_hash` 覆盖付款条款（scheme、network、asset、amount、payTo），以及验收条件：
商户描述（`extra.description` 和 `resource.description`）、`dataSchema`、交付条件（`mimeType`、`maxTimeoutSeconds`、`extra.escrow`）。
哈希前会先规范化：键排序，字符串做 NFC 并折叠空白，只去掉排版差异。审批单保存审批人看到的完整报价快照，
付款或锁款前会重新取一次报价，重新计算的哈希与审批单不一致就按 R8 拒绝。验货只用审批单快照里的描述和 schema，
不用商户付款时再给的版本。

**任何验收条件变化都会触发拒绝，这是有意的严格策略**：哪怕商户只改了一句描述或放宽一个字段，
审批人批准的也已经不是同一份承诺。这种情况下守卫不会付款，需要重新发起购买、重新审批。

### 流程

1. **锁款**：金库提交 `EscrowCreate`（收款人 = 商户，金额 = 报价，`Condition` = PREIMAGE-SHA-256 条件，
   `CancelAfter` = 账本时间 + `escrow_cancel_after_seconds`），由 Ledger 签名。原像只保存在守卫的数据库里，不经过任何 API。
2. **付款证明**：守卫带以下请求头再次请求资源（替代 x402 的 `PAYMENT-SIGNATURE`）：
   - `X-ESCROW-OWNER`：锁款账户
   - `X-ESCROW-SEQUENCE`：EscrowCreate 的 Sequence
   - `X-ESCROW-TX`：EscrowCreate 的交易哈希
3. **商户核对后交货**：商户在链上查这个 escrow：收款人是自己、金额等于报价、带条件、离 `CancelAfter` 还有足够时间、没有被用过。都满足才返回数据。
4. **验货**（`guard/delivery_verifier.py`）：
   - 通用确定性检查：JSON 对象、非空、必需字段、`daily` 至少 7 条、每条日期合法、数值非负
   - 七天 DEX 数据专项检查（`dataSchema.profile = "xrpl_dex_7day"`，只用于商户 E 这类数据）：
     `dataset`、`week`、单位与报价一致；日期全部落在 `coverage` 内、不重复、连续 7 天；
     每个数值是有限的非负数；`total_volume_xrp` 等于每日之和。
     验收周期只来自审批快照里冻结的 `coverage`，不用当前日期推算；商户在交付内容里写别的周次也没用，
     报价里改周期又会改变 `quote_hash`（R8）。周期取“上一个完整 ISO 周”，同一周内任何一天报价都相同，
     跨天运行不会误判（例如 10 月 24 日批准、10 月 25 日取货仍然是 2026-W42）。
     垃圾模式（2019 年的样例数据）在这一步就被拦下，不依赖 LLM
   - LLM 语义检查（主题是否一致、是否占位数据）：额外的一道，只能多拒。确定性检查不通过时不调用 LLM

   **LLM 不可用时怎么处理**（出错、超时、拒答）：
   - 七天 DEX 数据：确定性检查全部通过就放款，验货结论里注明“语义检查未完成”（`semantic_check: "incomplete"`）。
     这类数据的验收条件（周期、日期、单位、数值、合计）都能确定性检查，LLM 只是锦上添花
   - 其他没有专项检查的 schema：确定性检查管不到日期范围和内容，仍保守判为不通过（不放款、退款）
5. **放款**：验货通过，守卫提交 `EscrowFinish`（带原像），商户收到钱。
6. **退款**：验货不通过（或商户不交货），等账本时间超过 `CancelAfter` 后提交 `EscrowCancel`，钱退回金库。
   `CancelAfter` 之前链上不允许取消，守卫也会提前拦下。

`EscrowFinish` / `EscrowCancel` 任何账户都可以提交，由热钱包提交（只付手续费），不需要金库的钥匙。
三笔交易都带审计 Memo；Finish / Cancel 的 Memo 里有验货结论的 sha256（`verification_hash`），
任何人都可以拿控制台里的验货结论复算比对。

### 已知限制

- 商户只有在 `EscrowFinish` 之后才拿到钱，需要信任买方会如实验货；作为补偿，买方的验货结论和哈希都上链公开。
- 确定性检查只能证明数据“形状和周期对”，证明不了数值是真实的链上成交量；这一点靠 LLM 语义检查兜一部分，
  LLM 不可用时七天 DEX 数据只凭确定性检查放款（结论里会注明）。
- 演示商户 E 用 `POST /data/e/mode {"mode": "normal" | "garbage" | "malformed"}` 切换交付内容。

## 已知限制

- **商户查链兜底不是 x402 协议的一部分。** 标准 x402 流程里，商户把买方签好的交易交给 facilitator 结算，facilitator 确认后商户才交货。测试网 facilitator 不稳定时 `/settle` 会超时，x402 中间件回 409 `settlement_status_unknown`（钱可能已经上链，但不交货）。演示商户为此加了兜底（`merchants/settle_fallback.py`）：只在这种 409 时，按交易哈希直接查链，确认交易已验证成功、收款方、实收金额、InvoiceID 与自己签发的 invoice 一致、invoice 未过期且这笔交易没被用过，才交货。这是演示商户对标准流程的兜底，不改变协议，其他 x402 商户不一定有；守卫这一侧不依赖它（交货失败时守卫按交易哈希对账，绝不重复付款）。
