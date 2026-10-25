#!/usr/bin/env bash
# 一键启动全部服务：Docker(colima) → Speculos(5001) → 演示商户(8000) → 守卫 API(8100) → 哨兵 → 控制台(5173)
# 日志在 data/logs/，进程号在 data/pids/。停止：scripts/stop_all.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"
mkdir -p data/logs data/pids

wait_http() {  # wait_http <名字> <url>
  for _ in $(seq 60); do curl -sf -o /dev/null "$2" && { echo "  ✓ $1  $2"; return 0; }; sleep 1; done
  echo "  ✗ $1 没有在 60 秒内就绪，看 data/logs/" >&2; return 1
}

start_bg() {  # start_bg <名字> <命令...>：已在运行就跳过
  local name="$1"; shift
  if [ -f "data/pids/$name" ] && kill -0 "$(cat "data/pids/$name")" 2>/dev/null; then return 0; fi
  nohup "$@" > "data/logs/$name.log" 2>&1 &
  echo $! > "data/pids/$name"
}

echo "[1/6] Docker"
if ! docker info >/dev/null 2>&1; then
  command -v colima >/dev/null || { echo "需要 docker：brew install colima docker" >&2; exit 1; }
  colima start --vm-type vz --vz-rosetta --cpu 4 --memory 6 >/dev/null
fi
echo "  ✓ docker $(docker info --format '{{.ServerVersion}}')"

echo "[2/6] Speculos"
scripts/start_speculos.sh "${SPECULOS_DEVICE:-stax}" | sed 's/^/  /'

echo "[3/6] 演示商户"
start_bg merchant .venv/bin/uvicorn merchants.demo:app --port 8000
wait_http "商户" http://127.0.0.1:8000/health

echo "[4/6] 守卫 API"
start_bg api .venv/bin/uvicorn guard.api:app --port 8100
wait_http "守卫 API" "http://127.0.0.1:8100/health"

echo "[5/6] 独立哨兵（Quicknode WSS 实时监控，独立进程）"
start_bg sentinel .venv/bin/python watchdog/sentinel.py
sentinel_status() { curl -sf http://127.0.0.1:8100/health | .venv/bin/python -c 'import json,sys; h=json.load(sys.stdin); print(h["sentinel"], h["sentinel_endpoint"] or "")'; }
for _ in $(seq 30); do [ "$(sentinel_status | cut -d' ' -f1)" = connected ] && break; sleep 1; done
if [ "$(sentinel_status | cut -d' ' -f1)" = connected ]; then echo "  ✓ 哨兵  $(sentinel_status)"
else echo "  ✗ 哨兵没有连上（$(sentinel_status)），看 data/logs/sentinel.log" >&2; fi

echo "[6/6] 控制台"
[ -d frontend/node_modules ] || (cd frontend && npm install --no-audit --no-fund >/dev/null)
start_bg console npm --prefix frontend run dev -- --host 127.0.0.1
wait_http "控制台" http://127.0.0.1:5173

cat <<MSG

全部就绪。签名方式：$(grep -E '^SIGNER_MODE=' .env | cut -d= -f2)
  控制台   http://127.0.0.1:5173
  设备     http://127.0.0.1:5001（控制台 Approvals 页右侧可直接操作）
  哨兵     $(sentinel_status)（Treasury & Audit 页的 Quicknode live monitor 卡片）

跑 demo：
  curl -X POST http://127.0.0.1:8100/api/demo/reset      # 清空本地状态（DEMO_MODE=true）
  .venv/bin/python agent/agent.py                        # agent 买 A、B、D；B 在控制台用 Ledger 签名批准
  .venv/bin/python scripts/simulate_compromise.py        # 模拟服务器被攻破，观察控制台的 UNAUTHORIZED 警报
MSG
