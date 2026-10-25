import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guard import credentials  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """每个测试用独立的 SQLite 文件、空的凭证缓存，风险解释只用模板（不调 LLM）。"""
    monkeypatch.setenv("GUARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("RISK_EXPLAINER", "template")
    credentials.clear_cache()
    yield
    credentials.clear_cache()
