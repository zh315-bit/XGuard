"""SQLite 连接与表结构。所有表共用同一个库文件。

两种用法：
- `with connect() as conn:` 普通读写（sqlite3 的上下文管理器负责提交）
- `with immediate() as conn:` 需要“先检查再写入”原子完成时（预算预留、状态转移），
  用 BEGIN IMMEDIATE 先拿写锁，其他写者会排队等待（busy timeout），不会出现两个都检查通过
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from guard.config import ROOT, env

BUSY_TIMEOUT_SECONDS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,          -- unix 秒，便于做时间窗口查询
    created_at  TEXT NOT NULL,          -- UTC ISO 时间，给人看
    url         TEXT NOT NULL,
    purpose     TEXT,
    network     TEXT,
    asset       TEXT,
    amount      TEXT,                   -- 报价原始金额（XRP 为 drops）
    amount_xrp  TEXT,                   -- 换算成 XRP 的十进制字符串，仅展示用
    pay_to      TEXT,
    invoice_id  TEXT,
    verdict     TEXT NOT NULL,          -- AUTO / HOLD / DENY
    rule_id     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL,          -- evaluated / paid / pay_failed / aborted / denied
                                        -- pending_approval / approved_paid / rejected / expired
    tx_hash     TEXT,
    error       TEXT,
    risk_json   TEXT                    -- 风险解释（risk_explainer），只读信息
);
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    url         TEXT NOT NULL,
    purpose     TEXT,
    quote_json  TEXT NOT NULL,          -- 完整 PaymentRequirements
    rule_id     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / approving / approved / rejected / expired
    decided_at  TEXT,
    tx_hash     TEXT,                   -- 金库付款交易
    result_json TEXT,                   -- 付款后拿到的资源内容，交给 agent
    reject_reason TEXT,
    last_error  TEXT
);
CREATE TABLE IF NOT EXISTS payment_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    url         TEXT NOT NULL,
    purpose     TEXT,
    state       TEXT NOT NULL,          -- 见 state_machine.py
    verdict     TEXT,
    rule_id     TEXT,
    reason      TEXT,
    quote_json  TEXT,
    quote_hash  TEXT,                   -- 报价关键条款的哈希（不含每次都变的 invoiceId）
    amount_drops INTEGER,
    pay_to      TEXT,
    decision_id INTEGER,
    approval_id INTEGER,
    tx_hash     TEXT,                   -- 签名后、提交前就记下
    last_ledger_sequence INTEGER,
    submitted_at REAL,                  -- 真正把签名交易发出去的时间（R6 只统计这些）
    result_json TEXT,
    error       TEXT,
    risk_json   TEXT,
    method      TEXT NOT NULL DEFAULT 'x402'   -- x402（先付款后拿货）/ escrow（锁款、验货后放款）/ topup（金库给热钱包补款）
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_requests_agent_request
    ON payment_requests(agent_id, request_id);
CREATE TABLE IF NOT EXISTS signing_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id INTEGER NOT NULL,
    payment_request_id INTEGER NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    signer_account TEXT NOT NULL,       -- 付款账户（金库）
    signing_pub_key TEXT NOT NULL,      -- 设备公钥（金库的 Regular Key）
    unsigned_hex TEXT NOT NULL,         -- encode(tx)，交给设备签名的字节
    status      TEXT NOT NULL,          -- pending / signed / rejected / expired
    signed_blob TEXT,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS escrows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_request_id INTEGER NOT NULL UNIQUE REFERENCES payment_requests(id),
    approval_id INTEGER,
    created_at  REAL NOT NULL,
    owner       TEXT NOT NULL,          -- 锁款账户（金库）
    destination TEXT NOT NULL,          -- 商户
    amount_drops INTEGER NOT NULL,
    condition   TEXT NOT NULL,          -- PREIMAGE-SHA-256 条件（公开，写在链上）
    preimage    TEXT NOT NULL,          -- 原像（守卫保密保存；拿到它的人就能放款，绝不返回给 API / agent）
    cancel_after INTEGER NOT NULL,      -- ripple 时间，过了这个时间才能 EscrowCancel
    description TEXT,                   -- 报价里商户声明的数据描述
    data_schema TEXT,                   -- 报价里声明的数据结构（确定性检查用）
    create_tx   TEXT,
    offer_sequence INTEGER,             -- EscrowCreate 的 Sequence，Finish / Cancel 用它定位
    delivery_json TEXT,                 -- 商户交付的内容
    verification_json TEXT,             -- 验货结论（pass/fail、理由、证据、各项检查）
    verification_hash TEXT,             -- 验货结论的 sha256，写进 Finish / Cancel 的 Memo
    finish_tx   TEXT,
    cancel_tx   TEXT,
    status      TEXT NOT NULL           -- preparing / locked / delivered / releasing / released / refund_pending / refunding / refunded
);
CREATE TABLE IF NOT EXISTS missions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    task        TEXT NOT NULL,
    status      TEXT NOT NULL,          -- running / done / failed / cancelled
    report      TEXT,
    finished_at REAL,
    source      TEXT NOT NULL DEFAULT 'cli',   -- web（控制台发起）/ cli（命令行 / demo_run）
    pid         INTEGER                 -- agent 进程号：判断任务是否真的还在运行
);
CREATE TABLE IF NOT EXISTS llm_status (
    source      TEXT PRIMARY KEY,       -- risk_explainer / delivery_verifier / agent
    ts          REAL NOT NULL,
    ok          INTEGER NOT NULL,
    kind        TEXT,                   -- timeout / network / rate_limit / overloaded / auth / quota …
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS policy_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    created_at  TEXT NOT NULL,
    field       TEXT NOT NULL,          -- policy.yaml 里的键，如 auto_limit_xrp
    old_value   TEXT NOT NULL,          -- XRP
    new_value   TEXT NOT NULL,
    source      TEXT NOT NULL           -- console / test …
);
CREATE TABLE IF NOT EXISTS mission_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  INTEGER NOT NULL REFERENCES missions(id),
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,          -- text / tool_call / tool_result
    name        TEXT,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_request_id INTEGER NOT NULL UNIQUE REFERENCES payment_requests(id),
    day         TEXT NOT NULL,          -- UTC 日期，日预算按天统计
    kind        TEXT NOT NULL,          -- auto / approved
    amount_drops INTEGER NOT NULL,
    status      TEXT NOT NULL,          -- reserved / spent / released
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL           -- 超时仍停在付款前阶段则释放
);
"""

# 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER
_ADDED_COLUMNS = {
    "decisions": ["risk_json TEXT"],
    "payment_requests": ["method TEXT NOT NULL DEFAULT 'x402'"],
    "missions": ["source TEXT NOT NULL DEFAULT 'cli'", "pid INTEGER"],
    "approvals": ["decided_at TEXT", "tx_hash TEXT", "result_json TEXT", "reject_reason TEXT", "last_error TEXT",
                  "quote_hash TEXT", "expires_at REAL", "payment_request_id INTEGER", "risk_json TEXT",
                  "snapshot_json TEXT"],   # 审批人看到并批准的完整报价快照（含验收条件），验货只用它,
}

_initialized: set[Path] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    return ROOT / env("GUARD_DB_PATH", "data/agentguard.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    _ensure_schema(path)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def immediate() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE 事务：块内的读和写对其他写者是原子的。"""
    conn = connect()
    conn.isolation_level = None   # 手动管理事务
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _ensure_schema(path: Path) -> None:
    if path in _initialized:
        return
    with _init_lock:
        if path in _initialized:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # 读不阻塞写：MCP、API 两个进程共用这个库
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        conn.close()
        _initialized.add(path)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column.split()[0] not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
