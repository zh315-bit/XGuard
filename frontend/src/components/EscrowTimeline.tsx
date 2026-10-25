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
          Verify before release · <span className="font-semibold">{e.amount_drops / 1_000_000} XRP</span> → {short(e.destination)}
        </div>
        <EscrowStatus status={e.status} />
      </div>
      {e.description && <p className="mb-3 text-xs text-slate-400">Quote says: {e.description}</p>}
      {e.status === "failed" && e.payment_error && (
        <p className="mb-3 rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{e.payment_error}</p>
      )}
      <ol className="space-y-3 border-l border-slate-700 pl-4">
        <Step done={!!e.create_tx} bad={e.status === "failed"} title="① Lock: treasury EscrowCreate (Ledger-signed)">
          <TxLink hash={e.create_tx} url={e.create_tx_url} />
          <span className="ml-2 text-xs text-slate-500">Condition PREIMAGE-SHA-256; the guard holds the preimage</span>
        </Step>
        <Step done={!!e.delivery} title="② Delivery: fetch the data with the escrow payment proof">
          {e.delivery ? (
            <pre className="mt-1 max-h-28 overflow-auto rounded bg-slate-900 p-2 text-[11px] text-slate-400">
              {JSON.stringify(e.delivery, null, 1).slice(0, 600)}
            </pre>
          ) : <span className="text-xs text-slate-500">{e.status === "locked" ? "Waiting for the merchant to deliver…" : "The merchant did not deliver"}</span>}
        </Step>
        <Step done={!!v} bad={v?.verdict === "fail"} title="③ Verification: deterministic checks + LLM">
          {v ? (
            <div className="space-y-1">
              <div className={`text-sm font-medium ${v.verdict === "pass" ? "text-emerald-300" : "text-rose-300"}`}>
                {v.verdict === "pass" ? "✓ Pass" : "✗ Fail"} <span className="text-xs text-slate-500">({v.source})</span>
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
              {e.verification_hash && <div className="font-mono text-[10px] text-slate-600">Verdict hash {short(e.verification_hash, 10)} (written to the on-chain Memo)</div>}
            </div>
          ) : <span className="text-xs text-slate-500">Waiting for delivery</span>}
        </Step>
        {!refunding ? (
          <Step done={!!e.finish_tx} title="④ Release: EscrowFinish (submits the preimage)">
            {e.finish_tx ? <TxLink hash={e.finish_tx} url={e.finish_tx_url} /> : <span className="text-xs text-slate-500">Released once verification passes</span>}
          </Step>
        ) : (
          <Step done={!!e.cancel_tx} bad title="④ Refund: EscrowCancel (after CancelAfter)">
            {e.cancel_tx ? (
              <span><TxLink hash={e.cancel_tx} url={e.cancel_tx_url} /> <span className="ml-2 text-xs text-emerald-300">Returned to the treasury</span></span>
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
      {left > 0 ? `Funds are locked in escrow; refundable in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "Expired, submitting the refund…"}
    </span>
  );
}

function EscrowStatus({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    preparing: ["Preparing lock", "text-slate-400"], failed: ["Lock not completed · no funds moved", "text-rose-300"],
    locked: ["Locked", "text-sky-300"], delivered: ["Delivered", "text-sky-300"],
    releasing: ["Releasing", "text-sky-300"], released: ["Released", "text-emerald-300"],
    refund_pending: ["Verification failed · refund pending", "text-amber-300"], refunding: ["Refunding", "text-amber-300"],
    refunded: ["Refunded", "text-emerald-300"],
  };
  const [text, color] = map[status] ?? [status, "text-slate-400"];
  return <span className={`text-xs font-semibold ${color}`}>{text}</span>;
}
