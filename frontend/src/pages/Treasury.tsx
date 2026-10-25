import { useState } from "react";
import { RuleText } from "../components/rules";
import { DemoDeviceNotice } from "../components/DemoDeviceNotice";
import { postJson, usePoll, type AuditRow, type Escrow, type Treasury as TreasuryT } from "../api";
import { EscrowTimeline } from "../components/EscrowTimeline";
import { MonitorCard } from "../components/Monitor";
import { SettingsCard } from "../components/SettingsCard";
import { Card, Empty, ErrorLine, Stat, TxLink, VerdictBadge, short } from "../components/ui";

export function Treasury() {
  const { data: t, error } = usePoll<TreasuryT>("/api/treasury");
  const { data: audit } = usePoll<AuditRow[]>("/audit?limit=100");
  const { data: escrows } = usePoll<Escrow[]>("/api/escrows");
  const txs = (audit ?? []).filter((d) => d.tx_hash);

  return (
    <div className="space-y-6">
      <ErrorLine error={error} />
      {t && (
        <>
          <SecurityBoundary t={t} />

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat label="Spent today" value={`${t.budget_detail.spent_xrp ?? "0"} XRP`} sub="Auto + human-approved, on chain" />
            <Stat label="Reserved" value={`${t.budget_detail.reserved_xrp ?? "0"} XRP`} sub="Evaluated, payment in progress" tone="warn" />
            <Stat label="Auto budget left" value={`${t.budget.auto_remaining_today_xrp} XRP`}
                  sub={`Daily budget ${t.budget.auto_daily_budget_xrp} XRP · per payment ${t.budget.auto_limit_per_payment_xrp} XRP`} tone="good" />
            <Stat label="Daily total cap" value={`${t.budget.total_committed_today_xrp} / ${t.budget.daily_total_cap_xrp}`}
                  sub={`XRP, cannot be exceeded even with approval · hard cap ${t.budget.hard_cap_per_payment_xrp} XRP/payment`} />
          </div>

          <SettingsCard />

          <HotWalletCap t={t} />

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <WalletCard title="Agent hot wallet" subtitle="Small automatic payments; the key is on the guard server, out of the agent's reach" account={t.hot_wallet} />
            <WalletCard title="Treasury" subtitle="Large payments only with human approval + Ledger signature" account={t.treasury} />
          </div>

        </>
      )}

      <MonitorCard />

      {escrows && escrows.length > 0 && (
        <Card title={`Escrow（${escrows.length}）`}>
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {escrows.map((e) => <EscrowTimeline key={e.id} escrow={e} />)}
          </div>
        </Card>
      )}

      <Card title={`On-chain transactions (${txs.length})`}>
        {txs.length === 0 ? <Empty>No on-chain payments yet</Empty> : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                <th className="pb-2 pr-3 font-medium">Time (UTC)</th>
                <th className="pb-2 pr-3 font-medium">Resource</th>
                <th className="pb-2 pr-3 font-medium">Result</th>
                <th className="pb-2 pr-3 font-medium text-right">Amount</th>
                <th className="pb-2 pr-3 font-medium">Payee</th>
                <th className="pb-2 pr-3 font-medium">Payer</th>
                <th className="pb-2 font-medium text-right">Tx hash</th>
              </tr>
            </thead>
            <tbody>
              {txs.map((d) => (
                <tr key={d.id} className="border-t border-slate-800">
                  <td className="py-2 pr-3 text-xs text-slate-500">{d.created_at.slice(11, 19)}</td>
                  <td className="py-2 pr-3 font-mono text-xs text-slate-300">{new URL(d.url).pathname}</td>
                  <td className="py-2 pr-3"><VerdictBadge verdict={d.verdict} /> <RuleText rule={d.rule_id} className="text-xs text-slate-500" /></td>
                  <td className="py-2 pr-3 text-right tabular-nums text-slate-200">{d.amount_xrp} XRP</td>
                  <td className="py-2 pr-3 font-mono text-xs text-slate-400">{short(d.pay_to)}</td>
                  <td className="py-2 pr-3 text-xs text-slate-400">{d.payer ?? "—"}</td>
                  <td className="py-2 text-right"><TxLink hash={d.tx_hash} url={d.explorer_url} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

function SecurityBoundary({ t }: { t: TreasuryT }) {
  const s = t.security;
  const hotOk = s.hot_wallet_balance_xrp !== undefined && s.hot_wallet_balance_xrp <= s.hot_wallet_cap_xrp;
  const allOk = s.master_disabled && s.regular_key_is_device && hotOk;
  return (
    <section className={`rounded-xl border p-5 ${allOk ? "border-emerald-500/40 bg-emerald-500/[0.04]" : "border-amber-500/40 bg-amber-500/[0.04]"}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold text-slate-100">Security boundaries</h2>
        <p className={`text-sm ${allOk ? "text-emerald-300" : "text-amber-300"}`}>
          {allOk ? "On-chain configuration in place (see the precondition below)" : "Some protections are missing; see the highlighted items below"}
        </p>
      </div>
      <DemoDeviceNotice />
      <ul className="mt-3 space-y-1.5 text-sm text-slate-300">
        <li><b className="text-slate-100">Hot wallet: </b>the key is on the server and the balance is capped at {s.hot_wallet_cap_xrp} XRP, the most the server can spend directly</li>
        <li><b className="text-slate-100">Treasury: </b>every payment needs a device signature; the payee and amount must be checked on the device screen</li>
        <li><b className="text-slate-100">Escrow: </b>locked funds are limited to the locked amount; the guard holds the release preimage</li>
        <li className="text-amber-200"><b>Precondition: </b>these boundaries need a real hardware device; in this demo Speculos runs on the same computer as the server, so they are simulated</li>
      </ul>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <Boundary ok={s.master_disabled} label="Treasury master key"
                  value={s.master_disabled ? "Disabled" : "Not disabled"}
                  detail={s.master_disabled ? "The treasury seed on the server no longer works (lsfDisableMaster on chain)" : "Run scripts/lock_treasury.py to disable it"}
                  link={s.disable_master_tx_url ?? s.treasury_url}
                  linkText={s.disable_master_tx_url ? "Disable tx ↗" : "Treasury account ↗"} />
        <Boundary ok={s.regular_key_is_device} label="Regular Key (the only key that can sign for the treasury)"
                  value={s.regular_key ? short(s.regular_key, 8) : "Not set"} mono
                  detail={s.regular_key_is_device ? "= Ledger device address (44'/144'/0'/0/0)" : "Not the current device"}
                  link={s.regular_key_url ?? s.treasury_url} linkText="Device account ↗" />
        <Boundary ok={hotOk} label="Hot wallet cap / balance"
                  value={`${s.hot_wallet_cap_xrp} / ${s.hot_wallet_balance_xrp?.toLocaleString(undefined, { maximumFractionDigits: 6 }) ?? "—"} XRP`}
                  detail="The key is on the server, so the balance is the most it can spend directly; top-ups need approval + a device signature"
                  link={s.hot_wallet_url} linkText="Hot wallet account ↗" />
      </div>
    </section>
  );
}

function Boundary({ ok, label, value, detail, link, linkText, mono }: {
  ok: boolean; label: string; value: string; detail: string; link: string; linkText: string; mono?: boolean;
}) {
  return (
    <div className={`rounded-lg border px-4 py-3 ${ok ? "border-emerald-500/30 bg-slate-950/40" : "border-amber-500/40 bg-slate-950/40"}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-slate-400">{label}</span>
        <span className={`text-xs ${ok ? "text-emerald-300" : "text-amber-300"}`}>{ok ? "✓" : "!"}</span>
      </div>
      <div className={`mt-1 text-lg font-semibold ${ok ? "text-slate-100" : "text-amber-200"} ${mono ? "font-mono text-base" : ""}`}>{value}</div>
      <div className="mt-1 text-xs text-slate-500">{detail}</div>
      <a href={link} target="_blank" rel="noreferrer" className="mt-2 inline-block text-xs text-sky-400 hover:underline">{linkText}</a>
    </div>
  );
}

function HotWalletCap({ t }: { t: TreasuryT }) {
  const hot = t.hot_wallet;
  const pct = hot.balance_xrp !== undefined ? Math.min(100, (hot.balance_xrp / hot.cap_xrp) * 100) : 0;
  const [amount, setAmount] = useState("5");
  const [msg, setMsg] = useState<string>();
  const topup = async () => {
    setMsg(undefined);
    try {
      const r = await postJson<{ approval_id: number }>("/api/hot-wallet/topup", { amount_xrp: amount, reason: "Hot wallet top-up from the console" });
      setMsg(`Created top-up approval #${r.approval_id}: sign it with the Ledger on the Approvals page`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <Card title="Hot wallet balance cap" right={<span className="text-xs text-slate-500">The most the server can spend directly</span>}>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-slate-500">Hot wallet cap / balance</div>
          <div className={`mt-1 text-3xl font-semibold tabular-nums ${hot.within_cap ? "text-emerald-300" : "text-rose-300"}`}>
            {hot.cap_xrp} XRP / {hot.balance_xrp?.toLocaleString(undefined, { maximumFractionDigits: 6 }) ?? "—"} XRP
          </div>
          <div className="mt-2 flex gap-4 text-xs">
            <a href={hot.explorer_url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">Hot wallet account ↗</a>
            <a href={t.treasury.explorer_url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">Treasury account ↗</a>
          </div>
        </div>
        <div className="text-right">
          <div className="flex items-center gap-2">
            <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal"
                   className="w-20 rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-right text-sm text-slate-100" />
            <span className="text-sm text-slate-400">XRP</span>
            <button onClick={topup} className="rounded-lg border border-sky-500/50 px-3 py-1.5 text-sm text-sky-300 hover:bg-sky-500/10">
              Top up from treasury
            </button>
          </div>
          <div className="mt-1 text-[11px] text-slate-500">Paid by the treasury only: human approval + Ledger signature, never above the cap</div>
        </div>
      </div>
      <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-800">
        <div className={`h-full ${hot.within_cap ? "bg-emerald-400" : "bg-rose-400"}`} style={{ width: `${pct}%` }} />
      </div>
      {!hot.within_cap && <p className="mt-2 text-xs text-rose-300">Above the cap: run scripts/cap_hot_wallet.py to send the excess back to the treasury</p>}
      {msg && <p className="mt-2 text-xs text-sky-300">{msg}</p>}
    </Card>
  );
}

function WalletCard({ title, subtitle, account }: { title: string; subtitle: string; account: TreasuryT["treasury"] }) {
  return (
    <Card title={title} right={<a href={account.explorer_url} target="_blank" rel="noreferrer" className="text-xs text-sky-400 hover:underline">Explorer ↗</a>}>
      <div className="text-3xl font-semibold tabular-nums text-slate-100">
        {account.balance_xrp !== undefined ? `${account.balance_xrp.toLocaleString(undefined, { maximumFractionDigits: 6 })} XRP` : "—"}
      </div>
      <div className="mt-1 break-all font-mono text-xs text-slate-500">{account.address}</div>
      <p className="mt-3 text-xs text-slate-400">{subtitle}</p>
      {account.error && <p className="mt-2 text-xs text-rose-300">{account.error}</p>}
    </Card>
  );
}
