import { usePoll } from "../api";

type Status = { ok: boolean; reason: string | null; probe: { ok: boolean; latency_ms?: number } };

/** 顶部的 LLM 状态灯：demo 前一眼看到 Anthropic API 是否可用（agent、风险解释、验货都依赖它）。 */
export function LLMStatus() {
  const { data, error } = usePoll<Status>("/api/llm/status", 15_000);
  const ok = data?.ok && !error;
  const text = !data ? "Checking LLM…" : ok ? "LLM OK" : "LLM unavailable";
  const tip = !data ? "" : ok ? `Anthropic API responded in ${data.probe.latency_ms ?? "—"} ms`
    : error ?? data.reason ?? "The most recent LLM call failed";
  return (
    <span title={tip}
          className={`flex items-center gap-1.5 rounded-md px-2 py-1 font-medium ${
            !data ? "text-slate-400" : ok ? "bg-emerald-500/10 text-emerald-300" : "bg-rose-500/15 text-rose-300"}`}>
      <span className={`h-2 w-2 rounded-full ${!data ? "bg-slate-500" : ok ? "bg-emerald-400" : "bg-rose-400"}`} />
      {text}
      {data && !ok && data.reason && <span className="font-normal text-rose-200/80">· {data.reason}</span>}
    </span>
  );
}
