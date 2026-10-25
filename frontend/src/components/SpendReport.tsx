import { usePoll } from "../api";

type Report = {
  day: string; spent_xrp: string; purchased: number; auto_paid: number; approved: number;
  blocked: number; blocked_by_policy: number; rejected_by_human: number;
  escrow_refunded_xrp: string; escrow_refunded_count: number;
};

/** Today's spend report：大数字，适合投屏。放在首页左栏，三列排布。 */
export function SpendReport() {
  const { data: r } = usePoll<Report>("/api/report/today", 3000);
  const items: { label: string; value: string | number; unit?: string; sub?: string; tone: string }[] = r ? [
    { label: "Spent today", value: r.spent_xrp, unit: "XRP", tone: "text-slate-50" },
    { label: "Purchased", value: r.purchased, unit: "", tone: "text-sky-300" },
    { label: "Auto-paid", value: r.auto_paid, unit: "", tone: "text-emerald-300" },
    { label: "Approved", value: r.approved, unit: "", tone: "text-amber-300" },
    { label: "Blocked", value: r.blocked, unit: "", tone: "text-rose-300",
      sub: `By policy ${r.blocked_by_policy} · by humans ${r.rejected_by_human}` },
    { label: "Refunded", value: r.escrow_refunded_xrp, unit: "XRP", tone: "text-violet-300",
      sub: `${r.escrow_refunded_count} escrow refund(s)` },
  ] : [];
  return (
    <section className="rounded-xl border border-slate-800 bg-gradient-to-br from-slate-900/80 to-slate-950/80 p-5 backdrop-blur-md">
      <div className="mb-4 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">Today's spend report</h2>
        <span className="text-xs text-slate-500">{r ? `${r.day} (UTC)` : "Loading…"}</span>
      </div>
      <div className="grid grid-cols-3 gap-2.5">
        {items.map((i) => (
          <div key={i.label} className="rounded-lg bg-slate-950/60 px-3 py-3">
            <div className="truncate text-xs text-slate-400">{i.label}</div>
            <div className={`mt-1 text-2xl font-bold tabular-nums ${i.tone}`}>
              {i.value}<span className="ml-1 text-xs font-medium text-slate-500">{i.unit}</span>
            </div>
            {i.sub && <div className="mt-1 text-[11px] leading-4 text-slate-500">{i.sub}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}
