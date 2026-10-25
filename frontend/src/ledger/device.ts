/**
 * Ledger 设备访问：DMK + Speculos 传输 + XRP signer。浏览器（审批页面）和 Node（cli/ledger.ts）共用。
 *
 * 做法与 Ledger 官方 workshop（device-management-kit-ts-workshop/xrpl-hackathon）一致：
 * - 浏览器直接连 Speculos 的 HTTP API（Speculos 用 flask-cors 放开了所有来源，不需要代理）
 * - signTransaction 的输入是 xrpl 的 encode(tx)，不是 encodeForSigning：XRP app 自己加签名前缀
 * - 输出是 DER 签名，大写 hex 后放进 TxnSignature
 */
import {
  DeviceActionStatus,
  DeviceManagementKitBuilder,
  DeviceModelId,
  bufferToHexaString,
  hexaStringToBuffer,
  type DeviceManagementKit,
  type DeviceSessionId,
  type ExecuteDeviceActionReturnType,
} from "@ledgerhq/device-management-kit";
import { SignerXrpBuilder, type SignerXrp } from "@ledgerhq/device-signer-kit-xrp";
import { speculosIdentifier, speculosTransportFactory } from "@ledgerhq/device-transport-kit-speculos";
import { firstValueFrom } from "rxjs";

export const DERIVATION_PATH = "44'/144'/0'/0/0";
export const DEFAULT_SPECULOS_URL = "http://127.0.0.1:5001";

export type DeviceStatus = (message: string) => void;

/** 把配置里的型号名（STAX / stax / nanox / NANO_X …）转成 DMK 的 DeviceModelId（值是小写，如 "stax"）。 */
export function modelFromName(name: string): DeviceModelId {
  const key = name.replace(/[_\s-]/g, "").toLowerCase();
  const match = Object.values(DeviceModelId).find((v) => v.toLowerCase() === key);
  if (!match) throw new Error(`Unknown device model ${name}; options: ${Object.values(DeviceModelId).join(", ")}`);
  return match;
}

export class LedgerDevice {
  private constructor(
    private readonly dmk: DeviceManagementKit,
    private readonly sessionId: DeviceSessionId,
    private readonly signer: SignerXrp,
  ) {}

  /** 发现 Speculos 设备并建立会话。model 必须与 Speculos 启动时的 --model 一致。 */
  static async connect(url = DEFAULT_SPECULOS_URL, model = DeviceModelId.STAX): Promise<LedgerDevice> {
    const dmk = new DeviceManagementKitBuilder().addTransport(speculosTransportFactory(url, false, model)).build();
    const device = await firstValueFrom(dmk.startDiscovering({ transport: speculosIdentifier }));
    // 关掉会话的后台轮询（默认每秒发一次 GetAppAndVersion）。设备停在签名审核界面时收到这条 APDU，
    // XRP app 会回 0x6901，把正在等人确认的签名打断；另一个标签页或没关掉的旧会话的轮询同样会打断。
    const sessionId = await dmk.connect({ device, sessionRefresherOptions: { isRefresherDisabled: true } });
    const signer = new SignerXrpBuilder({ dmk, sessionId }).build();
    return new LedgerDevice(dmk, sessionId, signer);
  }

  async getAddress(onStatus?: DeviceStatus): Promise<{ address: string; publicKey: string }> {
    const out = await run(this.signer.getAddress(DERIVATION_PATH, { checkOnDevice: false }), onStatus);
    // XRPL 要求 SigningPubKey 是大写 hex
    return { address: out.address, publicKey: out.publicKey.toUpperCase() };
  }

  /** 输入：未签名交易的 encode(tx) hex（SigningPubKey 已设为设备公钥）。输出：DER 签名的大写 hex。 */
  async signTransaction(unsignedTxHex: string, onStatus?: DeviceStatus): Promise<string> {
    const bytes = hexaStringToBuffer(unsignedTxHex);
    if (!bytes) throw new Error("The unsigned transaction is not valid hex");
    const signature = await run(this.signer.signTransaction(DERIVATION_PATH, bytes), onStatus);
    return bufferToHexaString(signature, false).toUpperCase();
  }

  async close(): Promise<void> {
    await this.dmk.disconnect({ sessionId: this.sessionId }).catch(() => undefined);
    this.dmk.close();
  }
}

const INTERACTION_TEXT: Record<string, string> = {
  UnlockDevice: "Unlock the device",
  ConfirmOpenApp: "Confirm opening the XRP app on the device",
  SignTransaction: "Check the payee and amount on the device screen, then hold to sign",
  VerifyAddress: "Verify the address on the device",
};

/** 把 DMK 的 device action（observable）变成 Promise；pending 时把需要用户做的事回调出去。 */
function run<O, E, I extends { requiredUserInteraction?: unknown }>(
  action: ExecuteDeviceActionReturnType<O, E, I>,
  onStatus?: DeviceStatus,
): Promise<O> {
  return new Promise((resolve, reject) => {
    let lastHint = "";
    action.observable.subscribe({
      next: (state) => {
        switch (state.status) {
          case DeviceActionStatus.Pending: {
            const needed = String(state.intermediateValue?.requiredUserInteraction ?? "None");
            const hint = INTERACTION_TEXT[needed] ?? "";
            if (hint && hint !== lastHint) onStatus?.(hint);
            lastHint = hint;
            break;
          }
          case DeviceActionStatus.Completed:
            resolve(state.output);
            break;
          case DeviceActionStatus.Error:
            reject(new Error(describeError(state.error)));
            break;
          case DeviceActionStatus.Stopped:
            reject(new Error("The device action was cancelled"));
            break;
        }
      },
      error: (e) => reject(e instanceof Error ? e : new Error(String(e))),
    });
  });
}

function describeError(error: unknown): string {
  if (error && typeof error === "object") {
    const e = error as { _tag?: string; message?: string; errorCode?: string };
    return [e._tag, e.errorCode, e.message].filter(Boolean).join(": ") || JSON.stringify(error);
  }
  return String(error);
}
