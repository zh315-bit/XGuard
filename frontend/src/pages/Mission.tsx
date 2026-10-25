import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import { RuleText } from "../components/rules";
import remarkGfm from "remark-gfm";
import { usePoll, type Escrow, type Mission as MissionT, type MissionEvent } from "../api";
import { EscrowTimeline } from "../components/EscrowTimeline";
import { MissionLauncher } from "../components/MissionLauncher";
import { SpendReport } from "../components/SpendReport";
import { Card, Empty, ErrorLine, TxLink, VerdictBadge, timeAgo } from "../components/ui";

export function Mission() {
  const { data, error, refresh } = usePoll<MissionT | null>("/api/missions/latest");
  const { data: escrows } = usePoll<Escrow[]>("/api/escrows");
  const launcher = <MissionLauncher mission={data} onChange={refresh} />;

  if (!data) {
    return (
      <Layout error={error} left={<>{launcher}<SpendReport /></>}>
        <Card className="flex h-full min-h-[420px] items-center justify-center">
          <Empty>No missions yet. Enter a task on the left and click “Run”, or use <code className="text-slate-300">scripts/demo_run.py</code></Empty>
        </Card>
      </Layout>
    );
  }

  const purchases = data.events.filter((e) => e.kind === "tool_result" && e.name === "fetch_paid_resource");
  // 周报就是 agent 的最后一条输出：已在“最终周报”里显示，这里不再重复
  const steps = data.events.filter((e) => !(e.kind === "text" && isReport(String(e.payload), data.report)));
  const missionEscrows = (escrows ?? []).filter((e) => e.created_at >= data.created_at);
  return (
    <Layout error={error} left={
      <>
        {launcher}
        <SpendReport />
        <Card title={`Mission #${data.id}`} right={<MissionStatus status={data.status} />}>
          <p className="max-h-48 overflow-y-auto whitespace-pre-wrap text-sm leading-6 text-slate-200">{data.task}</p>
          <p className="mt-2 text-xs text-slate-500">Started {timeAgo(data.created_at)} · the agent holds no keys and can only spend through AgentGuard's MCP tools</p>
        </Card>
      </>
    }>
      {data.status === "llm_unavailable" && (
        <div className="rounded-xl border border-rose-500/60 bg-rose-500/10 px-5 py-4">
          <div className="text-lg font-bold text-rose-200">⚠ LLM unavailable, mission aborted</div>
          <p className="mt-1 text-sm text-rose-100/80">
            The Anthropic API still failed after retries, so the agent ended the mission instead of waiting. The guard's records are authoritative for any payments already made; a summary of completed steps is below.
          </p>
        </div>
      )}
      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <Card title="Steps">
          {steps.length === 0 ? <Empty>Waiting for the agent's first step…</Empty> : (
            <ol className="space-y-3">
              {steps.map((e) => <Step key={e.id} event={e} />)}
            </ol>
          )}
        </Card>

        <div className="space-y-6">
          <Card title="Purchased data sources">
            {purchases.length === 0 ? <Empty>Nothing purchased yet</Empty> : (
              <table className="w-full text-sm">
                <tbody>
                  {purchases.map((e) => {
                    const r = finalResult(e, data.events);
                    return (
                      <tr key={e.id} className="border-b border-slate-800 last:border-0">
                        <td className="py-2 pr-3 font-mono text-xs text-slate-200">{r.url ? new URL(r.url).pathname : "—"}</td>
                        <td className="py-2 pr-3"><VerdictBadge verdict={r.verdict ?? "—"} /></td>
                        <td className="py-2 pr-3 text-xs text-slate-400">{r.status}</td>
                        <td className="py-2 pr-3 text-right text-xs tabular-nums text-slate-300">{r.quote ? `${r.quote.amount_xrp} XRP` : ""}</td>
                        <td className="py-2 text-right"><TxLink hash={r.tx_hash} url={r.explorer_url} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>

          {missionEscrows.length > 0 && (
            <Card title="Verify before release (escrow)">
              <div className="space-y-4">
                {missionEscrows.map((e) => <EscrowTimeline key={e.id} escrow={e} />)}
              </div>
            </Card>
          )}

          <Card title={data.status === "llm_unavailable" ? "Completed steps" : "Final report"}>
            {data.report ? (
              <Markdown>{data.report}</Markdown>
            ) : <Empty>{data.status === "running" ? "The agent is still working…" : "No report was produced"}</Empty>}
          </Card>
        </div>
      </div>
    </Layout>
  );
}

/** 首页左右布局：左边是控制栏（发起任务、今日花费、当前任务），右边是任务进展。窄屏时上下堆叠。
 *  宽屏时左栏固定在视口里，右边往下滚也一直能看到；左栏比视口高时在栏内单独滚动。 */
function Layout({ error, left, children }: { error?: string; left: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-6">
      <ErrorLine error={error} />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[420px_minmax(0,1fr)] 2xl:grid-cols-[460px_minmax(0,1fr)]">
        <aside className="sidebar-scroll space-y-6 lg:sticky lg:top-6 lg:max-h-[calc(100vh-3rem)] lg:self-start lg:overflow-y-auto lg:pr-1">{left}</aside>
        <div className="min-w-0 space-y-6">{children}</div>
      </div>
    </div>
  );
}

/** agent 的这段文字是否就是周报（忽略空白差异）。
 *  agent 上报文字事件时截到 2000 字（agent.py），所以被截断的周报只是它的开头部分。 */
function isReport(text: string, report?: string) {
  if (!report) return false;
  const norm = (t: string) => t.replace(/\s+/g, " ").trim();
  const t = norm(text), r = norm(report);
  return t === r || (text.length >= 2000 && r.startsWith(t));
}

/** HOLD 的购买以之后 wait_for_approval / approval_status 的最后结果为准（批准、验货不通过等）。 */
function finalResult(purchase: MissionEvent, events: MissionEvent[]) {
  const first = purchase.payload ?? {};
  if (!first.approval_id) return first;
  const later = events.filter((e) => e.kind === "tool_result" && e.id > purchase.id &&
    (e.name === "wait_for_approval" || e.name === "approval_status") && e.payload?.approval_id === first.approval_id);
  const last = later.at(-1)?.payload;
  return last ? { ...first, ...last, verdict: first.verdict, quote: first.quote } : first;
}

function Step({ event: e }: { event: MissionEvent }) {
  if (e.kind === "text") return <AgentText text={String(e.payload)} />;
  if (e.kind === "tool_call") {
    return (
      <li className="rounded-lg border border-slate-800 px-3 py-2">
        <div className="text-xs text-slate-500">Tool call</div>
        <div className="font-mono text-sm text-sky-300">{e.name}</div>
        <pre className="mt-1 whitespace-pre-wrap break-all text-xs text-slate-400">{JSON.stringify(e.payload, null, 1)}</pre>
      </li>
    );
  }
  const r = e.payload ?? {};
  return (
    <li className="ml-4 rounded-lg border-l-2 border-slate-700 bg-slate-950/40 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-500">Result</span>
        {r.verdict && <VerdictBadge verdict={r.verdict} />}
        <RuleText rule={r.rule_id} />
        {r.status && <span className="text-xs text-slate-300">{r.status}</span>}
        {r.approval_id && <span className="text-xs text-amber-300">Approval #{r.approval_id}</span>}
        <TxLink hash={r.tx_hash} url={r.explorer_url} />
      </div>
      {r.reason && <p className="mt-1 text-xs text-slate-400">{r.reason}</p>}
    </li>
  );
}

/** agent 输出的 Markdown（标题、表格、列表）。表格放在可横向滚动的容器里，避免长哈希撑破卡片。 */
function Markdown({ children }: { children: string }) {
  return (
    <div className="report">
      <ReactMarkdown remarkPlugins={[remarkGfm]}
                     components={{ table: ({ node: _node, ...props }) => <div className="overflow-x-auto"><table {...props} /></div> }}>
        {children}
      </ReactMarkdown>
    </div>
  );
}

/** agent 说的话。长文本（通常是最后的周报草稿）默认折叠，可展开。 */
function AgentText({ text }: { text: string }) {
  const long = text.length > 400 || text.split("\n").length > 12;
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-lg border border-slate-800 bg-slate-950/40 px-4 py-3">
      <div className="mb-1 text-xs text-slate-500">💬 agent</div>
      <div className={long && !open ? "relative max-h-56 overflow-hidden" : ""}>
        <Markdown>{text}</Markdown>
        {long && !open && <div className="absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-slate-950 to-transparent" />}
      </div>
      {long && (
        <button onClick={() => setOpen(!open)} className="mt-2 text-xs text-sky-400 hover:underline">
          {open ? "Collapse" : "Show all"}
        </button>
      )}
    </li>
  );
}

function MissionStatus({ status }: { status: string }) {
  const style = { running: "text-sky-300", done: "text-emerald-300", failed: "text-rose-300", cancelled: "text-slate-400", llm_unavailable: "text-rose-300" }[status] ?? "text-slate-400";
  const text = { running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled", llm_unavailable: "LLM unavailable, aborted" }[status] ?? status;
  return <span className={`text-xs font-semibold ${style}`}>{status === "running" ? "● " : ""}{text}</span>;
}
