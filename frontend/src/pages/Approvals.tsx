import { useEffect, useState } from "react";
import { RuleText } from "../components/rules";
import { postJson, usePoll, type Approval, type Config } from "../api";
import { Card, Empty, ErrorLine, RiskBlock, StateText, TxLink, short } from "../components/ui";
import { SpeculosScreen } from "../components/SpeculosScreen";
import { DemoDeviceNotice } from "../components/DemoDeviceNotice";
import { approveWithLedger } from "../ledger/approvalSigning";
import { DayGroup, DetailLayout, ExpandRow, KV, MethodIcon, MiniSteps, Tabs, flowSteps, groupByDay, methodLabel } from "../components/queue";

type Tab = "queue" | "history";

export function Approvals({ config }: { config?: Config }) {
  const { data, error, refresh } = usePoll<Approval[]>("/approvals?status=");
  const [tab, setTab] = useState<Tab>("queue");
  // undefined = 默认展开最紧急的那张；null = 用户全部收起
  const [openId, setOpenId] = useState<number | null>();
  const all = data ?? [];
  // 待审批按剩余有效期排序，最紧急的在最上面
  const open = all.filter((a) => a.status === "pending" || a.status === "approving")
    .sort((x, y) => (x.expires_in_seconds ?? Infinity) - (y.expires_in_seconds ?? Infinity));
  const history = all.filter((a) => !open.includes(a)).sort((x, y) => decidedTs(y) - decidedTs(x));
  // 默认展开的那张一旦选定就固定，不会因为来了更紧急的新单子而跳走
  useEffect(() => {
    if (openId === undefined && open[0]) setOpenId(open[0].id);
  }, [openId, open[0]?.id]);
  const expanded = openId ?? undefined;
  const toggle = (id: number) => setOpenId(expanded === id ? null : id);

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_320px]">
      <div className="min-w-0 space-y-6">
        <ErrorLine error={error} />
        <Tabs
          tabs={[{ id: "queue", label: "Pending", count: open.length }, { id: "history", label: "History", count: history.length }]}
          value={tab}
          onChange={setTab}
          right={tab === "queue" && open.length > 0 && <span className="text-xs text-slate-500">Sorted by time left, most urgent first</span>}
        />

        {/* 两栏都保持挂载：切到“历史”时进行中的签名状态不会丢 */}
        <div className={tab === "queue" ? "" : "hidden"}>
          {open.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-800"><Empty>No payments awaiting approval</Empty></div>
          ) : (
            <DayGroup label={`To review · ${open.length}`}>
              {open.map((a) => (
                <PendingRow key={a.id} approval={a} config={config} open={expanded === a.id} onToggle={() => toggle(a.id)} onDone={refresh} />
              ))}
            </DayGroup>
          )}
        </div>
        <div className={tab === "history" ? "space-y-6" : "hidden"}>
          {history.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-800"><Empty>No approvals handled yet</Empty></div>
          ) : groupByDay(history, decidedTs).map((g) => (
            <DayGroup key={g.label} label={g.label}>
              {g.items.map((a) => <HistoryRow key={a.id} approval={a} open={expanded === a.id} onToggle={() => toggle(a.id)} />)}
            </DayGroup>
          ))}
        </div>
      </div>

      <aside className="xl:sticky xl:top-6 xl:self-start">
        <Card title="Ledger device (Speculos)" right={<span className="text-xs text-slate-500">{config?.speculos_model}</span>}>
          <div className="-mt-2 mb-3"><DemoDeviceNotice /></div>
          {config ? <SpeculosScreen url={config.speculos_url} /> : <Empty>Loading configuration…</Empty>}
          {config?.device_address && (
            <div className="mt-4 text-center text-xs text-slate-500">
              Device address <span className="font-mono text-slate-300">{short(config.device_address)}</span>
              <br />Treasury Regular Key
            </div>
          )}
        </Card>
      </aside>
    </div>
  );
}

function approvalSteps(a: Approval) {
  const decided = a.decided_at ? new Date(a.decided_at).toLocaleTimeString() : undefined;
  const approvalDetail =
    a.status === "pending" ? <>Approval #{a.id}{a.expires_in_seconds !== undefined && ` · ${mmss(a.expires_in_seconds)} left`}</>
    : a.status === "rejected" || a.status === "expired" ? <>{decided} {a.reject_reason}</>
    : decided && `Approved ${decided}`;
  return flowSteps(
    { state: a.payment_state ?? "PENDING_APPROVAL", verdict: "HOLD", method: a.method, rule_id: a.rule_id, hasQuote: true, txHash: a.tx_hash },
    {
      quote: `${xrp(a)} XRP → ${short(a.quote.payTo, 4)}`,
      evaluate: <RuleText rule={a.rule_id} className="text-xs text-slate-500" />,
      approval: approvalDetail,
      sign: a.signing_request?.status === "pending" ? "Waiting for confirmation on the device" : undefined,
      chain: a.tx_hash && <TxLink hash={a.tx_hash} url={a.explorer_url} />,
      lock: a.escrow?.create_tx ? <TxLink hash={a.escrow.create_tx} url={a.escrow.create_tx_url} /> : undefined,
      release: a.escrow?.finish_tx ? <TxLink hash={a.escrow.finish_tx} url={a.escrow.finish_tx_url} />
        : a.escrow?.cancel_tx ? <TxLink hash={a.escrow.cancel_tx} url={a.escrow.cancel_tx_url} /> : undefined,
    },
  );
}

/** 行摘要：#单号 · 类型图标 + 一句话 · 金额 · 右侧状态区。 */
function RowSummary({ a, right }: { a: Approval; right: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[2.5rem_minmax(0,1fr)_7rem_13rem] items-center gap-4">
      <span className="font-mono text-xs text-slate-500">#{a.id}</span>
      <div className="flex min-w-0 items-center gap-3">
        <MethodIcon method={a.method} verdict="HOLD" />
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm text-slate-100">{a.purpose || methodLabel(a.method)}</span>
            {a.payee_unverified && (
              <span className="shrink-0 rounded bg-rose-600 px-1.5 py-0.5 text-[10px] font-bold text-white">Unverified payee</span>
            )}
          </div>
          <div className="truncate text-xs text-slate-500">
            {methodLabel(a.method)} · <RuleText rule={a.rule_id} className="text-xs text-slate-500" />
          </div>
        </div>
      </div>
      <div className="text-right">
        <div className="text-sm tabular-nums text-slate-100">{xrp(a)} XRP</div>
        <div className="font-mono text-[11px] text-slate-500">→ {short(a.quote.payTo, 4)}</div>
      </div>
      {right}
    </div>
  );
}

function PendingRow({ approval: a, config, open, onToggle, onDone }: {
  approval: Approval; config?: Config; open: boolean; onToggle: () => void; onDone: () => void;
}) {
  const steps = approvalSteps(a);
  const signing = a.status === "approving" && a.signing_request?.status === "pending";
  const label = a.status === "pending" ? "Awaiting approval" : signing ? "Awaiting device signature" : "Payment in progress";
  const urgent = a.expires_in_seconds !== undefined && a.expires_in_seconds < 30;
  return (
    <ExpandRow open={open} onToggle={onToggle} accent={a.payee_unverified ? "border-l-rose-500" : "border-l-amber-400"} summary={
      <RowSummary a={a} right={
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <MiniSteps steps={steps} />
            <div className={`text-xs ${a.status === "pending" ? "text-amber-300" : "text-sky-300"}`}>{label}</div>
          </div>
          {a.status === "pending" && a.expires_in_seconds !== undefined && (
            <span title="Time left" className={`rounded-md border px-2 py-1 font-mono text-xs tabular-nums ${urgent ? "border-rose-500/50 text-rose-300" : "border-slate-700 text-slate-300"}`}>
              {mmss(a.expires_in_seconds)}
            </span>
          )}
        </div>
      } />
    }>
      <DetailLayout steps={steps}>
        <ApprovalBody approval={a} config={config} onDone={onDone} />
      </DetailLayout>
    </ExpandRow>
  );
}

function HistoryRow({ approval: a, open, onToggle }: { approval: Approval; open: boolean; onToggle: () => void }) {
  const steps = approvalSteps(a);
  return (
    <ExpandRow open={open} onToggle={onToggle} summary={
      <RowSummary a={a} right={
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <DecisionText status={a.status} />
            <div><StateText state={a.payment_state} /></div>
          </div>
          <TxLink hash={a.tx_hash} url={a.explorer_url} />
        </div>
      } />
    }>
      <DetailLayout steps={steps}>
        <dl className="space-y-2">
          <KV label="Payee" mono>{a.quote.payTo}</KV>
          <KV label="Amount">{xrp(a)} XRP</KV>
          <KV label="Purpose">{a.purpose}</KV>
          <KV label="Resource" mono>{a.url}</KV>
          <KV label="Rule"><RuleText rule={a.rule_id} className="text-sm text-slate-200" /><div className="text-xs text-slate-500">{a.reason}</div></KV>
          <KV label="Created">{new Date(a.created_at).toLocaleString()}</KV>
          <KV label="Decided">{a.decided_at && new Date(a.decided_at).toLocaleString()}</KV>
          <KV label="Rejection reason">{a.reject_reason}</KV>
        </dl>
        {a.payment_error && <p className="rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{a.payment_error}</p>}
        <RiskBlock risk={a.risk} />
      </DetailLayout>
    </ExpandRow>
  );
}

function ApprovalBody({ approval: a, config, onDone }: { approval: Approval; config?: Config; onDone: () => void }) {
  const [status, setStatus] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);     // R3：展开“输入收款地址后 4 位”的确认框
  const [suffix, setSuffix] = useState("");
  const signing = a.status === "approving" && a.signing_request?.status === "pending";
  // 签名已提交（或服务器签名中）、付款正在执行：这时既不能再批准也不能拒绝
  const processing = a.status === "approving" && !signing;
  const unverified = !!a.payee_unverified;
  const expectedSuffix = a.quote.payTo.slice(-4);

  const startSign = () => {
    if (unverified && !signing) {
      setConfirming(true);
      return;
    }
    sign();
  };

  const sign = async (confirmSuffix?: string) => {
    if (!config) return;
    setBusy(true);
    setError(undefined);
    setConfirming(false);
    try {
      await approveWithLedger(a, config, setStatus, confirmSuffix);
      setStatus("Submitted, waiting for on-chain confirmation…");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStatus(undefined);
    } finally {
      setBusy(false);
      onDone();
    }
  };

  const reject = async () => {
    setBusy(true);
    try {
      await postJson(`/approvals/${a.id}/reject`, { reason: "Rejected by the approver in the console" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      onDone();
    }
  };

  const impact = a.budget_impact;
  return (
    <>
      {unverified && (
        <div className="flex items-center gap-3 rounded-lg bg-rose-600 px-4 py-3 text-white">
          <span className="text-xl">⚠</span>
          <div>
            <div className="text-base font-bold">Unverified payee: this may be a phishing address</div>
            <div className="text-xs text-white/85">This address has no AgentGuardVerifiedMerchant on-chain credential. Reject unless you know this payee.</div>
          </div>
        </div>
      )}
      <div>
        <h3 className="text-base font-semibold text-slate-100">{summary(a)}</h3>
        <p className="mt-1"><RuleText rule={a.rule_id} className="text-sm text-amber-200" /></p>
        <details className="mt-1 text-xs text-slate-500">
          <summary className="cursor-pointer">Details</summary>
          Rule {a.rule_id}: {a.reason}
        </details>
      </div>

      <div className="grid gap-3 text-sm sm:grid-cols-3">
        <Field label="Payee"><span className="break-all font-mono text-xs text-slate-200">{a.quote.payTo}</span></Field>
        <Field label="Amount">{xrp(a)} XRP</Field>
        <Field label="Budget impact">
          Total today {impact.total_committed_today_xrp} → {impact.after_approval_xrp ?? impact.total_committed_today_xrp} / {impact.daily_total_cap_xrp} XRP
          {!impact.within_cap && <div className="text-xs text-rose-300">Over the daily total cap; approval will be refused</div>}
        </Field>
      </div>

      {a.method === "escrow" && (
        <p className="rounded-lg bg-sky-500/10 px-3 py-2 text-xs text-sky-200">
          Verify before release: you are signing an EscrowCreate (a lock), not a direct payment. After delivery the guard verifies the data and releases only if it passes; otherwise the funds return to the treasury when the escrow expires.
        </p>
      )}
      {a.acceptance && (a.acceptance.description || a.acceptance.resourceDescription || a.acceptance.dataSchema) && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2 text-xs text-slate-300">
          <span className="text-slate-500">Acceptance terms (bound to the quote hash; any change before payment is refused): </span>
          {a.acceptance.description ?? a.acceptance.resourceDescription}
          {a.acceptance.dataSchema?.required && (
            <span className="ml-1 text-slate-500">· required fields <span className="font-mono">{a.acceptance.dataSchema.required.join(", ")}</span></span>
          )}
        </div>
      )}
      <RiskBlock risk={a.risk} />

      {confirming && (
        <div className="rounded-lg border border-rose-500/50 bg-slate-950 p-4">
          <div className="text-sm text-rose-200">Second confirmation: type the <b>last 4 characters</b> of the payee address <span className="font-mono">{a.quote.payTo}</span></div>
          <div className="mt-3 flex items-center gap-3">
            <input autoFocus value={suffix} onChange={(e) => setSuffix(e.target.value.trim())} maxLength={4}
                   placeholder="Last 4" className="w-28 rounded-md border border-slate-600 bg-slate-900 px-3 py-1.5 font-mono text-sm text-slate-100" />
            <button onClick={() => sign(suffix)} disabled={suffix !== expectedSuffix}
                    className="rounded-lg bg-rose-500 px-4 py-1.5 text-sm font-semibold text-white hover:bg-rose-400 disabled:opacity-40">
              Confirmed, continue to sign
            </button>
            <button onClick={() => { setConfirming(false); setSuffix(""); }}
                    className="text-sm text-slate-400 hover:text-slate-200">Cancel</button>
            {suffix.length === 4 && suffix !== expectedSuffix && <span className="text-xs text-rose-300">Does not match</span>}
          </div>
        </div>
      )}

      {processing ? (
        <div className="text-sm text-sky-300">Signed; payment in progress (on chain / delivery / verification). It moves to “History” when done…</div>
      ) : (
        <div className="flex flex-wrap items-center gap-3 border-t border-slate-800 pt-4">
          <button
            onClick={startSign}
            disabled={busy || !config || confirming}
            className="rounded-lg bg-sky-500 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-50"
          >
            {busy ? "Working…" : signing ? "Continue signing with Ledger" : config?.signer_mode === "speculos" ? "Sign with Ledger" : "Approve (server signs)"}
          </button>
          {!signing && (
            <button onClick={reject} disabled={busy} className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-50">
              Reject
            </button>
          )}
          {config?.signer_mode === "speculos" && (
            <span className="text-xs text-amber-300/90">Check the payee and amount on the device screen, then hold to sign</span>
          )}
        </div>
      )}
      {(status || a.payment_state === "SIGNING") && !error && (
        <div className="text-sm text-sky-300">{status ?? "Waiting for the device signature…"}</div>
      )}
      {error && <div className="rounded-md bg-rose-500/10 px-3 py-2 text-sm text-rose-300">{error}</div>}
    </>
  );
}

const xrp = (a: Approval) => Number(a.quote.amount) / 1_000_000;
const decidedTs = (a: Approval) => Date.parse(a.decided_at ?? a.created_at) / 1000;
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function summary(a: Approval) {
  if (a.method === "topup") return `Top up ${xrp(a)} XRP from treasury → hot wallet (${a.purpose})`;
  if (a.method === "escrow") return `Lock ${xrp(a)} XRP for ${short(a.quote.payTo)}, released only after verification. Purpose: ${a.purpose}`;
  return `Pay ${xrp(a)} XRP to ${short(a.quote.payTo)}. Purpose: ${a.purpose}`;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg bg-slate-950/50 px-3 py-2">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-0.5 text-slate-200">{children}</div>
    </div>
  );
}

function DecisionText({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    approved: ["Approved", "text-emerald-300"], rejected: ["Rejected", "text-rose-300"], expired: ["Expired", "text-slate-400"],
  };
  const [text, color] = map[status] ?? [status, "text-slate-400"];
  return <span className={`text-xs ${color}`}>{text}</span>;
}
