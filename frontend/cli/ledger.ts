/**
 * 后端脚本用的 Ledger 命令行（Node，经 Speculos 传输），输出一行 JSON 到 stdout，提示信息到 stderr。
 *
 *   npx tsx cli/ledger.ts address
 *   npx tsx cli/ledger.ts sign <未签名交易的 encode() hex>
 *
 * 环境变量：SPECULOS_URL（默认 http://127.0.0.1:5001）、SPECULOS_MODEL（默认 STAX）
 */
import "reflect-metadata";
import { DEFAULT_SPECULOS_URL, LedgerDevice, modelFromName } from "../src/ledger/device";

async function main() {
  const [command, arg] = process.argv.slice(2);
  const url = process.env.SPECULOS_URL ?? DEFAULT_SPECULOS_URL;
  const model = modelFromName(process.env.SPECULOS_MODEL ?? "stax");
  const status = (msg: string) => console.error(`[ledger] ${msg}`);

  const device = await LedgerDevice.connect(url, model);
  try {
    if (command === "address") {
      console.log(JSON.stringify(await device.getAddress(status)));
    } else if (command === "sign" && arg) {
      console.log(JSON.stringify({ signature: await device.signTransaction(arg, status) }));
    } else {
      throw new Error("用法：ledger.ts address | ledger.ts sign <unsigned_tx_hex>");
    }
  } finally {
    await device.close();
  }
}

main().then(
  () => process.exit(0),
  (e) => {
    console.error(`[ledger] 失败：${e instanceof Error ? e.message : e}`);
    process.exit(1);
  },
);
