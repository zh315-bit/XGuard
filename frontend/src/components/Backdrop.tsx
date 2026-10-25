/** 全局背景（亮色）：晨雾白到浅天蓝、薄荷绿的渐变 + 缓慢漂移的光晕，叠加金融图表网格、K 线与均线、账本网络里流动的交易、漂浮的币。
 *  K 线位置固定、原地上下起伏：均线随之波动，每根 K 线随涨跌在红绿之间切换。
 *  全部是低不透明度的装饰层，不拦截鼠标；系统开启“减少动态效果”时动画停止。 */
import { useEffect, useMemo, useState } from "react";

const W = 1600;
const H = 420;
const N = 72;              // K 线根数（位置固定，不平移）
const STEP = W / N;
const LO = 60, HI = 140;   // 固定价格区间
const FPS = 30;

/** 固定种子的伪随机数：每根 K 线的相位、影线长度每次都一样 */
function rng(seed: number) {
  return () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
}

const rand = rng(42);
const SEEDS = Array.from({ length: N }, () => ({ phase: rand() * Math.PI * 2, speed: 0.6 + rand() * 0.9, wick: 1.5 + rand() * 3 }));

/** 第 i 根 K 线在 t 秒时的走势价格：一条静态走势 + 整体缓慢的上下“呼吸”。均线就画在这条走势上。 */
function trend(i: number, t: number) {
  const shape = 100 + 14 * Math.sin(i * 0.13) + 6 * Math.sin(i * 0.37 + 1);
  const breathe = 12 * Math.sin(t * 0.55) * Math.sin(i * 0.09 + 0.6);
  return shape + breathe;
}

/** 每根 K 线自己的小幅上下浮动：只平移整根 K 线，不改变它的红绿。 */
function bob(i: number, t: number) {
  const s = SEEDS[i];
  return 2.5 * Math.sin(t * s.speed + s.phase);
}

/** 原地起伏的行情：每帧重算所有 K 线。K 线的红绿跟走势从左到右的方向一致：
 *  比前一根高就是绿（上升段），比前一根低就是红（下降段）。走势随时间起伏，红绿也随之交替。 */
function useWavingCandles() {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let raf = 0, last = 0;
    const start = performance.now();
    const loop = (now: number) => {
      if (now - last >= 1000 / FPS) {
        last = now;
        setT((now - start) / 1000);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);

  return useMemo(() => {
    const y = (v: number) => H - 30 - ((v - LO) / (HI - LO)) * (H - 120);
    const prices = SEEDS.map((_, i) => trend(i, t));
    const candles = prices.map((close, i) => {
      const open = i > 0 ? prices[i - 1] : trend(-1, t);   // 开盘 = 前一根的收盘
      const up = close >= open;
      const half = Math.max(0.6, Math.abs(close - open) * 1.6);   // 放大实体，让 K 线块更明显；方向不变
      const shift = bob(i, t);
      const top = close + half + shift, bottom = close - half + shift;
      return {
        x: i * STEP + STEP / 2, up,
        o: y(up ? bottom : top), c: y(up ? top : bottom),
        h: y(top + SEEDS[i].wick), l: y(bottom - SEEDS[i].wick), w: STEP * 0.45,
      };
    });
    const ma = prices.map(y);
    const line = ma.map((v, i) => `${i ? "L" : "M"}${(i * STEP + STEP / 2).toFixed(1)},${v.toFixed(1)}`).join(" ");
    return { candles, line, area: `${line} L${W - STEP / 2},${H} L${STEP / 2},${H} Z` };
  }, [t]);
}

// 账本网络：节点 = 账户，连线 = 付款通道，光点 = 正在流动的交易
const NODES: [number, number][] = [[60, 80], [210, 40], [340, 130], [480, 60], [560, 200], [400, 260], [230, 220], [110, 300]];
const EDGES: [number, number][] = [[0, 1], [1, 2], [2, 3], [3, 4], [2, 5], [4, 5], [5, 6], [6, 2], [6, 7], [0, 6], [1, 3]];
const PULSES = [[0, 4.5], [2, 6], [4, 5.2], [6, 7], [8, 5.6], [10, 6.4]] as const;

const COINS = [
  { glyph: "XRP", left: "8%", size: 46, dur: 38, delay: 0 },
  { glyph: "$", left: "22%", size: 30, dur: 44, delay: -12 },
  { glyph: "¥", left: "41%", size: 34, dur: 50, delay: -30 },
  { glyph: "XRP", left: "63%", size: 38, dur: 42, delay: -20 },
  { glyph: "€", left: "78%", size: 28, dur: 47, delay: -6 },
  { glyph: "XRP", left: "92%", size: 32, dur: 40, delay: -26 },
];

export function Backdrop() {
  const { candles, line, area } = useWavingCandles();
  return (
    <div aria-hidden className="backdrop pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      {/* 底色渐变 */}
      <div className="absolute inset-0 bg-[linear-gradient(160deg,#f8fbff_0%,#e8f3ff_38%,#ecfbf4_72%,#fdf8ec_100%)]" />

      {/* 漂移的光晕：天蓝（信任）、翡翠绿（资金）、琥珀金（价值）、靛蓝 */}
      <div className="aurora absolute -left-[10%] -top-[20%] h-[70vh] w-[60vw] bg-[radial-gradient(closest-side,rgba(56,189,248,0.30),transparent)]" />
      <div className="aurora aurora-2 absolute -right-[15%] top-[10%] h-[65vh] w-[55vw] bg-[radial-gradient(closest-side,rgba(52,211,153,0.24),transparent)]" />
      <div className="aurora aurora-3 absolute bottom-[-25%] left-[25%] h-[60vh] w-[50vw] bg-[radial-gradient(closest-side,rgba(251,191,36,0.22),transparent)]" />
      <div className="aurora aurora-2 absolute left-[35%] top-[30%] h-[50vh] w-[40vw] bg-[radial-gradient(closest-side,rgba(129,140,248,0.16),transparent)]" />

      {/* 图表坐标网格，向四周淡出 */}
      <div className="absolute inset-0 bg-[linear-gradient(rgba(51,65,85,0.07)_1px,transparent_1px),linear-gradient(90deg,rgba(51,65,85,0.07)_1px,transparent_1px)] bg-[size:56px_56px] [mask-image:radial-gradient(ellipse_at_center,black_30%,transparent_80%)]" />

      {/* 账本网络（右上） */}
      <svg viewBox="0 0 620 340" className="absolute right-[-2%] top-[16%] w-[46vw] max-w-[760px] opacity-70">
        {EDGES.map(([a, b], i) => (
          <path key={i} id={`edge-${i}`} d={`M${NODES[a][0]},${NODES[a][1]} L${NODES[b][0]},${NODES[b][1]}`}
                stroke="rgba(2,132,199,0.35)" strokeWidth="1" fill="none" strokeDasharray="3 5" />
        ))}
        {NODES.map(([x, y], i) => (
          <g key={i}>
            <circle cx={x} cy={y} r="9" fill="rgba(255,255,255,0.7)" stroke="rgba(2,132,199,0.45)" />
            <circle cx={x} cy={y} r="2.5" fill="rgba(2,132,199,0.85)" />
          </g>
        ))}
        {PULSES.map(([edge, dur], i) => (
          <circle key={i} r="2.5" fill={i % 2 ? "#059669" : "#d97706"} className="pulse">
            <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${-i * 1.3}s`}>
              <mpath href={`#edge-${edge}`} />
            </animateMotion>
          </circle>
        ))}
      </svg>

      {/* K 线 + 均线（底部） */}
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMax slice" className="absolute bottom-0 left-0 h-[46vh] w-full opacity-[0.28]">
        <defs>
          <linearGradient id="bd-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#0ea5e9" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#0ea5e9" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="bd-fade" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="white" stopOpacity="0" />
            <stop offset="15%" stopColor="white" stopOpacity="1" />
            <stop offset="85%" stopColor="white" stopOpacity="1" />
            <stop offset="100%" stopColor="white" stopOpacity="0" />
          </linearGradient>
          <mask id="bd-mask"><rect width={W} height={H} fill="url(#bd-fade)" /></mask>
        </defs>
        <g mask="url(#bd-mask)">
          {candles.map((c, i) => (
            <g key={i} className="candle" style={{ stroke: c.up ? "#059669" : "#e11d48", fill: c.up ? "#10b981" : "#f43f5e" }} fillOpacity={0.5}>
              <line x1={c.x} x2={c.x} y1={c.h} y2={c.l} strokeWidth="1.2" />
              <rect x={c.x - c.w / 2} y={Math.min(c.o, c.c)} width={c.w} height={Math.max(1.5, Math.abs(c.o - c.c))} strokeWidth="0.8" />
            </g>
          ))}
          <path d={area} fill="url(#bd-area)" />
          <path d={line} fill="none" stroke="#0284c7" strokeWidth="2.5" />
        </g>
      </svg>

      {/* 漂浮的币 */}
      {COINS.map((c, i) => (
        <span key={i} className="coin absolute bottom-[-80px] flex items-center justify-center rounded-full border border-amber-500/40 bg-[radial-gradient(circle_at_30%_30%,rgba(254,243,199,0.9),rgba(251,191,36,0.35))] font-semibold text-amber-700/70 shadow-sm shadow-amber-500/20"
              style={{ left: c.left, width: c.size, height: c.size, fontSize: c.glyph.length > 1 ? c.size * 0.28 : c.size * 0.5,
                       animationDuration: `${c.dur}s`, animationDelay: `${c.delay}s` }}>
          {c.glyph}
        </span>
      ))}

      {/* 顶部提亮，保证导航栏可读 */}
      <div className="absolute inset-x-0 top-0 h-40 bg-gradient-to-b from-white/80 to-transparent" />
    </div>
  );
}
