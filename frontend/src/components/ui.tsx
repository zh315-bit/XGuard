import type { ReactNode } from "react";
import { VERDICT_TEXT } from "./rules";

export function Card({ title, right, children, className = "" }: {
  title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`rounded-xl border border-slate-800 bg-slate-900/70 backdrop-blur-md ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-slate-800 px-5 py-3">
          <h2 className="text-sm font-semibold tracking-wide text-slate-200">{title}</h2>
          {right}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

const VERDICT_STYLE: Record<string, string> = {
  AUTO: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  HOLD: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  DENY: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  FREE: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
  ERROR: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
};

export function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <span title={verdict} className={`inline-flex items-center whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${VERDICT_STYLE[verdict] ?? VERDICT_STYLE.FREE}`}>
      {VERDICT_TEXT[verdict] ?? verdict}
    </span>
  );
}

const STATE_STYLE: Record<string, string> = {
  RESOURCE_DELIVERED: "text-emerald-300",
  VALIDATED: "text-emerald-300",
  PENDING_APPROVAL: "text-amber-300",
  SIGNING: "text-sky-300",
  SUBMITTED: "text-sky-300",
  RESERVED: "text-sky-300",
  UNKNOWN_SETTLEMENT: "text-orange-300",
  DENIED: "text-rose-300",
  REJECTED: "text-rose-300",
  EXPIRED: "text-slate-400",
  FAILED: "text-rose-300",
};

const STATE_TEXT: Record<string, string> = {
  RECEIVED: "Received", QUOTED: "Quoted", EVALUATED: "Evaluated", DENIED: "Denied, not paid",
  PENDING_APPROVAL: "Awaiting approval", RESERVED: "Budget reserved", SIGNING: "Signing", SUBMITTED: "Submitted",
  VALIDATED: "Validated on chain", RESOURCE_DELIVERED: "Paid, data received", EXPIRED: "Approval expired, not paid",
  REJECTED: "Rejected, not paid", FAILED: "Failed", UNKNOWN_SETTLEMENT: "Settlement being reconciled",
  ESCROW_LOCKED: "Locked in escrow", DELIVERY_RECEIVED: "Delivered, verifying", REFUND_PENDING: "Verification failed, refund pending",
  REFUNDED: "Refunded",
};

export function StateText({ state }: { state?: string }) {
  if (!state) return null;
  return <span title={state} className={`text-xs ${STATE_STYLE[state] ?? "text-slate-400"}`}>{STATE_TEXT[state] ?? state}</span>;
}

export function Stat({ label, value, sub, tone = "default" }: {
  label: string; value: ReactNode; sub?: ReactNode; tone?: "default" | "good" | "warn" | "bad";
}) {
  const color = { default: "text-slate-100", good: "text-emerald-300", warn: "text-amber-300", bad: "text-rose-300" }[tone];
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/60 px-4 py-3">
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-10 text-center text-sm text-slate-500">{children}</div>;
}

export function ErrorLine({ error }: { error?: string }) {
  if (!error) return null;
  return <div className="rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-300">Backend unreachable: {error}</div>;
}

export function short(address?: string, n = 6) {
  if (!address) return "";
  return address.length > n * 2 + 1 ? `${address.slice(0, n)}…${address.slice(-n)}` : address;
}

export function TxLink({ hash, url }: { hash?: string; url?: string }) {
  if (!hash) return <span className="text-slate-600">—</span>;
  return (
    <a href={url} target="_blank" rel="noreferrer" className="font-mono text-xs text-sky-400 hover:underline">
      {short(hash, 8)} ↗
    </a>
  );
}

export function RiskBlock({ risk }: { risk?: { risk_summary: string; supporting_facts: string[]; suggested_review_questions: string[]; source: string } | null }) {
  if (!risk) return null;
  return (
    <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-950/50 p-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-slate-300">Risk explanation</span>
        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-slate-400">
          {risk.source === "llm" ? "LLM" : "Rule template"}
        </span>
        <span className="text-[10px] text-slate-600">Read-only, does not affect the verdict</span>
      </div>
      <p className="text-sm leading-6 text-slate-200">{risk.risk_summary}</p>
      <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-400">
        {risk.supporting_facts.map((f, i) => <li key={i}>{f}</li>)}
      </ul>
      {risk.suggested_review_questions.length > 0 && (
        <div className="pt-1">
          <div className="text-xs font-medium text-slate-400">Check before approving</div>
          <ul className="mt-1 space-y-0.5 text-xs text-slate-300">
            {risk.suggested_review_questions.map((q, i) => <li key={i}>？{q}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

export function timeAgo(ts: number) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}
