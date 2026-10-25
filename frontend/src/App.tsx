import { useEffect, useState } from "react";
import { Backdrop } from "./components/Backdrop";
import { LLMStatus } from "./components/LLMStatus";
import { usePoll, type Approval, type Config } from "./api";
import { Approvals } from "./pages/Approvals";
import { Mission } from "./pages/Mission";
import { PaymentQueue } from "./pages/PaymentQueue";
import { Treasury } from "./pages/Treasury";

const PAGES = [
  { id: "mission", label: "Mission" },
  { id: "queue", label: "Payment Queue" },
  { id: "approvals", label: "Approvals" },
  { id: "treasury", label: "Treasury & Audit" },
] as const;
type PageId = (typeof PAGES)[number]["id"];

function pageFromHash(): PageId {
  const id = location.hash.slice(1);
  return PAGES.some((p) => p.id === id) ? (id as PageId) : "mission";
}

export function App() {
  const [page, setPage] = useState<PageId>(() => pageFromHash());
  // 直接改地址栏的 #hash（或前进 / 后退）也能切换页面
  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const { data: config } = usePoll<Config>("/api/config", 10_000);
  const { data: pending } = usePoll<Approval[]>("/approvals?status=pending");
  const go = (id: PageId) => {
    setPage(id);
    location.hash = id;
  };

  return (
    <div className="relative isolate min-h-screen text-slate-100">
      <Backdrop />
      <header className="border-b border-slate-800/70 bg-slate-950/50 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] items-center gap-8 px-6 py-4">
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="AgentGuard" className="h-10 w-10 rounded-xl shadow-md shadow-sky-500/20 ring-1 ring-slate-800" />
            <div>
              <div className="text-sm font-semibold tracking-wide">AgentGuard</div>
              <div className="text-[11px] text-slate-500">AI agent 支付守卫 · XRPL Testnet</div>
            </div>
          </div>
          <nav className="flex gap-1">
            {PAGES.map((p) => (
              <button
                key={p.id}
                onClick={() => go(p.id)}
                className={`relative rounded-lg px-4 py-2 text-sm transition ${page === p.id ? "bg-slate-800 text-slate-50" : "text-slate-400 hover:text-slate-200"}`}
              >
                {p.label}
                {p.id === "approvals" && pending && pending.length > 0 && (
                  <span className="ml-2 rounded-full bg-amber-500 px-1.5 py-0.5 text-[10px] font-bold text-slate-950">{pending.length}</span>
                )}
              </button>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
            <LLMStatus />
            {config && <span>签名：{config.signer_mode === "speculos" ? "Ledger（Speculos）" : "服务器（备用）"}</span>}
            <span className="flex items-center gap-1.5"><span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />每 2 秒刷新</span>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1500px] px-6 py-6">
        {page === "mission" && <Mission />}
        {page === "queue" && <PaymentQueue />}
        {page === "approvals" && <Approvals config={config} />}
        {page === "treasury" && <Treasury />}
      </main>
    </div>
  );
}
