"""控制台发起的 agent 任务：在后台子进程里运行 agent/agent.py，同一时间只允许一个任务。

安全边界：
- 任务文本只通过环境变量 AGENTGUARD_TASK 交给 agent 当输入；守卫的判定（策略、预算、审批）完全不看它
- 子进程只拿一份最小环境变量：本进程（守卫 API）的环境里有 .env 加载进来的钱包 seed，绝不能继承给 agent
"""

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from guard import missions
from guard.config import ROOT

MAX_TASK_CHARS = 2000
AGENT_COMMAND = [sys.executable, "-u", "agent/agent.py"]
# 只把这些系统变量传给 agent 子进程；模型 key 等由 agent 自己从 .env 按名字读取
_PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR")

_lock = threading.Lock()
_procs: dict[int, subprocess.Popen] = {}


class MissionBusy(RuntimeError):
    def __init__(self, mission: dict) -> None:
        super().__init__(f"任务 #{mission['id']} 正在运行，先等它结束或取消它")
        self.mission = mission


class NotRunning(RuntimeError):
    pass


def agent_env(mission_id: int, task: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ}
    env.update(AGENTGUARD_MISSION_ID=str(mission_id), AGENTGUARD_TASK=task, PYTHONUNBUFFERED="1")
    return env


def start(task: str) -> int:
    task = task.strip()
    if not task or len(task) > MAX_TASK_CHARS:
        raise ValueError(f"任务文本长度必须在 1 到 {MAX_TASK_CHARS} 字之间")
    with _lock:
        busy = running_mission()
        if busy:
            raise MissionBusy(busy)
        mission_id = missions.create(task, source="web")
        log_path = ROOT / "data" / "logs" / f"mission-{mission_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "w")
        proc = subprocess.Popen(AGENT_COMMAND, cwd=ROOT, stdout=log,
                                stderr=subprocess.STDOUT, env=agent_env(mission_id, task),
                                start_new_session=True)   # 独立进程组：取消时连同它拉起的 MCP server 一起结束
        _procs[mission_id] = proc
        missions.set_pid(mission_id, proc.pid)
    threading.Thread(target=_watch, args=(mission_id, proc, log), daemon=True).start()
    return mission_id


def cancel(mission_id: int) -> dict:
    mission = missions.get(mission_id)
    if mission is None or mission["status"] != "running":
        raise NotRunning(f"任务 #{mission_id} 没有在运行")
    # 先标记为已取消，再结束进程：否则 _watch 线程看到进程被杀（非 0 退出码）会抢先标成 failed
    missions.finish(mission_id, "cancelled", "任务已在控制台取消")
    pid = mission.get("pid")
    if pid and _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc = _procs.get(mission_id)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(pid, signal.SIGKILL)
    return missions.get(mission_id)


def running_mission() -> Optional[dict]:
    """正在运行的任务（以进程是否还活着为准：agent 崩溃后不会一直占着“运行中”）。"""
    for mission in missions.running():
        pid = mission.get("pid")
        if pid is None and time.time() - mission["created_at"] < 30:
            return mission                       # 刚注册、还没来得及记下 pid
        if pid and _alive(pid):
            return mission
        missions.finish(mission["id"], "failed", "agent 进程已退出，没有上报结果")
    return None


def _watch(mission_id: int, proc: subprocess.Popen, log) -> None:
    code = proc.wait()
    log.close()
    _procs.pop(mission_id, None)
    # agent 正常结束时自己会上报 done / failed；这里只兜底它没来得及上报的情况
    missions.finish(mission_id, "failed" if code else "done",
                    f"agent 进程退出码 {code}，详见 data/logs/mission-{mission_id}.log" if code else None)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True     # 自己启动的子进程由 _watch 线程及时回收，这里不调用 waitpid，免得抢走它的退出码
