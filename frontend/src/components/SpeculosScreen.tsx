import { useEffect, useRef, useState, type PointerEvent } from "react";

/**
 * Speculos 设备屏幕，可以直接用鼠标操作（做法同 Ledger 官方 workshop 的 SpeculosScreen）：
 * - 触屏型号（Stax / Flex）：点击 = 轻触，按住不放 = “Hold to sign”
 * - 按键型号（Nano X / S Plus）：用屏幕下方的三个按钮
 * 屏幕变化时 Speculos 会推送 /events 事件，收到就刷新截图。
 */
export function SpeculosScreen({ url }: { url: string }) {
  const base = url.replace(/\/+$/, "");
  const [src, setSrc] = useState<string>();
  const [hasButtons, setHasButtons] = useState(false);
  const [failures, setFailures] = useState(0);   // 连续加载失败次数：失败就重试，不立即判定“没在运行”
  const pressed = useRef<{ x: number; y: number } | undefined>(undefined);

  useEffect(() => {
    const refresh = () => setSrc(`${base}/screenshot?t=${Date.now()}`);
    refresh();
    const events = new EventSource(`${base}/events?stream=true`);
    events.onmessage = refresh;
    events.onopen = refresh;
    events.onerror = () => setTimeout(refresh, 2000);
    return () => events.close();
  }, [base]);

  // 截图加载失败后每 2 秒重试（一次失败可能只是瞬时的）
  useEffect(() => {
    if (failures === 0) return;
    const timer = setTimeout(() => setSrc(`${base}/screenshot?t=${Date.now()}`), 2000);
    return () => clearTimeout(timer);
  }, [failures, base]);

  const post = (path: string, body: object) =>
    fetch(`${base}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => undefined);

  const onPointerDown = (e: PointerEvent<HTMLImageElement>) => {
    const img = e.currentTarget;
    const rect = img.getBoundingClientRect();
    const x = Math.round(((e.clientX - rect.left) * img.naturalWidth) / rect.width);
    const y = Math.round(((e.clientY - rect.top) * img.naturalHeight) / rect.height);
    img.setPointerCapture(e.pointerId);
    pressed.current = { x, y };
    post("finger", { action: "press", x, y });
  };

  const onPointerUp = () => {
    if (!pressed.current) return;
    post("finger", { action: "release", ...pressed.current });
    pressed.current = undefined;
  };

  const button = (name: "left" | "both" | "right") => post(`button/${name}`, { action: "press-and-release" });

  return (
    <div className="flex flex-col items-center gap-3">
      <div className="rounded-[28px] border border-slate-700 bg-black p-3 shadow-2xl shadow-black/50">
        {src && (
          // 失败时只隐藏、不卸载：后台重试成功后 onLoad 会把失败计数清零，画面自动恢复
          <img
            src={src}
            alt="Ledger 设备屏幕（Speculos）"
            draggable={false}
            className={failures >= 3 ? "hidden" : "w-[260px] cursor-pointer select-none rounded-2xl"}
            onPointerDown={onPointerDown}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onLoad={(e) => {
              setHasButtons(e.currentTarget.naturalWidth > e.currentTarget.naturalHeight);
              setFailures(0);
            }}
            onError={() => setFailures((n) => n + 1)}
          />
        )}
        {(!src || failures >= 3) && (
          <div className="flex h-[436px] w-[260px] items-center justify-center p-6 text-center text-xs text-slate-500">
            暂时连不上 Speculos（{base}），每 2 秒重试中…
            <br />一直连不上的话运行 scripts/start_speculos.sh
          </div>
        )}
      </div>
      <p className="text-xs text-slate-500">
        {hasButtons ? "用下方按钮操作设备" : "点击 = 轻触屏幕，按住不放 = 长按签名"}
      </p>
      {src && hasButtons && (
        <div className="flex gap-2">
          {(["left", "both", "right"] as const).map((b) => (
            <button key={b} onClick={() => button(b)} className="rounded-md bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700">
              {b === "left" ? "◀" : b === "right" ? "▶" : "Both"}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
