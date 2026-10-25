import { useEffect, useState } from "react";
import { postJson, usePoll, type Mission } from "../api";

const MAX_CHARS = 2000;

type Preset = { id: string; label: string; task: string };
type Mode = "normal" | "garbage";

/** 在网页上发起 agent 任务。任务文本只是 agent 的输入：无论写什么，守卫的判定都不受影响。 */
export function MissionLauncher({ mission, onChange }: { mission?: Mission | null; onChange: () => void }) {
  // 预设也轮询（间隔长）：页面加载时后端还没就绪也能自动补上，不会一直是空的
  const { data: presetData } = usePoll<Preset[]>("/api/missions/presets", 15_000);
  const presets = presetData ?? [];
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean }>();
  const { data: eMode, refresh: refreshMode } = usePoll<{ mode: Mode }>("/api/demo/merchant-e-mode", 5000);
  const running = mission?.status === "running";

  useEffect(() => {
    if (presetData?.length) setTask((t) => t || presetData[0].task);   // 默认预填“正常周报”
  }, [presetData]);

  const act = async (fn: () => Promise<string>) => {
    setBusy(true);
    setMsg(undefined);
    try {
      setMsg({ text: await fn() });
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), bad: true });
    } finally {
      setBusy(false);
      onChange();
    }
  };

  const run = () => act(async () => {
    const r = await postJson<{ id: number }>("/api/missions", { task });
    return `Mission #${r.id} started`;
  });
  const cancel = () => act(async () => {
    await postJson(`/api/missions/${mission!.id}/cancel`);
    return `Mission #${mission!.id} cancelled`;
  });
  const reset = () => act(async () => {
    await postJson("/api/demo/reset");
    return "Cleared local audit, budget, approval and mission records (on-chain transactions are unaffected)";
  });
  const setMode = (mode: Mode) => act(async () => {
    await postJson("/api/demo/merchant-e-mode", { mode });
    refreshMode();
    return `Merchant E now serves ${mode === "normal" ? "normal data" : "garbage data"}`;
  });

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/70 p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">Start a mission</h2>
        <div className="flex flex-wrap gap-2">
          {presets.map((p) => (
            <button key={p.id} onClick={() => setTask(p.task)} disabled={running}
                    className={`rounded-md border px-3 py-1 text-xs disabled:opacity-40 ${p.id === "overspend"
                      ? "border-rose-500/40 text-rose-300 hover:bg-rose-500/10"
                      : "border-slate-700 text-slate-300 hover:bg-slate-800"}`}>
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <textarea value={task} onChange={(e) => setTask(e.target.value.slice(0, MAX_CHARS))} disabled={running} rows={7}
                className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 p-3 text-sm leading-6 text-slate-100 outline-none focus:border-sky-500/60 disabled:opacity-60"
                placeholder="Task for the agent…" />
      <div className="mt-1 flex justify-between text-[11px] text-slate-500">
        <span>The task is only the agent's input; whatever it says, every payment still goes through the guard's policy, budget and approvals</span>
        <span className={task.length >= MAX_CHARS ? "text-amber-300" : ""}>{task.length} / {MAX_CHARS}</span>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button onClick={run} disabled={busy || running || !task.trim()}
                className="rounded-lg bg-sky-500 px-5 py-2 text-sm font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-40">
          {running ? "Running…" : "Run"}
        </button>
        <button onClick={cancel} disabled={busy || !running}
                className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
          Cancel
        </button>
        <span className="mx-1 h-6 w-px bg-slate-800" />
        <button onClick={reset} disabled={busy || running}
                className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-40">
          Reset demo
        </button>
        <div className="flex items-center gap-1 rounded-lg border border-slate-700 p-1 text-xs">
          <span className="px-2 text-slate-500">Merchant E</span>
          {(["normal", "garbage"] as const).map((m) => (
            <button key={m} onClick={() => setMode(m)} disabled={busy}
                    className={`rounded-md px-2.5 py-1 ${eMode?.mode === m
                      ? (m === "normal" ? "bg-emerald-500/20 text-emerald-300" : "bg-rose-500/20 text-rose-300")
                      : "text-slate-400 hover:bg-slate-800"}`}>
              {m === "normal" ? "Normal data" : "Garbage data"}
            </button>
          ))}
        </div>
        {msg && <span className={`text-xs ${msg.bad ? "text-rose-300" : "text-sky-300"}`}>{msg.text}</span>}
      </div>
    </section>
  );
}
