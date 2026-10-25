#!/usr/bin/env bash
# 停止 start_all.sh 启动的服务（colima 虚拟机保留，下次启动更快；要关掉它：colima stop）
#   1. 按 data/pids/ 里的进程号停止
#   2. 停止本仓库的哨兵进程（包括 pid 文件丢失的残留）
#   3. 按端口（5001 8000 8100 5173）找残留进程并停止：先 TERM，5 秒后还在就 KILL
#   4. 检查所有端口确实空闲；还有占用就列出来并以非 0 退出
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"
source scripts/ports.sh

stopped=""
stop_pid() {  # stop_pid <pid> <名字>
  kill -0 "$1" 2>/dev/null || return 0
  stopped="$stopped $1"
  pkill -P "$1" 2>/dev/null || true   # npm 启动的 vite 是子进程
  kill "$1" 2>/dev/null && echo "已停止 $2（pid $1）"
}

wait_gone() {  # wait_gone <pid...>：最多等 5 秒，还在就 KILL
  for _ in $(seq 50); do
    local alive=""
    for p in "$@"; do kill -0 "$p" 2>/dev/null && alive="$alive $p"; done
    [ -z "$alive" ] && return 0
    sleep 0.1
  done
  for p in $alive; do kill -9 "$p" 2>/dev/null && echo "  强制结束 pid $p"; done
}

# 1. pid 文件
for name in console sentinel api merchant; do
  pidfile="data/pids/$name"
  if [ -f "$pidfile" ]; then
    stop_pid "$(cat "$pidfile")" "$name"
    rm -f "$pidfile"
  fi
done

# 2. 哨兵残留
for pid in $(sentinel_pids); do
  case " $stopped " in *" $pid "*) continue ;; esac   # 第 1 步刚发过 TERM，正在退出
  stop_pid "$pid" "残留的哨兵"
done

# Speculos：容器删掉，colima 的端口转发随之释放
docker rm -f speculos-xrp >/dev/null 2>&1 && echo "已停止 speculos"

# 3. 端口残留
leftover=""
for port in $PORT_MERCHANT $PORT_API $PORT_CONSOLE $PORT_SPECULOS; do
  for pid in $(listeners "$port"); do
    if is_colima_forward "$pid"; then continue; fi
    echo "端口 $port 有残留进程，停止：$(describe "$pid")"
    kill "$pid" 2>/dev/null
    leftover="$leftover $pid"
  done
done
[ -n "$leftover" ] && wait_gone $leftover
[ -n "$stopped$(sentinel_pids)" ] && wait_gone $stopped $(sentinel_pids)

# 4. 确认端口空闲（colima 转发释放端口可能要几秒）
busy=""
for _ in $(seq 20); do
  busy=""
  for port in $ALL_PORTS; do [ -n "$(listeners "$port")" ] && busy="$busy $port"; done
  [ -z "$busy" ] && [ -z "$(sentinel_pids)" ] && break
  sleep 0.5
done
if [ -n "$busy" ] || [ -n "$(sentinel_pids)" ]; then
  for port in $busy; do for pid in $(listeners "$port"); do echo "✗ 端口 $port 仍被占用：$(describe "$pid")" >&2; done; done
  for pid in $(sentinel_pids); do echo "✗ 哨兵仍在运行：$(describe "$pid")" >&2; done
  exit 1
fi
echo "✓ 端口 $ALL_PORTS 均已空闲，哨兵已停止"
