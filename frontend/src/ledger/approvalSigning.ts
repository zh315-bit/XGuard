/**
 * 审批页面的“用 Ledger 签名”：
 *   1. POST /approvals/{id}/approve —— 后端预留预算、重新校验报价，准备好未签名交易（状态进入 SIGNING）
 *   2. 设备签名：DMK + speculos transport + signer-kit-xrp，输入 encode(tx)，输出 DER 签名
 *   3. 用 xrpl.js 把签名放进 TxnSignature，encode 成 blob
 *   4. POST /approvals/{id}/signature —— 后端校验收款地址、金额、资产、quote_hash 和签名后才提交
 * 设备上拒绝或出错 → POST /signature/cancel，这次付款失败，不会发出任何交易。
 */
import { encode } from "xrpl";
import { postJson, type Approval, type Config } from "../api";
import { LedgerDevice, modelFromName, type DeviceStatus } from "./device";

let device: Promise<LedgerDevice> | undefined;

function connectOnce(config: Config): Promise<LedgerDevice> {
  device ??= LedgerDevice.connect(config.speculos_url, modelFromName(config.speculos_model)).catch((e) => {
    device = undefined; // 连接失败下次重试
    throw e;
  });
  return device;
}

export async function approveWithLedger(approval: Approval, config: Config, onStatus: DeviceStatus,
                                        confirmPayeeSuffix?: string): Promise<void> {
  // 之前已批准（比如在命令行），签名请求还在等：直接签
  let request = approval.status === "approving" && approval.signing_request?.status === "pending"
    ? approval.signing_request
    : undefined;

  if (!request) {
    onStatus("后端正在重新校验报价、预留预算并准备待签交易…");
    // R3（收款方未认证）：后端要求带上审批人手动输入的收款地址后 4 位
    const view = await postJson<Approval>(`/approvals/${approval.id}/approve`,
      confirmPayeeSuffix ? { confirm_payee_suffix: confirmPayeeSuffix } : {});
    if (config.signer_mode !== "speculos") return;                 // software 模式：后端已经签好付完
    if (view.payment_state !== "SIGNING" || !view.signing_request) {
      throw new Error(view.reject_reason ?? view.payment_error ?? `批准未进入签名阶段（${view.payment_state}）`);
    }
    request = view.signing_request;
  }

  onStatus("连接 Ledger（Speculos）…");
  let signature: string;
  try {
    const ledger = await connectOnce(config);
    signature = await ledger.signTransaction(request.unsigned_tx_hex, onStatus);
  } catch (e) {
    device = undefined;
    const reason = e instanceof Error ? e.message : String(e);
    await postJson(`/approvals/${approval.id}/signature/cancel`, { reason: `设备签名失败：${reason}` }).catch(() => undefined);
    throw new Error(reason.includes("6985") ? "已在设备上拒绝签名" : reason);
  }

  onStatus("设备已签名，提交给后端校验并上链…");
  const signedBlob = encode({ ...request.tx_json, TxnSignature: signature } as Parameters<typeof encode>[0]);
  await postJson(`/approvals/${approval.id}/signature`, { signed_blob: signedBlob });
}
