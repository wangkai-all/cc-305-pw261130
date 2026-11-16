"""存储层：SQLite（WAL）。事件 append-only；撤销是 tombstone 事件，不物理改行。

核心不变量：
- events.seq 是全局摄入水位线（autoincrement）。任何"截至水位线 W"的查询
  （seq <= W，撤销也以撤销事件的 seq <= W 生效）都是可复算的，与并发摄入无关。
- analysis_runs / decisions 只增不改（decisions 仅允许 confirmed -> superseded）。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS experiments (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  variants          TEXT NOT NULL,          -- json: ["control","treatment"]
  start_time      REAL NOT NULL,        -- 观察窗起点（epoch 秒），永不重置
  planned_days    INTEGER NOT NULL,
  planned_daily_n INTEGER NOT NULL,     -- 每组计划日流量
  baseline_p      REAL NOT NULL,
  alpha           REAL NOT NULL,
  mde_abs         REAL NOT NULL,        -- 最小感兴趣效应（绝对差）
  tau             REAL NOT NULL,        -- mSPRT 混合先验尺度
  spending        TEXT NOT NULL,        -- obf | pocock（组序贯对照方案）
  status          TEXT NOT NULL DEFAULT 'running',  -- running | stopped | concluded
  created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,  -- 摄入水位线
  event_id        TEXT NOT NULL UNIQUE,
  experiment_id   TEXT NOT NULL,
  event_type      TEXT NOT NULL,        -- exposure | conversion | revoke | allocation_change
  subject_id      TEXT,
  variant         TEXT,
  event_time      REAL NOT NULL,        -- 事件发生时间（业务时间）
  ingest_time     REAL NOT NULL,        -- 到达时间
  target_event_id TEXT,                 -- revoke 指向的原事件
  payload         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_exp ON events(experiment_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_target ON events(target_event_id, seq);

CREATE TABLE IF NOT EXISTS metric_defs (
  metric_id   TEXT NOT NULL,
  version     INTEGER NOT NULL,
  definition  TEXT NOT NULL,            -- json，见 metrics 说明
  note        TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  PRIMARY KEY (metric_id, version)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
  run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id  TEXT NOT NULL,
  metric_id      TEXT NOT NULL,
  metric_version INTEGER NOT NULL,
  method_version TEXT NOT NULL,         -- 算法版本绑定
  params         TEXT NOT NULL,         -- json: alpha/tau/spending/planned...
  as_of_seq      INTEGER NOT NULL,      -- 数据水位线（结论绑定的数据快照）
  as_of_time     REAL NOT NULL,
  result         TEXT NOT NULL,         -- json
  created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_exp ON analysis_runs(experiment_id, run_id);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  metric_id     TEXT NOT NULL,
  run_id        INTEGER NOT NULL REFERENCES analysis_runs(run_id),
  decision      TEXT NOT NULL,   -- stop_efficacy|stop_futility|continue|ship|rollback
  rationale     TEXT NOT NULL DEFAULT '',
  decided_by    TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed | superseded
  created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_attempts (  -- 被拒绝的确认（审计）
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL,
  metric_id     TEXT NOT NULL,
  run_id        INTEGER,
  decision      TEXT NOT NULL,
  reason        TEXT NOT NULL,
  detail        TEXT NOT NULL DEFAULT '',
  created_at    REAL NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- 实验与指标

def create_experiment(conn, *, exp_id, name, variants, start_time, planned_days,
                      planned_daily_n, baseline_p, alpha=0.05, mde_abs, tau=None,
                      spending="obf") -> dict:
    now = time.time()
    if tau is None:
        tau = mde_abs  # 默认混合尺度 = MDE（对 MDE 量级效应最敏感）
    conn.execute(
        "INSERT INTO experiments(id,name,variants,start_time,planned_days,"
        "planned_daily_n,baseline_p,alpha,mde_abs,tau,spending,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,'running',?)",
        (exp_id, name, json.dumps(variants), start_time, planned_days,
         planned_daily_n, baseline_p, alpha, mde_abs, tau, spending, now))
    conn.commit()
    return get_experiment(conn, exp_id)


def get_experiment(conn, exp_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["variants"] = json.loads(d["variants"])
    return d


def list_experiments(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM experiments ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["variants"] = json.loads(d["variants"])
        out.append(d)
    return out


def set_experiment_status(conn, exp_id: str, status: str) -> None:
    conn.execute("UPDATE experiments SET status=? WHERE id=?", (status, exp_id))
    conn.commit()


def add_metric_version(conn, metric_id: str, definition: dict, note: str = "") -> int:
    """指标定义换版：追加新版本，旧版本及绑定它的历史 run 不受影响。"""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM metric_defs WHERE metric_id=?",
        (metric_id,)).fetchone()
    version = (row["v"] or 0) + 1
    conn.execute(
        "INSERT INTO metric_defs(metric_id,version,definition,note,created_at)"
        " VALUES(?,?,?,?,?)",
        (metric_id, version, json.dumps(definition, ensure_ascii=False), note,
         time.time()))
    conn.commit()
    return version


def get_metric_def(conn, metric_id: str, version: int | None = None) -> dict | None:
    if version is None:
        row = conn.execute(
            "SELECT * FROM metric_defs WHERE metric_id=? ORDER BY version DESC LIMIT 1",
            (metric_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM metric_defs WHERE metric_id=? AND version=?",
            (metric_id, version)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["definition"] = json.loads(d["definition"])
    return d


def list_metrics(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM metric_defs ORDER BY metric_id, version").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["definition"] = json.loads(d["definition"])
        out.append(d)
    return out


# ---------------------------------------------------------------- 事件摄入

def ingest_events(conn, items: list[dict]) -> tuple[int, int]:
    """批量摄入（单事务）。item 字段见下；ingest_time 缺省为 now（仿真可显式指定）。

    返回 (first_seq, last_seq)。revoke 事件通过 target_event_id 指向原事件。
    """
    now = time.time()
    first = last = None
    with conn:  # 单事务，保证水位线连续
        for it in items:
            cur = conn.execute(
                "INSERT INTO events(event_id,experiment_id,event_type,subject_id,"
                "variant,event_time,ingest_time,target_event_id,payload)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (it.get("event_id") or uuid.uuid4().hex,
                 it["experiment_id"], it["event_type"], it.get("subject_id"),
                 it.get("variant"), float(it["event_time"]),
                 float(it.get("ingest_time", now)), it.get("target_event_id"),
                 json.dumps(it.get("payload") or {}, ensure_ascii=False)))
            seq = cur.lastrowid
            if first is None:
                first = seq
            last = seq
    return (first or 0, last or 0)


def current_watermark(conn) -> int:
    row = conn.execute("SELECT MAX(seq) AS s FROM events").fetchone()
    return row["s"] or 0


def allocation_epochs(conn, exp_id: str) -> list[dict]:
    """分流 epoch 列表（仅记录与展示；观察窗不随 epoch 重置）。"""
    rows = conn.execute(
        "SELECT seq,event_time,payload FROM events WHERE experiment_id=?"
        " AND event_type='allocation_change' ORDER BY seq", (exp_id,)).fetchall()
    return [{"seq": r["seq"], "event_time": r["event_time"],
             "weights": json.loads(r["payload"]).get("weights", {})} for r in rows]
