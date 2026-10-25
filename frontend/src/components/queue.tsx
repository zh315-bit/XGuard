/** 交易队列的共享部件（参考 Safe{Wallet} 的 Queue / History）：分栏、按日期分组、可展开行、处理进度时间线。 */
import type { KeyboardEvent, ReactNode } from "react";

// ---- 处理进度：由支付状态机的当前状态推出每一步 ----

export type StepStatus = "done" | "current" | "warn" | "failed" | "todo";
export type Step = { id: string; label: string; status: StepStatus; detail?: ReactNode };

const TERMINAL = new Set(["DENIED", "REJECTED", "EXPIRED", "FAILED", "RESOURCE_DELIVERED", "REFUNDED"]);
export const isTerminal = (state?: string) => !!state && TERMINAL.has(state);

type Flow = {
  state?: string;
  verdict?: string;
  method?: string;
  rule_id?: string;
  hasQuote: boolean;
  txHash?: string;
};

/** 每笔付款要走的步骤，以及现在走到哪一步。details 按步骤 id 补充说明（交易链接、审批单号等）。 */
export function flowSteps(f: Flow, details: Partial<Record<string, ReactNode>> = {}): Step[] {
  const escrow = f.method === "escrow";
  const topup = f.method === "topup";
  if (f.rule_id === "PROBE") {
    return [
      { id: "request", label: "Request", status: "done" },
      { id: "probe", label: "Fetching the resource failed; nothing paid", status: "failed" },
    ];
  }
  if (f.verdict === "FREE" || (f.state === "RESOURCE_DELIVERED" && !f.hasQuote)) {
    return [
      { id: "request", label: "Request", status: "done" },
      { id: "deliver", label: "Free resource, data received", status: "done" },
    ];
  }
  const hold = f.verdict === "HOLD" || topup;
  const refunded = f.state === "REFUND_PENDING" || f.state === "REFUNDED";
  const labels: Record<string, string> = {
    quote: topup ? "Top-up requested" : "Quote received",
    evaluate: "Policy check",
    approval: "Human approval",
    sign: hold ? "Ledger signature" : "Hot wallet auto-sign",
    // x402 付款由商户（facilitator）提交上链、等验证后交货；补款是金库自己提交
    chain: topup ? "On-chain confirmation" : "Merchant settles on chain",
    deliver: topup ? "Top-up received" : "Paid, data received",
    lock: "Lock on chain (EscrowCreate)",
    verify: "Receive and verify",
    release: f.state === "REFUNDED" ? "Verification failed, refunded to treasury" : refunded ? "Verification failed, refund pending" : "Verified, released to merchant",
  };
  const ids = ["quote", "evaluate", ...(hold ? ["approval"] : []), "sign",
               ...(escrow ? ["lock", "verify", "release"] : ["chain", "deliver"])];
  const chain = escrow ? "lock" : "chain";

  const at: Record<string, [string, StepStatus]> = {
    RECEIVED: ["quote", "current"],
    QUOTED: ["evaluate", "current"],
    EVALUATED: ["evaluate", "current"],
    PENDING_APPROVAL: ["approval", "current"],
    RESERVED: ["sign", "current"],
    SIGNING: ["sign", "current"],
    SUBMITTED: [chain, "current"],
    UNKNOWN_SETTLEMENT: [chain, "warn"],
    VALIDATED: ["deliver", "current"],
    ESCROW_LOCKED: ["verify", "current"],
    DELIVERY_RECEIVED: ["verify", "current"],
    REFUND_PENDING: ["release", "warn"],
    REFUNDED: ["release", "failed"],
    // R8：付款前重新拿报价发现被调包，拦在签名这一步
    DENIED: [f.rule_id === "R8" ? "sign" : "evaluate", "failed"],
    REJECTED: ["approval", "failed"],
    EXPIRED: ["approval", "failed"],
    // FAILED 可能发生在多个阶段，按已有信息推断
    FAILED: [!f.hasQuote ? "quote" : f.txHash ? chain : "sign", "failed"],
    RESOURCE_DELIVERED: [ids[ids.length - 1], "done"],
  };
  const [currentId, currentStatus] = at[f.state ?? ""] ?? ["quote", "current"];
  const idx = Math.max(0, ids.indexOf(currentId));
  return ids.map((id, i) => ({
    id,
    label: labels[id],
    status: i < idx ? "done" : i === idx ? currentStatus : "todo",
    detail: details[id],
  }));
}

const DOT: Record<StepStatus, string> = {
  done: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
  current: "bg-sky-500/15 text-sky-300 ring-sky-400/60",
  warn: "bg-amber-500/15 text-amber-300 ring-amber-500/50",
  failed: "bg-rose-500/15 text-rose-300 ring-rose-500/50",
  todo: "bg-slate-900 text-slate-600 ring-slate-700",
};

/** 走到哪一步，哪一步的圆圈就跳动；波纹颜色跟状态走 */
const PING: Partial<Record<StepStatus, string>> = { current: "bg-sky-400/50", warn: "bg-amber-400/50", failed: "bg-rose-400/50" };

const LABEL: Record<StepStatus, string> = {
  done: "text-slate-200", current: "text-sky-200 font-medium", warn: "text-amber-200 font-medium",
  failed: "text-rose-200 font-medium", todo: "text-slate-500",
};

/** 竖向时间线（Safe 展开交易后右侧的 Created / Confirmations / Execution）。 */
export function StepTimeline({ steps }: { steps: Step[] }) {
  return (
    <ol>
      {steps.map((s, i) => (
        <li key={s.id} className="relative flex gap-3 pb-4 last:pb-0">
          {i < steps.length - 1 && (
            <span className={`absolute left-[11px] top-6 bottom-0 w-px ${s.status === "done" ? "bg-emerald-500/40" : "bg-slate-800"}`} />
          )}
          <span className="relative z-10 flex h-6 w-6 shrink-0">
            {/* 当前所在的那一步：圆圈跳动，外面一圈波纹扩散 */}
            {s.status in PING && <span className={`step-ping absolute inset-0 rounded-full ${PING[s.status]}`} />}
            {/* 外层实心底，挡住后面的波纹；内层叠状态色 */}
            <span className={`relative h-6 w-6 rounded-full bg-slate-900 ${s.status in PING ? "step-hop" : ""}`}>
              <span className={`flex h-full w-full items-center justify-center rounded-full ring-1 ring-inset ${DOT[s.status]}`}>
                {s.status === "done" && <Icon d={ICONS.check} className="h-3.5 w-3.5" />}
                {s.status === "failed" && <Icon d={ICONS.x} className="h-3.5 w-3.5" />}
                {s.status === "warn" && <span className="text-xs font-bold">!</span>}
                {s.status === "current" && <span className="h-2 w-2 rounded-full bg-sky-400" />}
                {s.status === "todo" && <span className="h-1.5 w-1.5 rounded-full bg-slate-700" />}
              </span>
            </span>
          </span>
          <div className="min-w-0 pt-0.5">
            <div className={`text-sm ${LABEL[s.status]}`}>{s.label}</div>
            {s.detail && <div className="mt-0.5 text-xs text-slate-500">{s.detail}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}

const BAR: Record<StepStatus, string> = {
  done: "bg-emerald-400", current: "bg-sky-400 animate-pulse", warn: "bg-amber-400", failed: "bg-rose-400", todo: "bg-slate-700",
};

/** 行内的小进度条（Safe 行里的 “2 of 3” 确认数）。 */
export function MiniSteps({ steps }: { steps: Step[] }) {
  const done = steps.filter((s) => s.status === "done").length;
  return (
    <div className="flex items-center gap-2" title={steps.map((s) => `${s.label}：${s.status}`).join("\n")}>
      <div className="flex gap-0.5">
        {steps.map((s) => <span key={s.id} className={`h-1.5 w-3 rounded-full ${BAR[s.status]}`} />)}
      </div>
      <span className="text-[11px] tabular-nums text-slate-500">{done}/{steps.length}</span>
    </div>
  );
}

// ---- 布局 ----

export function Tabs<T extends string>({ tabs, value, onChange, right }: {
  tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (id: T) => void; right?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 border-b border-slate-800">
      <div className="flex gap-6">
        {tabs.map((t) => (
          <button key={t.id} onClick={() => onChange(t.id)}
                  className={`-mb-px flex items-center gap-2 border-b-2 pb-3 text-sm transition ${value === t.id ? "border-sky-400 text-slate-50" : "border-transparent text-slate-400 hover:text-slate-200"}`}>
            {t.label}
            {t.count !== undefined && (
              <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${value === t.id ? "bg-sky-500/20 text-sky-200" : "bg-slate-800 text-slate-400"}`}>{t.count}</span>
            )}
          </button>
        ))}
      </div>
      {right && <div className="pb-2">{right}</div>}
    </div>
  );
}

/** 按日期分组（今天 / 昨天 / 10月22日），ts 是 Unix 秒。保持传入顺序。 */
export function groupByDay<T>(items: T[], ts: (item: T) => number): { label: string; items: T[] }[] {
  const groups: { label: string; items: T[] }[] = [];
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  for (const item of items) {
    const d = new Date(ts(item) * 1000);
    d.setHours(0, 0, 0, 0);
    const days = Math.round((today.getTime() - d.getTime()) / 86_400_000);
    const label = days === 0 ? "Today" : days === 1 ? "Yesterday" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    const last = groups[groups.length - 1];
    if (last?.label === label) last.items.push(item);
    else groups.push({ label, items: [item] });
  }
  return groups;
}

export function DayGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">{label}</h3>
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/70 backdrop-blur-md">{children}</div>
    </section>
  );
}

/** 可展开的一行。收起时内容只是隐藏、不卸载，所以签名中的状态不会因为收起而丢失。 */
export function ExpandRow({ summary, open, onToggle, accent, children }: {
  summary: ReactNode; open: boolean; onToggle: () => void; accent?: string; children: ReactNode;
}) {
  const onKey = (e: KeyboardEvent) => {
    if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      onToggle();
    }
  };
  return (
    <div className={`border-t border-slate-800 first:border-t-0 ${accent ? `border-l-2 ${accent}` : ""}`}>
      <div role="button" tabIndex={0} aria-expanded={open} onClick={onToggle} onKeyDown={onKey}
           className={`flex cursor-pointer items-center gap-4 px-4 py-3 transition hover:bg-slate-800/40 ${open ? "bg-slate-800/30" : ""}`}>
        <div className="min-w-0 flex-1">{summary}</div>
        <Icon d={ICONS.chevron} className={`h-4 w-4 shrink-0 text-slate-500 transition ${open ? "rotate-180" : ""}`} />
      </div>
      <div className={open ? "border-t border-slate-800/70 bg-slate-950/30 px-5 py-5" : "hidden"}>{children}</div>
    </div>
  );
}

/** 展开内容的两栏：左边详情，右边处理进度。 */
export function DetailLayout({ steps, children }: { steps: Step[]; children: ReactNode }) {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_260px]">
      <div className="min-w-0 space-y-4">{children}</div>
      <div className="lg:border-l lg:border-slate-800 lg:pl-6">
        <h4 className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500">Progress</h4>
        <StepTimeline steps={steps} />
      </div>
    </div>
  );
}

export function KV({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  if (children === undefined || children === null || children === "") return null;
  return (
    <div className="flex gap-3 text-sm">
      <dt className="w-20 shrink-0 text-slate-500">{label}</dt>
      <dd className={`min-w-0 break-all text-slate-200 ${mono ? "font-mono text-xs leading-5" : ""}`}>{children}</dd>
    </div>
  );
}

// ---- 图标 ----

export const ICONS = {
  send: "M4.5 19.5l15-15m0 0H8.25m11.25 0v11.25",
  lock: "M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z",
  topup: "M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5",
  deny: "M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636",
  check: "M4.5 12.75l6 6 9-13.5",
  x: "M6 18L18 6M6 6l12 12",
  chevron: "M19.5 8.25l-7.5 7.5-7.5-7.5",
};

export function Icon({ d, className = "h-4 w-4" }: { d: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d={d} />
    </svg>
  );
}

const TONE: Record<string, string> = {
  AUTO: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30",
  HOLD: "bg-amber-500/10 text-amber-300 ring-amber-500/30",
  DENY: "bg-rose-500/10 text-rose-300 ring-rose-500/30",
};

/** 行首的类型图标：普通付款 / 托管锁款 / 金库补款 / 拒绝。颜色跟判定结果走。 */
export function MethodIcon({ method, verdict }: { method?: string; verdict?: string }) {
  const d = verdict === "DENY" ? ICONS.deny : method === "escrow" ? ICONS.lock : method === "topup" ? ICONS.topup : ICONS.send;
  const title = verdict === "DENY" ? "Denied" : method === "escrow" ? "Escrow payment (verify before release)" : method === "topup" ? "Treasury → hot wallet top-up" : "x402 payment";
  return (
    <span title={title} className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ring-1 ring-inset ${TONE[verdict ?? ""] ?? "bg-slate-800 text-slate-300 ring-slate-700"}`}>
      <Icon d={d} />
    </span>
  );
}

export function methodLabel(method?: string) {
  return method === "escrow" ? "Escrow payment" : method === "topup" ? "Treasury top-up" : "x402 payment";
}

/** 资源 URL 的简短显示：http 取路径，其余（如 xrpl://treasury/...）原样。 */
export function resourceLabel(url: string) {
  try {
    const u = new URL(url);
    return u.protocol.startsWith("http") ? `${u.host}${u.pathname}` : url;
  } catch {
    return url;
  }
}
