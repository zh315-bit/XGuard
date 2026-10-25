# AgentGuard 2.0｜XRPL Hackathon 36 小时开发档案

**版本**：v1.0 · 2026-10-24  
**目标赛道**：Agentic Finance  
**项目定位**：The Financial Control Layer for Autonomous AI Agents  
**团队假设**：3–4 人，具备 Python / JavaScript 开发能力；Ledger 硬件可用性待确认  
**状态**：开发执行方案；第三方 SDK、网络功能与合作伙伴奖规则需在开工时验证

## 0. 一页执行摘要

**一句话 Pitch**：x402 lets agents pay. AgentGuard makes sure they spend within policy, with human approval when it matters.

**目标**：让研究型 AI Agent 在执行任务时购买付费数据，所有 x402 付款请求先经过可信报价校验、确定性策略引擎和预算控制；小额可信交易自动执行，大额交易人工审批，可疑交易阻断。付款在 XRPL Testnet 结算，后台展示风险解释、预算变化、交易哈希与审计轨迹。最终 Agent 使用成功购买的数据生成研究摘要。

**胜负手**：可重复演示的真实 XRPL 支付 + 安全边界真实生效 + 人在环中审批 + 有业务闭环的可视化 Demo。

**绝对不做**：在核心闭环未完成前开发 Escrow、Batch、复杂智能合约、多链桥、完整会计 ERP、生产级托管钱包或泛化的 Agent 平台。

### 完成标准（Definition of Done）

1. Agent 调用受控工具获取付费资源，不能直接读取付款私钥。
2. 正常小额请求成功完成 XRPL Testnet Payment，并展示已验证的交易哈希。
3. 超单笔限额请求进入 HOLD，只有授权签名后才能继续。
4. 黑名单、网络不符、超硬上限或报价被篡改时进入 DENY，且没有任何付款交易广播。
5. 重复请求不得导致重复付款；并发请求不得绕过预算。
6. 控制台能看到预算、判定原因、审批记录、交易状态及最终任务结果。
7. GitHub README、环境示例、测试命令、演示视频和至少一个真实交易浏览器链接齐全。

## 1. 背景、差异化与用户故事

### 1.1 痛点

x402 允许 AI Agent 为 API / 数据付费，但 HTTP 402 报价是外部输入，价格、收款地址、资产和网络都需要验证。完全自动付款存在过度支出和欺骗风险；每笔都人工审批又破坏自主性。企业还需要预算控制、职责分离和审计证据。

### 1.2 目标用户

使用 AI Agent 自动采购数据、调用付费 API 的开发者、研究团队和企业财务负责人。**MVP 是测试网概念验证，不宣称具备生产级安全认证。**

### 1.3 核心创新

**Risk-Adaptive Autonomy**：策略驱动的三级自主权 AUTO / HOLD / DENY。LLM 负责解释和辅助识别风险，最终资金权限由确定性规则、授权身份和签名系统决定。

### 1.4 Demo 故事

用户要求研究 Agent 生成一份 XRPL 生态简报，预算 10 XRP。Agent 依次访问三个付费数据源：

- **商户 A**：认证地址，报价 0.01 XRP，预算内 → AUTO → 真实支付 → 返回数据。
- **商户 B**：认证地址，报价 8 XRP，超过自动单笔限额但低于硬上限 → HOLD → 人工确认签名 → 真实支付 → 返回数据。
- **商户 C**：返回未知或被篡改收款地址（可模拟与登记地址不一致） → DENY → 不广播交易 → Agent 换用备用来源。
- **收尾**：Agent 生成研究摘要；控制台展示已付 8.01 XRP、剩余预算 1.99 XRP、两笔付款、一次拒绝，以及每项决策证据。数字为演示设定，实际以测试网执行结果为准。

## 2. 功能范围与优先级

| 等级 | 功能 | 验收依据 | 负责人 |
|---|---|---|---|
| P0 | 受控 x402 请求与报价规范化 | 402 可解析，异常报价 fail-closed | 后端 |
| P0 | AUTO / HOLD / DENY 策略引擎 | 单元测试覆盖所有规则 | 后端 |
| P0 | XRPL Testnet Payment | 实际交易哈希、验证成功 | XRPL |
| P0 | 人工审批（软件签名可降级） | 未审批不支付，审批后才签名 | XRPL+前端 |
| P0 | 防重复与预算预留 | 相同请求只产生一次结算；并发不超额 | 后端 |
| P0 | SQLite 审计日志 | 包含拒绝、待审批、成功、失败 | 后端 |
| P0 | 三商户端到端演示 | 三种决策可重复触发 | 全员 |
| P1 | AI 风险解释 | 使用可信结构化字段生成理由，不决定放款 | AI |
| P1 | 预算及风险控制台 | 显示余额、预算、审批和审计轨迹 | 前端 |
| P1 | Agent 最终研究摘要 | 只使用成功购买或公开数据 | AI |
| P1 | 商户 Credential 验证 | XRPL 链上凭证经真实验证 | XRPL |
| P1 | MCP 工具接口 | Agent 只能通过守卫请求付款 | AI |
| P2 | Ledger 真机签名 | 真设备、真实签名、测试网提交 | XRPL |
| P2 | QuickNode / AWS 集成 | 按当前奖项规则验证实际要求 | XRPL/AI |
| 暂缓 | Escrow / Batch | 不纳入 36 小时承诺 | — |

> 真实 Ledger 设备若已到手，真机签名可从 P2 提前到并行 P1；否则先保证主赛道 MVP。商户 Credentials 和 MCP 不应阻塞支付闭环。

## 3. 架构

```text
User: "Create an XRPL market brief within 10 XRP"
                         |
                  Research Agent
                         |
             fetch_paid_resource() [MCP/HTTP]
                         |
                AgentGuard API (FastAPI)
                         |
          x402 Quote Probe + Normalization
                         |
          Quote Binding / Merchant Verification
                         |
           Deterministic Policy Engine
                  /       |       \
               AUTO     HOLD      DENY
                |         |         |
           Reserve $   Approval   Audit only
                |         |         |
                |    Human signature|
                +---------+         |
                         |          |
                XRPL Testnet Payment |
                         |          |
                 Verify transaction  |
                         |          |
                 Audit / Budget DB <-+
                         |
                 Dashboard + AI Explanation
                         |
             Agent receives paid resource
                         |
                Research summary
```

**技术选型**：Python 3.11+、FastAPI、Pydantic、SQLite（事务 + 唯一索引）、xrpl-py、requests/httpx、React/Vite/TypeScript、Tailwind、可选 LLM API、可选 MCP SDK、可选 Ledger DMK。测试用 pytest；演示商户由 FastAPI 提供。

**信任边界**：Agent 不接触密钥；LLM 不发起链上签名；报价服务器不可信；客户端传入的 `approved=true` 不可信；审批必须绑定请求 ID、收款地址、资产、金额、网络和过期时间。

## 4. 钱包与网络

| 钱包 | 用途 | 安全边界 |
|---|---|---|
| Treasury | 人工审批的大额付款、给热钱包补款 | Ledger 真机优先；无真机用隔离测试网软件钱包 |
| Agent Hot Wallet | 低额自动付款 | 服务端受控，仅持少量测试 XRP |
| Issuer（可选） | 签发商户凭证 | 仅开发测试 |
| Merchant A/B/C | 模拟收款 | 测试网账户 |

所有私钥放在本地 `.env`，禁止提交仓库；不要使用真实资金或主网私钥。热钱包余额封顶，预算与钱包余额是两个独立约束。Ledger 硬件签名库、网络 ID、x402 facilitator、Credentials / Batch 可用性均在开工时通过当前官方文档和实际测试确认，不硬编码未经验证的假设。

## 5. 支付状态机与规则

### 5.1 状态机

`RECEIVED → QUOTED → EVALUATED → {DENIED | PENDING_APPROVAL | RESERVED} → SIGNING → SUBMITTED → VALIDATED → RESOURCE_DELIVERED`

异常分支：`EXPIRED / REJECTED / FAILED / UNKNOWN_SETTLEMENT`。超时不等于未支付；未知结算状态先查链上，不允许盲目重发。

### 5.2 判定优先级

1. **DENY**：网络或资产不允许、黑名单、硬上限、商户地址与已绑定报价不符、过期或无效报价、显式重复已结算请求。
2. **HOLD**：未认证但未列入黑名单的商户、超过单笔自动上限、超过自动预算阈值、频率异常或模型风险提示（仅作为人工复核依据）。
3. **AUTO**：已认证且所有硬约束满足、金额与预算均允许。

预算不足时，不能简单把 HOLD 变成可绕过预算的批准。人工审批也必须遵守绝对预算和硬上限；若允许额外支出，需独立的预算调整动作并留痕。

### 5.3 示例 policy.yaml

```yaml
network_allowlist: ["xrpl:1"]  # 启动时以实际 x402 网络约定校验
asset_allowlist: ["XRP"]
auto_payment_limit_xrp: 1
approval_limit_xrp: 10
hard_limit_xrp: 50
daily_budget_xrp: 10
hot_wallet_max_balance_xrp: 20
merchant_rate_limit_per_minute: 3
quote_ttl_seconds: 120
require_merchant_match: true
```

实际支付资产单位、XRP drops 转换及 x402 的网络表示须通过所选 SDK 确认；所有金额使用 Decimal/整数最小单位，不用浮点数处理资金。

## 6. x402 安全集成设计

1. 以普通 HTTP 请求获取 402 响应。
2. 按 SDK 实际版本解析 `payment-required` 等字段，得到规范化报价：`resource_url, method, payee, amount, asset, network, quote_id, expiry`。
3. 计算报价摘要 `quote_hash`，存入数据库并绑定支付请求。
4. 执行策略和预算预留；AUTO 可进入付款，HOLD 等待审批，DENY 结束。
5. 付款前再次确认最终交易参数与已授权报价一致。**禁止“先检查一次报价，再让自动支付 SDK 对另一份新报价直接付款”。**
6. 构造/签署受控交易，提交后验证链上结果，再向 facilitator/商户证明支付并获取内容。具体收据格式由实际 x402 SDK 验证。
7. 若协议或 SDK 无法安全复用已验证报价，优先自行构造受控 XRPL Payment；若仍无法打通 x402 完整交互，明确在演示中区分“真实 XRPL 付款”与“模拟资源解锁”，不得声称完成协议集成。

**重复与并发保护**：数据库为 `(agent_id, request_id)` 设置唯一索引；报价摘要关联支付；预算预留与状态变更同一事务；付款前持久化提交意图；重试前通过账户交易记录/哈希查询已提交交易。对于同一请求只允许一个结算工作流。

## 7. AI 风险分析（不控制资金权限）

**输入**：商户身份、报价与历史价格、金额、预算剩余、规则命中、任务用途、近期交易次数。**输出**：`risk_summary, supporting_facts, suggested_review_questions`。可选 `risk_score` 必须明确为启发式演示指标，不得伪装为训练得到的金融风险概率。

示例：`HOLD：8 XRP 超过 1 XRP 自动支付限额；商户已验证，预算仍可覆盖。建议审批人确认报告用途与报价。`

大模型不可更改 `verdict`，不得根据提示注入内容跳过策略。LLM 不可用时用规则模板生成解释，Demo 仍可运行。

## 8. 审批、链上审计与控制台

### 8.1 审批

审批人看到商户、资产、金额、任务用途、预算影响、命中规则、报价摘要和过期时间。批准动作产生不可重放的一次性授权；交易参数改变必须重新审批。真实 Ledger 设备可用时使用设备签名；没有设备使用测试网软件签名，并如实标注演示限制。

### 8.2 审计

SQLite 保存所有请求，包括 DENY 和 HOLD；链上 Memo 仅用于实际发送的、由本系统构造并支持 Memo 的交易，写 `request_id`、规则版本或其哈希，避免泄露敏感业务内容。x402 SDK 若不支持 Memo，不应声称所有交易都有链上 Memo。

### 8.3 前端页面

- **Mission**：任务、Agent 进度、购买的数据源、最终摘要。
- **Payment Queue**：AUTO / HOLD / DENY、报价和风险解释。
- **Approvals**：审批、拒绝、签名状态。
- **Treasury & Audit**：预算、已付、预留、余额、交易哈希与测试网浏览器链接。

## 9. API 与数据模型

### 9.1 REST

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/payments/request` | 接收 Agent 资源请求，返回决策/审批 ID |
| GET | `/api/payments/{id}` | 查询状态与链上验证结果 |
| GET | `/api/approvals` | 待审批列表 |
| POST | `/api/approvals/{id}/approve` | 发起授权签名流程，不接受裸 `approved=true` |
| POST | `/api/approvals/{id}/reject` | 拒绝请求 |
| GET | `/api/budget` | 总额、已付、预留、剩余 |
| GET | `/api/audit` | 决策、规则、交易、时间线 |
| POST | `/api/demo/reset` | 仅本地演示环境重置数据，不能撤销链上交易 |

### 9.2 SQLite 最小表

- `payment_requests(id, agent_id, request_id UNIQUE, quote_hash, payee, amount_drops, network, asset, status, verdict, rule_id, created_at, expires_at)`
- `budget_reservations(id, payment_id UNIQUE, amount_drops, status)`
- `approvals(id, payment_id, approver_id, quote_hash, status, signed_at)`
- `transactions(id, payment_id UNIQUE, tx_hash, ledger_result, validated_at)`
- `audit_events(id, payment_id, event_type, rule_version, details_json, created_at)`

## 10. 项目目录

```text
agentguard/
├── backend/
│   ├── app.py
│   ├── models.py
│   ├── policy.py
│   ├── quote.py
│   ├── payments.py
│   ├── xrpl_client.py
│   ├── approvals.py
│   ├── budget.py
│   ├── audit.py
│   ├── risk_explainer.py
│   └── db.py
├── agent/
│   ├── research_agent.py
│   └── mcp_tools.py           # P1
├── merchants/
│   └── demo_merchants.py
├── frontend/
│   └── src/
├── tests/
│   ├── test_policy.py
│   ├── test_idempotency.py
│   ├── test_budget.py
│   └── test_quote_binding.py
├── scripts/
│   ├── setup_testnet.py
│   └── demo_run.py
├── policy.yaml
├── .env.example
└── README.md
```

## 11. 36 小时排期（四人并行）

| 时间 | XRPL 开发 | 后端/AI | 前端 | 产品/测试/演示 | 阶段门槛 |
|---|---|---|---|---|---|
| 0–3h | 钱包、测试网转账、SDK 验证 | 策略框架、数据库 | 页面骨架 | 核对规则、商户脚本 | 第一笔测试网交易成功 |
| 3–8h | 支付封装、交易验证 | 报价解析、状态机 | 队列和预算页 | 三商户数据与测试用例 | 报价→判定跑通 |
| 8–14h | 签名/审批接口 | 幂等、预算预留 | 审批页 | 联调脚本、README | AUTO/HOLD/DENY 完整 |
| 14–18h | x402 收据与资源解锁 | Agent 工具接入 | 交易详情 | 端到端验证 | 核心闭环 P0 完成 |
| 18–24h | Ledger 真机或 Credentials（择一） | AI 风险解释 | Mission 页面 | 攻击演示与自动化测试 | 产品故事可演示 |
| 24–28h | 修复结算边界 | Agent 研究摘要 | UI 打磨 | 部署与录屏 | 冻结新功能 |
| 28–32h | 测试网稳定性 | 回归测试 | 错误态修复 | README、提交材料 | 可重复演示 |
| 32–36h | 现场支持 | 现场支持 | 现场支持 | Pitch 彩排、最终提交 | 提交完成 |

**关键止损**：3h 无测试网交易 → XRPL 专人排障；8h x402 SDK 不兼容 → 改受控 Payment + 明确协议降级；14h 审批签名未通 → 先用隔离软件签名；18h P0 未完成 → 取消全部 P1/P2；28h 后不增加功能。

### 三人团队调整

A：XRPL + 钱包签名；B：后端 + AI + 商户；C：前端 + 产品 + 测试。优先删除复杂 UI、AWS 和 Credentials，保留测试、文档、录屏。

## 12. 测试矩阵

| 测试 | 输入 | 期望 |
|---|---|---|
| T01 正常付款 | 已认证商户，0.01 XRP | AUTO，真实链上成功 |
| T02 大额审批 | 已认证商户，8 XRP | HOLD，签名后付款 |
| T03 恶意收款地址 | 地址与绑定商户不符 | DENY，无付款 |
| T04 过期报价 | expiry 已过 | DENY，无付款 |
| T05 不允许网络 | 错误网络 | DENY，无付款 |
| T06 超硬上限 | 51 XRP | DENY，即使人工批准也不付款 |
| T07 重复请求 | 相同 request_id 两次 | 仅一笔付款 |
| T08 并发预算 | 两笔请求合计超过剩余预算 | 不得两笔都通过 |
| T09 报价替换 | 检查后金额/地址变化 | 拒绝付款并要求重新评估 |
| T10 签名取消 | HOLD 被拒绝 | 无链上付款 |
| T11 RPC 超时 | 提交后响应丢失 | 先查询结算，不盲目重付 |
| T12 LLM 不可用 | AI API 故障 | 策略正常，模板解释可用 |

## 13. 三分钟 Demo 与英文 Pitch

**0:00–0:25 痛点**：AI agents can pay for APIs, but a malicious or inflated payment request can drain their funds. Fully manual approvals defeat autonomy.

**0:25–0:50 解决方案**：AgentGuard sits between agents and wallets. It evaluates every request with programmable policies and routes payments to AUTO, HOLD, or DENY.

**0:50–1:25 正常付款**：Agent 购买商户 A 数据，展示真实交易哈希与解锁数据。

**1:25–2:05 人工审批**：8 XRP 请求进入 HOLD，展示风险解释与预算影响，批准后签名付款；若真 Ledger 可用，展示设备确认。

**2:05–2:35 攻击拦截**：商户 C 返回不可信地址，显示 DENY，链上无付款，Agent 选择替代来源。

**2:35–3:00 闭环**：展示研究摘要、预算与审计时间线；强调 XRPL 结算与可编程控制。

**收尾句**：*Agents decide what they need. AgentGuard controls what they are allowed to spend. Humans stay in control when it matters.*

## 14. 奖项策略与待确认事项

- **主目标**：Agentic Finance；以真实交易、安全设计和完成度竞争。
- **Ledger 合作伙伴奖**：必须向主办方确认真实设备签名及演示要求；软件钱包不能替代硬件获奖条件。
- **QuickNode**：如果规则允许且接入成本低，使用其 XRPL RPC；确认当前奖项报名及技术要求。
- **AWS**：只在 P0/P1 完成后考虑 Strands / AgentCore，避免部署成本影响主线。
- **Credentials**：验证 XRPL Testnet 上对应功能与 SDK 的当前状态，再决定是否接入。

**开工即核对**：赛事截止时间、报名 track、合作伙伴奖条件、是否可借 Ledger、x402 SDK 当前 API、Testnet 网络与资产编码、facilitator 是否可用、链上 Credentials 可用性。

## 15. 交付清单

- [ ] 公开 GitHub 仓库；README 有架构、运行步骤、环境变量说明
- [ ] `.env.example` 无密钥；`.gitignore` 排除所有私钥文件
- [ ] 可运行前后端、三个演示商户、Agent
- [ ] 策略/幂等/预算/报价绑定测试通过
- [ ] XRPL Testnet 成功交易哈希和可打开的浏览器链接
- [ ] 三种判定 Demo 录屏；失败或离线时可播放
- [ ] 说明真 Ledger 是否实际使用、Credentials 是否实际部署
- [ ] 2–3 分钟英文 Pitch 和提交页项目简介
- [ ] 在官方截止时间前完成提交，预留至少 1 小时

## 16. 参考资料（开发时验证最新版本）

- XRPL Agentic Payments / x402：https://xrpl.org/docs/agents/agentic-payments-x402
- XRPL Amendments：https://xrpl.org/resources/known-amendments
- XRPL Testnet Faucet：https://xrpl.org/resources/dev-tools/xrp-faucets
- x402-xrpl Python：https://pypi.org/project/x402-xrpl/
- Ledger DMK：https://github.com/LedgerHQ/device-sdk-ts
- Ledger XRPL Hackathon：https://developers.ledger.com/xrplnyc
- 赛事资源：https://hackathons.xrpl-commons.org/hackathons/ai-fintech-xrp-ledger-hackathon-f78ed817?tab=resources

---

**产品原则**：先做真正安全、可验证的支付闭环，再做智能解释与体验；绝不为了演示效果绕过资金控制。
