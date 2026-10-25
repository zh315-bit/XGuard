/** 独立哨兵（watchdog/sentinel.py）：所有页面顶部的 UNAUTHORIZED 警报 + Treasury 页的实时监控卡片。 */
import { useState } from "react";
import { postJson, usePoll, type Lockdown, type Monitor, type MonitorTx } from "../api";
import { Card, Empty, Stat, TxLink, short, timeAgo } from "./ui";

const ROLE_TEXT: Record<string, string> = { treasury: "Treasury", hot_wallet: "Hot wallet" };

function party(address?: string, role?: string | null) {
  if (!address) return "—";
  return role ? ROLE_TEXT[role] ?? role : short(address);
}

export function AlertBanner() {
  const { data, refresh } = usePoll<Monitor>("/api/monitor");
  const lock = usePoll<Lockdown>("/api/lockdown");
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [confirming, setConfirming] = useState(false);
  const alerts = data?.open_alerts ?? [];
  const contained = lock.data?.locked ? lock.data.containment : null;
  if (alerts.length === 0 && !contained) return null;

  const ack = async (hash: string) => {
    setBusy(hash);
    setError(undefined);
    try {
      await postJson(`/api/monitor/alerts/${hash}/ack`, { note: "Marked as handled from the console" });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(undefined);
    }
  };

  // 只有人点了按钮、再点确认才会执行；没有任何自动触发
  const contain = async () => {
    setBusy("contain");
    setError(undefined);
    try {
      await postJson("/api/lockdown/contain", { confirm: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setConfirming(false);
      setBusy(undefined);
      await lock.refresh();
    }
  };

  return (
    <div role="alert" data-testid="unauthorized-alert" className="border-b border-rose-500/60 bg-rose-600/90 text-white shadow-lg shadow-rose-900/40">
      <div className="mx-auto max-w-[1500px] space-y-2 px-6 py-3">
        {alerts.map((a) => (
          <div key={a.hash} className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="rounded bg-white px-2 py-0.5 text-xs font-bold tracking-wider text-rose-700">UNAUTHORIZED</span>
            <span>
              <b>{party(a.account, a.account_role)}</b> sent {a.tx_type === "Payment" ? a.amount ?? "a payment" : a.tx_type}
              {a.destination && <> to <span className="font-mono">{short(a.destination, 8)}</span></>} without a guard record
              {a.result && a.result !== "tesSUCCESS" && <span className="text-rose-100"> ({a.result})</span>}
            </span>
            <a href={a.explorer_url} target="_blank" rel="noreferrer" className="font-mono text-xs underline decoration-white/60 hover:decoration-white">
              {short(a.hash, 10)} ↗
            </a>
            <span className="text-xs text-rose-100">ledger {a.ledger_index} · flagged {a.flagged_at ? timeAgo(a.flagged_at) : ""}</span>
            <button
              onClick={() => ack(a.hash)}
              disabled={busy === a.hash}
              className="ml-auto rounded-md border border-white/70 px-3 py-1 text-xs font-medium hover:bg-white/15 disabled:opacity-50"
            >
              {busy === a.hash ? "Saving…" : "Mark as handled"}
            </button>
          </div>
        ))}
        {contained ? (
          <div data-testid="contained" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="rounded bg-white px-2 py-0.5 text-xs font-bold tracking-wider text-rose-700">CONTAINED</span>
            <span><b>Contained:</b> payments are paused, {contained.amount_xrp} XRP returned to the treasury</span>
            <a href={contained.explorer_url} target="_blank" rel="noreferrer" className="font-mono text-xs underline decoration-white/60 hover:decoration-white">
              {short(contained.tx_hash, 10)} ↗
            </a>
            {contained.result && contained.result !== "tesSUCCESS" && <span className="text-xs text-rose-100">({contained.result})</span>}
          </div>
        ) : confirming ? (
          <div className="flex flex-wrap items-center gap-3 rounded-lg bg-rose-900/60 px-3 py-2 text-sm">
            <span>Contain will <b>pause all payments</b> and move the hot wallet balance (minus reserve and fee) back to the treasury. Continue?</span>
            <button onClick={contain} disabled={busy === "contain"}
                    className="rounded-md bg-white px-3 py-1 text-xs font-bold text-rose-700 hover:bg-rose-50 disabled:opacity-50">
              {busy === "contain" ? "Containing…" : "Yes, contain now"}
            </button>
            <button onClick={() => setConfirming(false)} disabled={busy === "contain"} className="text-xs text-rose-100 hover:text-white">Cancel</button>
          </div>
        ) : (
          <div className="flex items-center gap-3 text-sm">
            <button onClick={() => setConfirming(true)}
                    className="rounded-md bg-white px-3 py-1 text-xs font-bold text-rose-700 hover:bg-rose-50">Contain</button>
            <span className="text-xs text-rose-100">Pause all payments and pull the hot wallet funds back to the treasury</span>
          </div>
        )}
        {error && <p className="text-xs text-rose-100">{error}</p>}
      </div>
    </div>
  );
}

/** 锁定期间所有页面顶部显示；只能在这里手动解除（记入审计）。 */
export function LockdownBar() {
  const { data, refresh } = usePoll<Lockdown>("/api/lockdown");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  if (!data?.locked) return null;

  const release = async () => {
    setBusy(true);
    setError(undefined);
    try {
      await postJson("/api/lockdown/release", { note: "Lockdown released from the console" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setConfirming(false);
      await refresh();
    }
  };

  return (
    <div data-testid="lockdown-bar" className="border-b border-amber-500/60 bg-amber-400 text-slate-950">
      <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-x-4 gap-y-2 px-6 py-2.5 text-sm">
        <span className="rounded bg-slate-950 px-2 py-0.5 text-xs font-bold tracking-wider text-amber-300">LOCKDOWN</span>
        <span><b>Payments are paused.</b> Every payment request is denied and pending approvals cannot be approved.</span>
        {data.locked_at && <span className="text-xs text-slate-800">since {timeAgo(data.locked_at)}</span>}
        {confirming ? (
          <span className="ml-auto flex items-center gap-3">
            <span className="text-xs">Release the lockdown and resume payments?</span>
            <button onClick={release} disabled={busy}
                    className="rounded-md bg-slate-950 px-3 py-1 text-xs font-semibold text-amber-300 hover:bg-slate-800 disabled:opacity-50">
              {busy ? "Releasing…" : "Yes, release"}
            </button>
            <button onClick={() => setConfirming(false)} className="text-xs text-slate-800 hover:text-slate-950">Cancel</button>
          </span>
        ) : (
          <button onClick={() => setConfirming(true)}
                  className="ml-auto rounded-md border border-slate-950/70 px-3 py-1 text-xs font-semibold hover:bg-slate-950/10">
            Release lockdown
          </button>
        )}
        {error && <span className="w-full text-xs text-rose-800">{error}</span>}
      </div>
    </div>
  );
}

const HEALTH_STYLE: Record<string, { dot: string; text: string; label: string }> = {
  connected: { dot: "bg-emerald-400 animate-pulse", text: "text-emerald-300", label: "Connected" },
  connecting: { dot: "bg-amber-400", text: "text-amber-300", label: "Connecting" },
  reconnecting: { dot: "bg-amber-400 animate-pulse", text: "text-amber-300", label: "Reconnecting" },
  disconnected: { dot: "bg-rose-400", text: "text-rose-300", label: "Disconnected" },
  stale: { dot: "bg-rose-400", text: "text-rose-300", label: "No heartbeat" },
  stopped: { dot: "bg-slate-500", text: "text-slate-400", label: "Stopped" },
  offline: { dot: "bg-slate-500", text: "text-slate-400", label: "Not running" },
};

const STATUS_STYLE: Record<MonitorTx["status"], string> = {
  matched: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  UNAUTHORIZED: "bg-rose-500/20 text-rose-200 ring-rose-500/50",
  incoming: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  pending: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
};

export function MonitorCard() {
  const { data, error } = usePoll<Monitor>("/api/monitor");
  const h = data?.health;
  const style = HEALTH_STYLE[h?.status ?? "offline"] ?? HEALTH_STYLE.offline;

  return (
    <Card
      title="Quicknode live monitor"
      right={
        <span className={`flex items-center gap-2 text-xs ${style.text}`}>
          <span className={`h-2 w-2 rounded-full ${style.dot}`} />
          {style.label}
          {h?.endpoint && <span className="text-slate-500">· {h.endpoint}</span>}
        </span>
      }
    >
      <p className="text-xs text-slate-400">
        A separate sentinel process subscribes to the treasury and hot wallet over the Quicknode WSS endpoint and reconciles every
        validated transaction against the guard's records. Outgoing transactions with no authorized record are flagged UNAUTHORIZED.
      </p>
      {error && <p className="mt-2 text-xs text-rose-300">{error}</p>}
      {h && (
        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-500">
          <span>Last ledger heartbeat: {h.heartbeat_age_seconds != null ? `${h.heartbeat_age_seconds}s ago` : "—"}</span>
          <span>Processed through ledger: {h.cursor_ledger ?? "—"}</span>
          <span>Reconnects: {h.reconnects}</span>
          {h.last_backfill && <span>Last backfill: from ledger {h.last_backfill.from_ledger}, {h.last_backfill.found} found</span>}
          {h.status !== "connected" && h.last_error && <span className="text-rose-300">Last error: {h.last_error}</span>}
        </div>
      )}
      {data && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Matched" value={String(data.counts.matched)} sub="Outgoing, has an authorized record" tone="good" />
            <Stat label="Unauthorized" value={String(data.counts.unauthorized)}
                  sub={data.open_alerts.length ? `${data.open_alerts.length} not handled yet` : "All handled"} tone={data.counts.unauthorized ? "bad" : "default"} />
            <Stat label="Incoming" value={String(data.counts.incoming)} sub="Refunds, top-ups, deposits: recorded only" />
            <Stat label="Pending" value={String(data.counts.pending)} sub={`Within the ${data.health.grace_seconds}s grace period`} tone="warn" />
          </div>
          <div className="mt-4">
            {data.recent.length === 0 ? <Empty>No transactions reconciled yet</Empty> : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                    <th className="pb-2 pr-3 font-medium">Ledger</th>
                    <th className="pb-2 pr-3 font-medium">Type</th>
                    <th className="pb-2 pr-3 font-medium">From → To</th>
                    <th className="pb-2 pr-3 font-medium text-right">Amount</th>
                    <th className="pb-2 pr-3 font-medium">Reconciliation</th>
                    <th className="pb-2 pr-3 font-medium">Seen via</th>
                    <th className="pb-2 font-medium text-right">Tx hash</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((t) => (
                    <tr key={t.hash} className={`border-t border-slate-800 ${t.status === "UNAUTHORIZED" ? "bg-rose-500/[0.06]" : ""}`}>
                      <td className="py-2 pr-3 text-xs tabular-nums text-slate-500">{t.ledger_index}</td>
                      <td className="py-2 pr-3 text-xs text-slate-300">{t.tx_type}{t.result && t.result !== "tesSUCCESS" && <span className="text-amber-300"> · {t.result}</span>}</td>
                      <td className="py-2 pr-3 text-xs text-slate-400">{party(t.account, t.account_role)} → {party(t.destination, t.destination_role)}</td>
                      <td className="py-2 pr-3 text-right text-xs tabular-nums text-slate-200">{t.amount ?? "—"}</td>
                      <td className="py-2 pr-3">
                        <span className={`inline-flex rounded-md px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${STATUS_STYLE[t.status]}`}>{t.status}</span>
                        {t.match_kind && <span className="ml-2 text-xs text-slate-500">{t.match_kind} · {t.match_ref}</span>}
                        {t.acknowledged_at && <span className="ml-2 text-xs text-slate-500">handled</span>}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-500">{t.source === "backfill" ? "account_tx backfill" : "WSS stream"}</td>
                      <td className="py-2 text-right"><TxLink hash={t.hash} url={t.explorer_url} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </Card>
  );
}
