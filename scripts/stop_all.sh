#!/usr/bin/env bash
# 停止 start_all.sh 启动的服务（colima 虚拟机保留，下次启动更快；要关掉它：colima stop）
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"
for name in console api merchant; do
  pidfile="data/pids/$name"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    pkill -P "$pid" 2>/dev/null || true   # npm 启动的 vite 是子进程
    kill "$pid" 2>/dev/null && echo "已停止 $name"
    rm -f "$pidfile"
  fi
done
docker rm -f speculos-xrp >/dev/null 2>&1 && echo "已停止 speculos"
exit 0
