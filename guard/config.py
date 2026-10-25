"""项目级配置：定位项目根目录、加载 .env。其他模块都从这里取环境变量。

ROOT（.env、policy.yaml、data/ 所在目录）的查找顺序：
  1. 环境变量 AGENTGUARD_HOME
  2. 在本仓库里以 -e 方式安装 / 直接运行：仓库根目录（有 policy.yaml 的那一层）
  3. 作为依赖安装到别的项目：当前工作目录
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _root() -> Path:
    if os.getenv("AGENTGUARD_HOME"):
        return Path(os.environ["AGENTGUARD_HOME"]).resolve()
    repo = Path(__file__).resolve().parent.parent
    return repo if (repo / "policy.yaml").exists() else Path.cwd()


ROOT = _root()
load_dotenv(ROOT / ".env")


def require_env(name: str) -> str:
    """读取必需的环境变量；缺失时给出明确提示，而不是在深处报 KeyError。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}，先运行 scripts/setup_wallets.py 生成 .env")
    return value


def env(name: str, default: str) -> str:
    return os.getenv(name) or default
