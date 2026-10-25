import { useEffect, useState } from "react";
import { postJson, usePoll } from "../api";
import { Card } from "./ui";

type Field = { key: string; label: string; help: string; value_xrp: string };
type Change = { id: number; created_at: string; field: string; label: string; old_value: string; new_value: string };
type Settings = { fields: Field[]; history: Change[] };

/** 付款规则设置：改完写回 policy.yaml，下一笔付款立即按新规则判断；每次修改都有记录。 */
export function SettingsCard() {
  const { data, refresh } = usePoll<Settings>("/api/settings", 10_000);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean }>();

  useEffect(() => {          // 正在编辑时不让轮询覆盖输入
    if (data && !dirty) setDraft(Object.fromEntries(data.fields.map((f) => [f.key, f.value_xrp])));
  }, [data, dirty]);

  const changes = Object.fromEntries((data?.fields ?? [])
    .filter((f) => draft[f.key] !== undefined && draft[f.key].trim() !== f.value_xrp)
    .map((f) => [f.key, draft[f.key].trim()]));
  const changedCount = Object.keys(changes).length;

  const save = async () => {
    setBusy(true);
    setMsg(undefined);
    try {
      const r = await postJson<Settings & { changed: string[] }>("/api/settings", { changes });
      setDirty(false);
      setMsg({ text: r.changed.length ? `已保存 ${r.changed.length} 项，下一笔付款立即按新规则判断` : "没有变化" });
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : String(e), bad: true });
    } finally {
      setBusy(false);
      refresh();
    }
  };
  const cancel = () => { setDirty(false); setMsg(undefined); };

  return (
    <Card title="付款规则设置" right={<span className="text-xs text-slate-500">金额单位 XRP · 改完立即生效</span>}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(data?.fields ?? []).map((f) => (
          <label key={f.key} className="block rounded-lg border border-slate-800 bg-slate-950/50 px-4 py-3">
            <div className="text-sm font-medium text-slate-200">{f.label}</div>
            <div className="mt-2 flex items-center gap-2">
              <input value={draft[f.key] ?? ""} inputMode="decimal"
                     onChange={(e) => { setDirty(true); setDraft({ ...draft, [f.key]: e.target.value }); }}
                     className={`w-28 rounded-md border bg-slate-900 px-2 py-1.5 text-right text-lg tabular-nums text-slate-100 ${
                       changes[f.key] !== undefined ? "border-sky-500/70" : "border-slate-700"}`} />
              <span className="text-sm text-slate-400">XRP</span>
            </div>
            <div className="mt-2 text-xs leading-5 text-slate-500">{f.help}</div>
          </label>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button onClick={save} disabled={busy || changedCount === 0}
                className="rounded-lg bg-sky-500 px-5 py-2 text-sm font-semibold text-slate-950 hover:bg-sky-400 disabled:opacity-40">
          保存{changedCount ? `（${changedCount} 项）` : ""}
        </button>
        {dirty && <button onClick={cancel} className="text-sm text-slate-400 hover:text-slate-200">放弃修改</button>}
        {msg && <span className={`text-sm ${msg.bad ? "text-rose-300" : "text-emerald-300"}`}>{msg.text}</span>}
      </div>

      {data && data.history.length > 0 && (
        <div className="mt-5 border-t border-slate-800 pt-3">
          <div className="mb-2 text-xs text-slate-500">修改记录</div>
          <ul className="space-y-1 text-xs text-slate-400">
            {data.history.slice(0, 6).map((h) => (
              <li key={h.id}>
                <span className="text-slate-500">{new Date(h.created_at).toLocaleString()}</span>　
                {h.label}：{h.old_value} → <span className="text-slate-200">{h.new_value}</span> XRP
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
