import { useState } from "react";
import { RuleText } from "../components/rules";
import { DemoDeviceNotice } from "../components/DemoDeviceNotice";
import { postJson, usePoll, type AuditRow, type Escrow, type Treasury as TreasuryT } from "../api";
import { EscrowTimeline } from "../components/EscrowTimeline";
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
            <Stat label="今日已花费" value={`${t.budget_detail.spent_xrp ?? "0"} XRP`} sub="自动 + 人工批准，已上链" />
            <Stat label="预留中" value={`${t.budget_detail.reserved_xrp ?? "0"} XRP`} sub="已判定、付款进行中" tone="warn" />
            <Stat label="自动付款剩余" value={`${t.budget.auto_remaining_today_xrp} XRP`}
                  sub={`日预算 ${t.budget.auto_daily_budget_xrp} XRP · 单笔上限 ${t.budget.auto_limit_per_payment_xrp} XRP`} tone="good" />
            <Stat label="每日总上限" value={`${t.budget.total_committed_today_xrp} / ${t.budget.daily_total_cap_xrp}`}
                  sub={`XRP，人工批准也不能突破 · 硬上限 ${t.budget.hard_cap_per_payment_xrp} XRP/笔`} />
          </div>

          <SettingsCard />

          <HotWalletCap t={t} />

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <WalletCard title="Agent 热钱包" subtitle="小额自动付款；私钥在守卫服务端，agent 拿不到" account={t.hot_wallet} />
            <WalletCard title="金库 Treasury" subtitle="大额付款只能经人工审批 + Ledger 签名" account={t.treasury} />
          </div>

        </>
      )}

      {escrows && escrows.length > 0 && (
        <Card title={`Escrow（${escrows.length}）`}>
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {escrows.map((e) => <EscrowTimeline key={e.id} escrow={e} />)}
          </div>
        </Card>
      )}

      <Card title={`链上交易（${txs.length}）`}>
        {txs.length === 0 ? <Empty>还没有上链的付款</Empty> : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                <th className="pb-2 pr-3 font-medium">时间（UTC）</th>
                <th className="pb-2 pr-3 font-medium">资源</th>
                <th className="pb-2 pr-3 font-medium">结果</th>
                <th className="pb-2 pr-3 font-medium text-right">金额</th>
                <th className="pb-2 pr-3 font-medium">收款方</th>
                <th className="pb-2 pr-3 font-medium">付款方</th>
                <th className="pb-2 font-medium text-right">交易哈希</th>
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
        <h2 className="text-base font-semibold text-slate-100">安全边界</h2>
        <p className={`text-sm ${allOk ? "text-emerald-300" : "text-amber-300"}`}>
          {allOk ? "链上配置已到位（前提见下方）" : "还有保护没到位，见下方标黄的项目"}
        </p>
      </div>
      <DemoDeviceNotice />
      <ul className="mt-3 space-y-1.5 text-sm text-slate-300">
        <li><b className="text-slate-100">热钱包：</b>私钥在服务器上，余额封顶 {s.hot_wallet_cap_xrp} XRP，这是服务器能直接动用的上限</li>
        <li><b className="text-slate-100">金库：</b>每笔付款都需要设备签名，必须核对设备屏幕上的收款地址和金额</li>
        <li><b className="text-slate-100">Escrow：</b>锁定中的资金以锁定金额为限，放款用的原像由守卫保存</li>
        <li className="text-amber-200"><b>前提：</b>以上边界需要真实硬件设备；本演示中 Speculos 与服务器在同一台电脑上，边界是模拟的</li>
      </ul>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <Boundary ok={s.master_disabled} label="金库主密钥"
                  value={s.master_disabled ? "已禁用" : "未禁用"}
                  detail={s.master_disabled ? "服务器上的金库 seed 已失效（链上 lsfDisableMaster）" : "运行 scripts/lock_treasury.py 禁用"}
                  link={s.disable_master_tx_url ?? s.treasury_url}
                  linkText={s.disable_master_tx_url ? "禁用交易 ↗" : "金库账户 ↗"} />
        <Boundary ok={s.regular_key_is_device} label="Regular Key（唯一能签金库的钥匙）"
                  value={s.regular_key ? short(s.regular_key, 8) : "未设置"} mono
                  detail={s.regular_key_is_device ? "= Ledger 设备地址（44'/144'/0'/0/0）" : "不是当前设备"}
                  link={s.regular_key_url ?? s.treasury_url} linkText="设备账户 ↗" />
        <Boundary ok={hotOk} label="热钱包上限 / 当前余额"
                  value={`${s.hot_wallet_cap_xrp} / ${s.hot_wallet_balance_xrp?.toLocaleString(undefined, { maximumFractionDigits: 6 }) ?? "—"} XRP`}
                  detail="私钥在服务器上，余额是服务器能直接动用的上限；补款只能经审批 + 设备签名"
                  link={s.hot_wallet_url} linkText="热钱包账户 ↗" />
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
      const r = await postJson<{ approval_id: number }>("/api/hot-wallet/topup", { amount_xrp: amount, reason: "控制台发起的热钱包补款" });
      setMsg(`已生成补款审批单 #${r.approval_id}：去 Approvals 页用 Ledger 签名`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <Card title="热钱包余额上限" right={<span className="text-xs text-slate-500">服务器能直接动用的上限</span>}>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-slate-500">热钱包上限 / 当前余额</div>
          <div className={`mt-1 text-3xl font-semibold tabular-nums ${hot.within_cap ? "text-emerald-300" : "text-rose-300"}`}>
            {hot.cap_xrp} XRP / {hot.balance_xrp?.toLocaleString(undefined, { maximumFractionDigits: 6 }) ?? "—"} XRP
          </div>
          <div className="mt-2 flex gap-4 text-xs">
            <a href={hot.explorer_url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">热钱包账户 ↗</a>
            <a href={t.treasury.explorer_url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">金库账户 ↗</a>
          </div>
        </div>
        <div className="text-right">
          <div className="flex items-center gap-2">
            <input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal"
                   className="w-20 rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-right text-sm text-slate-100" />
            <span className="text-sm text-slate-400">XRP</span>
            <button onClick={topup} className="rounded-lg border border-sky-500/50 px-3 py-1.5 text-sm text-sky-300 hover:bg-sky-500/10">
              从金库补款
            </button>
          </div>
          <div className="mt-1 text-[11px] text-slate-500">只能由金库付款：人工审批 + Ledger 签名，补完不超过上限</div>
        </div>
      </div>
      <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-800">
        <div className={`h-full ${hot.within_cap ? "bg-emerald-400" : "bg-rose-400"}`} style={{ width: `${pct}%` }} />
      </div>
      {!hot.within_cap && <p className="mt-2 text-xs text-rose-300">超过上限：运行 scripts/cap_hot_wallet.py 把多出的部分转回金库</p>}
      {msg && <p className="mt-2 text-xs text-sky-300">{msg}</p>}
    </Card>
  );
}

function WalletCard({ title, subtitle, account }: { title: string; subtitle: string; account: TreasuryT["treasury"] }) {
  return (
    <Card title={title} right={<a href={account.explorer_url} target="_blank" rel="noreferrer" className="text-xs text-sky-400 hover:underline">浏览器 ↗</a>}>
      <div className="text-3xl font-semibold tabular-nums text-slate-100">
        {account.balance_xrp !== undefined ? `${account.balance_xrp.toLocaleString(undefined, { maximumFractionDigits: 6 })} XRP` : "—"}
      </div>
      <div className="mt-1 break-all font-mono text-xs text-slate-500">{account.address}</div>
      <p className="mt-3 text-xs text-slate-400">{subtitle}</p>
      {account.error && <p className="mt-2 text-xs text-rose-300">{account.error}</p>}
    </Card>
  );
}
