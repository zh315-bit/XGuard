import { useEffect, useState } from "react";
import type { Escrow } from "../api";
import { TxLink, short } from "./ui";

/** 一笔 escrow 的全过程：锁款 → 交货 → 验货 → 放款，或 → 退款倒计时 → 退款。 */
export function EscrowTimeline({ escrow: e }: { escrow: Escrow }) {
  const v = e.verification;
  const refunding = ["refund_pending", "refunding", "refunded"].includes(e.status);
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm text-slate-200">
          先验货再放款 · <span className="font-semibold">{e.amount_drops / 1_000_000} XRP</span> → {short(e.destination)}
        </div>
        <EscrowStatus status={e.status} />
      </div>
      {e.description && <p className="mb-3 text-xs text-slate-400">报价声明：{e.description}</p>}
      {e.status === "failed" && e.payment_error && (
        <p className="mb-3 rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{e.payment_error}</p>
      )}
      <ol className="space-y-3 border-l border-slate-700 pl-4">
        <Step done={!!e.create_tx} bad={e.status === "failed"} title="① 锁款：金库 EscrowCreate（Ledger 签名）">
          <TxLink hash={e.create_tx} url={e.create_tx_url} />
          <span className="ml-2 text-xs text-slate-500">条件 PREIMAGE-SHA-256，原像由守卫保管</span>
        </Step>
        <Step done={!!e.delivery} title="② 交货：带 escrow 付款证明取数据">
          {e.delivery ? (
            <pre className="mt-1 max-h-28 overflow-auto rounded bg-slate-900 p-2 text-[11px] text-slate-400">
              {JSON.stringify(e.delivery, null, 1).slice(0, 600)}
            </pre>
          ) : <span className="text-xs text-slate-500">{e.status === "locked" ? "等待商户交货…" : "商户没有交货"}</span>}
        </Step>
        <Step done={!!v} bad={v?.verdict === "fail"} title="③ 验货：确定性检查 + LLM">
          {v ? (
            <div className="space-y-1">
              <div className={`text-sm font-medium ${v.verdict === "pass" ? "text-emerald-300" : "text-rose-300"}`}>
                {v.verdict === "pass" ? "✓ 通过" : "✗ 不通过"} <span className="text-xs text-slate-500">（{v.source}）</span>
              </div>
              <p className="text-xs text-slate-300">{v.reason}</p>
              <ul className="list-disc pl-4 text-[11px] text-slate-500">
                {v.evidence.slice(0, 4).map((x, i) => <li key={i}>{x}</li>)}
              </ul>
              <div className="flex flex-wrap gap-1">
                {v.checks.map((c) => (
                  <span key={c.name} title={c.detail}
                        className={`rounded px-1.5 py-0.5 text-[10px] ${c.ok ? "bg-emerald-500/10 text-emerald-300" : "bg-rose-500/10 text-rose-300"}`}>
                    {c.ok ? "✓" : "✗"} {c.name}
                  </span>
                ))}
              </div>
              {e.verification_hash && <div className="font-mono text-[10px] text-slate-600">结论哈希 {short(e.verification_hash, 10)}（写进链上 Memo）</div>}
            </div>
          ) : <span className="text-xs text-slate-500">等待交货</span>}
        </Step>
        {!refunding ? (
          <Step done={!!e.finish_tx} title="④ 放款：EscrowFinish（提交原像）">
            {e.finish_tx ? <TxLink hash={e.finish_tx} url={e.finish_tx_url} /> : <span className="text-xs text-slate-500">验货通过后放款</span>}
          </Step>
        ) : (
          <Step done={!!e.cancel_tx} bad title="④ 退款：EscrowCancel（CancelAfter 之后）">
            {e.cancel_tx ? (
              <span><TxLink hash={e.cancel_tx} url={e.cancel_tx_url} /> <span className="ml-2 text-xs text-emerald-300">已退回金库</span></span>
            ) : <Countdown target={e.cancel_after_unix} />}
          </Step>
        )}
      </ol>
    </div>
  );
}

function Step({ done, bad, title, children }: { done: boolean; bad?: boolean; title: string; children: React.ReactNode }) {
  const dot = done ? (bad ? "bg-rose-400" : "bg-emerald-400") : "bg-slate-600";
  return (
    <li className="relative">
      <span className={`absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ${dot}`} />
      <div className="text-xs font-medium text-slate-300">{title}</div>
      <div className="mt-0.5">{children}</div>
    </li>
  );
}

function Countdown({ target }: { target: number }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);
  const left = Math.max(0, Math.round(target - now));
  return (
    <span className="text-xs text-amber-300">
      {left > 0 ? `钱锁在 escrow 里，${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} 后可以退款` : "已到期，正在提交退款…"}
    </span>
  );
}

function EscrowStatus({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    preparing: ["准备锁款", "text-slate-400"], failed: ["锁款未完成 · 没有动钱", "text-rose-300"],
    locked: ["已锁款", "text-sky-300"], delivered: ["已交货", "text-sky-300"],
    releasing: ["放款中", "text-sky-300"], released: ["已放款", "text-emerald-300"],
    refund_pending: ["验货不通过 · 待退款", "text-amber-300"], refunding: ["退款中", "text-amber-300"],
    refunded: ["已退款", "text-emerald-300"],
  };
  const [text, color] = map[status] ?? [status, "text-slate-400"];
  return <span className={`text-xs font-semibold ${color}`}>{text}</span>;
}
