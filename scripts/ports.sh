# start_all.sh / stop_all.sh 共用：端口、进程查询。用 source 引入，需要先设置 $root。

PORT_SPECULOS=5001
PORT_MERCHANT=8000
PORT_API=8100
PORT_CONSOLE=5173
ALL_PORTS="$PORT_SPECULOS $PORT_MERCHANT $PORT_API $PORT_CONSOLE"

listeners() {  # listeners <端口>：监听该端口的进程号
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u
}

describe() {  # describe <pid>：进程号 + 命令行，给人看
  echo "pid $1: $(ps -o command= -p "$1" 2>/dev/null | cut -c1-120)"
}

is_colima_forward() {  # colima 的 ssh 端口转发：不能杀，杀了 colima 就断了；容器删掉后它会自己释放端口
  ps -o command= -p "$1" 2>/dev/null | grep -q '^ssh: .*colima'
}

descends_from() {  # descends_from <pid> <祖先 pid>：npm → sh → node（vite）这类子孙进程也算
  local p="$1"
  for _ in 1 2 3 4; do
    [ "$p" = "$2" ] && return 0
    p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    [ -z "$p" ] || [ "$p" = 1 ] && return 1
  done
  return 1
}

sentinel_pids() {  # 本仓库的哨兵进程（按命令行找，再核对工作目录，不误伤别的项目）
  local pid
  for pid in $(pgrep -f 'watchdog/sentinel\.py' 2>/dev/null); do
    [ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" = "$root" ] && echo "$pid"
  done
}
