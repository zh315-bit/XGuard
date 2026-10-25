# Ledger DMK / Speculos / signer-kit-xrp 使用反馈

项目：AgentGuard（AI agent 支付守卫，XRPL Testnet），AI & Fintech XRPL Hackathon，2026-10-24。
场景：金库账户的 Regular Key 设为 Ledger 设备地址，大额付款由人在设备上核对后签名；现场没有实体设备，全部用 Speculos。

## 环境与版本

| 组件 | 版本 |
|---|---|
| `@ledgerhq/device-management-kit` | 1.11.0 |
| `@ledgerhq/device-signer-kit-xrp` | 0.3.0 |
| `@ledgerhq/device-transport-kit-speculos` | 1.3.0 |
| Speculos 镜像 | `ghcr.io/ledgerhq/speculos:latest`（arm64，2026-10-01 构建） |
| XRP app | 2.8.1（workshop 仓库提供的 `apps/stax/app.elf`） |
| 主机 | macOS 26.6（Apple Silicon），Docker 由 Colima 提供，Node 24，Python 3.12（xrpl-py 5.2.0） |

参考：[device-management-kit-ts-workshop/xrpl-hackathon](https://github.com/LedgerHQ/device-management-kit-ts-workshop/tree/main/xrpl-hackathon)（commit 4b85faa）。这个 workshop 非常有用，下面多数问题都是靠它绕过去的。

## 问题与绕过方法

### 1. macOS 上 5000 端口被 AirPlay 接收器占用
- **现象**：`lsof` 显示 `ControlCenter` 在 `*:5000` 监听，按文档把 Speculos 映射到 5000 会冲突。
- **绕过**：宿主机映射到 5001（`-p 127.0.0.1:5001:5000`），`speculosTransportFactory("http://127.0.0.1:5001", …)`。
- **建议**：workshop 的 Troubleshooting 提到了这一点，但 transport kit 的 README 和 DMK 文档没有；默认端口 5000 在新版 macOS 上几乎必然冲突，建议文档里直接给出换端口的示例。

### 2. 浏览器直连 Speculos 的 CORS：能用，但没有任何文档说明
- **实测**：Speculos 的 `speculos/api/api.py` 用 flask-cors 放开了所有来源（`origins: "*"`，`supports_credentials=True`），从 `http://localhost:5173` 发 `OPTIONS /apdu` 预检返回 200 并带 `Access-Control-Allow-Origin`，`/screenshot`、`/events`、`/finger` 同样可用，不需要代理。
- **建议**：`@ledgerhq/device-transport-kit-speculos` 的文档应说明“浏览器可以直接连，Speculos 自带 CORS”，否则开发者第一反应是去配代理。我们是读了 Speculos 源码并手工发预检请求才确认的。

### 3. `DeviceModelId` 的值是小写，传错大小写不会报错
- **现象**：枚举实际值是 `"stax"`、`"nanoX"`、`"apexp"` 等。我们一开始从环境变量传 `"STAX"`（强转成 `DeviceModelId`），`speculosTransportFactory(url, false, "STAX")` 照样连上、取地址、签名都成功，没有任何警告。
- **绕过**：自己写了 `modelFromName()`，把配置里的名字规范化成枚举值。
- **建议**：transport 对未知型号应该报错，或者至少在文档里写明型号参数的作用（是否只影响展示/屏幕尺寸）。Speculos 的 `--model apex_p` 和 DMK 的 `"apexp"` 命名也不一致。

### 4. `signTransaction` 的输入格式只在 workshop 里写了
- **现象**：`signTransaction(derivationPath, transaction: Uint8Array)` 的类型注释只说“Serialized XRP transaction”。实际必须是 `encode(tx)`（完整序列化，含 `SigningPubKey`、不含 `TxnSignature`），**不能**用 `encodeForSigning`，因为 XRP app 会自己加签名前缀。用错的话签名是“有效的”，但签的是另一串字节，上链才失败。
- **建议**：把这条写进 `signTransaction` 的 JSDoc 和 XRP signer 文档页，最好附一个 xrpl.js / xrpl-py 的完整例子（后端用 xrpl-py `binarycodec.encode`，前端用 xrpl.js `encode`，两边结果一致，我们验证过）。

### 5. 签名输出是 DER `Uint8Array`，转 hex 的参数含义不直观
- **现象**：需要 `bufferToHexaString(signature, false).toUpperCase()`；第二个参数（是否加 `0x` 前缀）从名字看不出来。XRPL 的 `TxnSignature` 和 `SigningPubKey` 都要求大写 hex。
- **建议**：signer-kit-xrp 直接提供 `signatureToTxnSignature()` 之类的辅助函数，或在文档写明。`getAddress` 返回的 `publicKey` 大小写在类型说明里也没写，我们统一转成大写。

### 6. Device action 返回 `{ observable, cancel }`，不是 Promise
- **现象**：每个 signer 方法都要自己订阅 observable、处理 `Pending / Completed / Error / Stopped` 四种状态，还要从 `intermediateValue.requiredUserInteraction` 里取“需要用户做什么”（`ConfirmOpenApp`、`SignTransaction` 等）才能给出界面提示。
- **绕过**：写了一个 `run()` 把它包成 Promise，并把 `requiredUserInteraction` 映射成中文提示（“请在设备屏幕上核对收款地址和金额，然后长按确认签名”）。workshop 的 `useDeviceAction.ts` 是 React 版本。
- **建议**：DMK 提供官方的 `toPromise(action, onIntermediate)`，并在文档列出 `requiredUserInteraction` 的所有取值和含义。

### 7. 设备上拒绝签名的错误形状没有文档
- **现象**：拒签时得到 `XrpAppCommandError`，`errorCode` 为 `6985`，消息是“Condition of use not satisfied (Rejected by user)”。错误对象的字段（`_tag`、`errorCode`、`message`）是打印出来才知道的。
- **建议**：文档列出常见错误码（6985 拒签、用户未打开 app 等）和推荐的判断方式（按 `_tag` 还是 `errorCode`）。

### 8. 0.x 版本号 + `^` 依赖会拿不到新版本
- **现象**：workshop 的 `package.json` 写的是 `"@ledgerhq/device-signer-kit-xrp": "^0.2.0"`，在 0.x 下 `^0.2.0` 不会升级到 0.3.0；0.3.0 新增了 `curve` 选项（secp256k1 / Ed25519）。
- **建议**：workshop 更新到 0.3.0，并在 changelog 里写明 0.2 → 0.3 的差异。

### 9. Speculos 没有“API 已就绪”的日志
- **现象**：容器起来后要等一会儿 HTTP API 才可用，日志里没有就绪信息。
- **绕过**：和 workshop 一样，轮询 `GET /` 直到返回 200。
- **建议**：Speculos 在 API 监听成功时打一行日志，或提供 `/health`。

### 10. 无人值守测试：`/events?currentscreenonly=true` 很好用，但没人提
- **现象**：做自动化测试时，需要知道设备当前停在哪一页。`GET /events?currentscreenonly=true` 返回当前屏幕上的所有文字及坐标，可以据此翻页、核对收款地址和金额，再在 “Hold to sign” 上按住（`/finger` 的 `press` → 等 3 秒 → `release`）。
- **坑**：Stax 上点 “Reject” 之后还有一个 “Reject transaction? / Yes, reject” 的二次确认，第一次只点 Reject，DMK 会一直等着不返回。
- **建议**：workshop 增加一节“自动化测试”，给出 Stax / Flex 的按钮坐标和这个 events 接口的用法。

### 11. LastLedgerSequence：autofill 只给约 1 分钟
- workshop 已经提示了：人在设备上核对需要时间，`autofill` 默认的 LastLedgerSequence 很容易让交易在签完之前就过期（`tefMAX_LEDGER`）。我们在后端构造交易时按报价有效期设置 LastLedgerSequence（约 +122 个账本）。建议把这一条放进 XRP signer 的官方文档，而不只是 workshop。

### 12. Regular Key 场景：设备显示的是被代签的账户
- **现象**：金库的 Regular Key 是设备地址，交易里 `Account` 是金库地址，`SigningPubKey` 是设备公钥。设备屏幕显示 “Account: 金库地址”，没有提示“正在以 Regular Key 身份代签”。签名本身完全正常、上链成功。
- **建议**：当 `SigningPubKey` 推导出的地址 ≠ `Account` 时，XRP app 可以多显示一行“Signing as regular key / delegated”，让签名人知道自己在动哪个账户的钱。

### 13. 长字段的显示
- x402 付款带 `InvoiceID`（64 位 hex）和两条 Memo（一条 invoiceId，一条我们的审计 JSON），在 Stax 上把审核拉长到 5 页；JSON memo 被按宽度折行，读起来不太方便。属于体验问题，不影响功能。我们核对时只看 `Amount` 和 `Destination`，这两项显示得很清楚。

### 14. 上一次签名被中断后，设备“忙”只报 `UnknownDeviceExchangeError`
- **现象**：脚本在等设备确认签名时被 Ctrl+C 中断，Speculos 仍停在 “Review transaction” 界面。之后任何新请求（包括 `getAddress`）都失败，DMK 只报 `UnknownDeviceExchangeError: Unexpected device exchange error happened.`；直接发 APDU 能看到设备回的是 `6901`。
- **绕过**：失败时读 Speculos 的 `/events?currentscreenonly=true`，如果停在审核界面，就提示用户在设备上 Reject → Yes, reject（或重启 Speculos）。
- **建议**：DMK 把“设备正忙于另一个 UI 流程”识别成单独的错误类型并给出提示；进程退出时如果有未完成的 device action，尽量通知设备取消。

## 我们最终的做法（供参考）

- 浏览器（审批控制台）：DMK + speculos transport 直连 `http://127.0.0.1:5001`；后端返回 `encode(tx)` 的 hex 和 tx JSON，前端签名后用 xrpl.js `encode({...tx, TxnSignature})` 组装 blob 提交。
- 后端在接受 blob 前校验：与待签交易逐字段一致、收款地址/金额/资产与审批单一致、quote_hash 一致、签名能用设备公钥验证。
- Node 脚本（`frontend/cli/ledger.ts`）复用同一个设备模块，给 Python 脚本（`lock_treasury.py`、`approve.py`）调用。
