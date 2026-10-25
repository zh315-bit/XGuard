#!/usr/bin/env bash
# 启动加载了 XRP 应用的 Speculos（Ledger 设备模拟器），后台运行。
# 参考 Ledger 官方 workshop 的 run-speculos.sh，区别：
#   - docker run -d 后台运行（方便一键启动全部服务），日志看 `docker logs -f speculos-xrp`
#   - 不用 --rm：容器退出后日志还在，能查崩溃原因；--restart unless-stopped：模拟器无论以什么退出码退出都会自动拉起（实测杀掉模拟器后主进程以 0 退出）
#   - 宿主机端口 5001 → 容器 5000（macOS 的 AirPlay 接收器常占用 5000）
#   - seed 保存在 speculos/.seed，设备地址固定（金库的 Regular Key 就是这个地址）
#
# 用法：scripts/start_speculos.sh [stax|flex|apex_p|nanox|nanosp]   （默认 stax）
# 停止：docker rm -f speculos-xrp
set -euo pipefail

MODEL="${1:-stax}"
IMAGE="${SPECULOS_IMAGE:-ghcr.io/ledgerhq/speculos:latest}"
API_PORT="${SPECULOS_API_PORT:-5001}"
root="$(cd "$(dirname "$0")/.." && pwd -P)"
apps="$root/speculos/apps"

[ -f "$apps/$MODEL/app.elf" ] || { echo "未知型号 '$MODEL'，可选：$(ls "$apps" | tr '\n' ' ')" >&2; exit 1; }
command -v docker >/dev/null || { echo "需要 docker（本机用 colima：colima start）" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker 没在运行（colima start）" >&2; exit 1; }

if [ -z "${SPECULOS_SEED:-}" ]; then
  [ -f "$root/speculos/.seed" ] || { openssl rand -hex 32 > "$root/speculos/.seed"; chmod 600 "$root/speculos/.seed"; }
  SPECULOS_SEED="$(cat "$root/speculos/.seed")"
fi

if docker ps --format '{{.Names}}' | grep -qx speculos-xrp; then
  echo "[speculos] 已在运行：http://127.0.0.1:${API_PORT}"
  exit 0
fi
if docker ps -a --format '{{.Names}}' | grep -qx speculos-xrp; then
  # 之前的容器已退出：先把它最后的日志存下来，再删掉重建
  mkdir -p "$root/data/logs"
  docker logs --tail 200 speculos-xrp > "$root/data/logs/speculos-exited-$(date +%Y%m%d-%H%M%S).log" 2>&1 || true
  echo "[speculos] 上一个容器已退出（$(docker inspect -f '{{.State.ExitCode}}' speculos-xrp)），日志已存到 data/logs/"
  docker rm -f speculos-xrp >/dev/null 2>&1 || true
fi

docker run -d --restart unless-stopped --name speculos-xrp \
  -p "127.0.0.1:$API_PORT:5000" -p 127.0.0.1:9999:9999 -p 127.0.0.1:41000:41000 \
  -v "$apps:/apps:ro" \
  "$IMAGE" \
  --model "$MODEL" --display headless --api-port 5000 --apdu-port 9999 \
  --vnc-port 41000 --vnc-password speculos \
  --seed "$SPECULOS_SEED" \
  "/apps/$MODEL/app.elf" >/dev/null

# Speculos 不会打印 API 就绪日志：轮询 HTTP 接口
for _ in $(seq 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:$API_PORT"; then
    echo "[speculos] 就绪（${MODEL}）：API http://127.0.0.1:${API_PORT}  APDU 127.0.0.1:9999  VNC vnc://127.0.0.1:41000（密码 speculos）"
    exit 0
  fi
  sleep 1
done
echo "[speculos] 60 秒内 API 没有响应，看日志：docker logs speculos-xrp" >&2
exit 1
