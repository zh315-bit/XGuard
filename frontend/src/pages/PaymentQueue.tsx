import { useState } from "react";
import { RuleText } from "../components/rules";
import { usePoll, type Payment } from "../api";
import { Empty, ErrorLine, RiskBlock, StateText, TxLink, VerdictBadge, short, timeAgo } from "../components/ui";
import {
  DayGroup, DetailLayout, ExpandRow, KV, MethodIcon, MiniSteps, Tabs, flowSteps, groupByDay, isTerminal, methodLabel, resourceLabel,
} from "../components/queue";

type Tab = "queue" | "history";
const FILTERS = [
  { id: "ALL", label: "全部", tone: "text-slate-200" },
  { id: "AUTO", label: "自动付款", tone: "text-emerald-300" },
  { id: "HOLD", label: "待人工确认", tone: "text-amber-300" },
  { id: "DENY", label: "已拒绝", tone: "text-rose-300" },
] as const;
type Filter = (typeof FILTERS)[number]["id"];

export function PaymentQueue() {
  const { data, error } = usePoll<Payment[]>("/payments");
  const [tab, setTab] = useState<Tab>("queue");
  const [filter, setFilter] = useState<Filter>("ALL");
  const [open, setOpen] = useState<number>();
  const rows = data ?? [];
  const queued = rows.filter((p) => !isTerminal(p.state));
  const history = rows.filter((p) => isTerminal(p.state));
  const inTab = tab === "queue" ? queued : history;
  const list = inTab.filter((p) => filter === "ALL" || p.verdict === filter);
  const count = (f: Filter) => (f === "ALL" ? inTab.length : inTab.filter((p) => p.verdict === f).length);

  return (
    <div className="space-y-6">
      <ErrorLine error={error} />
      <Tabs
        tabs={[{ id: "queue", label: "进行中", count: queued.length }, { id: "history", label: "历史", count: history.length }]}
        value={tab}
        onChange={(t) => { setTab(t); setOpen(undefined); }}
        right={
          <div className="flex rounded-lg border border-slate-800 bg-slate-900/70 p-0.5 text-xs">
            {FILTERS.map((f) => (
              <button key={f.id} onClick={() => setFilter(f.id)}
                      className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 transition ${filter === f.id ? "bg-slate-800 text-slate-50" : "text-slate-400 hover:text-slate-200"}`}>
                {f.label}
                <span className={`tabular-nums ${filter === f.id ? f.tone : "text-slate-500"}`}>{count(f.id)}</span>
              </button>
            ))}
          </div>
        }
      />

      {list.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800">
          <Empty>
            {rows.length === 0 ? "还没有付款请求"
              : tab === "queue" ? <>没有进行中的付款。<button onClick={() => setTab("history")} className="text-sky-400 hover:underline">查看历史</button></>
              : "没有符合条件的记录"}
          </Empty>
        </div>
      ) : (
        <div className="space-y-6">
          {groupByDay(list, (p) => p.created_at).map((g) => (
            <DayGroup key={g.label} label={g.label}>
              {g.items.map((p) => (
                <PaymentRow key={p.id} payment={p} open={open === p.id} onToggle={() => setOpen(open === p.id ? undefined : p.id)} />
              ))}
            </DayGroup>
          ))}
        </div>
      )}
    </div>
  );
}

function PaymentRow({ payment: p, open, onToggle }: { payment: Payment; open: boolean; onToggle: () => void }) {
  const steps = flowSteps(
    { state: p.state, verdict: p.verdict, method: p.method, rule_id: p.rule_id, hasQuote: !!p.quote, txHash: p.tx_hash },
    {
      quote: p.quote && `${p.quote.amount_xrp} ${p.quote.asset}`,
      evaluate: <RuleText rule={p.rule_id} className="text-xs text-slate-500" />,
      approval: p.approval_id && `审批单 #${p.approval_id}`,
      chain: p.tx_hash && <TxLink hash={p.tx_hash} url={p.explorer_url} />,
      lock: p.tx_hash && <TxLink hash={p.tx_hash} url={p.explorer_url} />,
    },
  );
  const waiting = p.state === "PENDING_APPROVAL";

  return (
    <ExpandRow open={open} onToggle={onToggle} accent={waiting ? "border-l-amber-400" : undefined} summary={
      <div className="grid grid-cols-[2.5rem_minmax(0,1fr)_7.5rem_5.5rem_6.5rem_11rem] items-center gap-4">
        <span className="font-mono text-xs text-slate-500">#{p.id}</span>
        <div className="flex min-w-0 items-center gap-3">
          <MethodIcon method={p.method} verdict={p.verdict} />
          <div className="min-w-0">
            <div className="truncate font-mono text-xs text-slate-200" title={p.url}>{resourceLabel(p.url)}</div>
            <div className="truncate text-xs text-slate-500" title={p.purpose}>{methodLabel(p.method)} · {p.purpose || p.request_id}</div>
          </div>
        </div>
        <div className="text-right">
          <div className="text-sm tabular-nums text-slate-100">{p.quote ? `${p.quote.amount_xrp} ${p.quote.asset}` : "—"}</div>
          {p.quote && <div className="font-mono text-[11px] text-slate-500">→ {short(p.quote.pay_to, 4)}</div>}
        </div>
        <span className="text-xs text-slate-500">{timeAgo(p.created_at)}</span>
        <span><VerdictBadge verdict={p.verdict} /></span>
        <div className="flex items-center justify-between gap-2">
          <div className="space-y-1">
            <MiniSteps steps={steps} />
            <StateText state={p.state} />
          </div>
          {waiting && (
            <button onClick={(e) => { e.stopPropagation(); location.hash = "approvals"; }}
                    className="shrink-0 rounded-md bg-amber-500 px-2.5 py-1 text-xs font-semibold text-slate-950 hover:bg-amber-400">
              去审批
            </button>
          )}
        </div>
      </div>
    }>
      <DetailLayout steps={steps}>
        <dl className="grid gap-x-8 gap-y-2 xl:grid-cols-2">
          <KV label="资源" mono>{p.url}</KV>
          <KV label="用途">{p.purpose}</KV>
          <KV label="请求编号" mono>{p.request_id}</KV>
          <KV label="Agent" mono>{p.agent_id}</KV>
          <KV label="收款地址" mono>{p.quote?.pay_to}</KV>
          <KV label="交易">{p.tx_hash && <TxLink hash={p.tx_hash} url={p.explorer_url} />}</KV>
        </dl>
        <div className="rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <VerdictBadge verdict={p.verdict} />
            <RuleText rule={p.rule_id} className="text-sm text-slate-200" />
          </div>
          {p.reason && <p className="mt-1 text-xs text-slate-500">{p.reason}</p>}
        </div>
        {p.risk ? <RiskBlock risk={p.risk} /> : <p className="text-xs text-slate-500">自动付款没有风险解释（只有 HOLD / DENY 才生成）</p>}
        {p.error && <p className="rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{p.error}</p>}
      </DetailLayout>
    </ExpandRow>
  );
}
