"""决策日志：确认（confirm）带并发守卫 —— 旧水位线的迟到任务不能改写已确认决策。

规则：
- 决策必须引用一个已存在的 analysis_run（结论由此绑定数据水位 + 指标版本 + 方法版本）。
- 同一 (experiment, metric) 下，新确认决策的 run.as_of_seq 不得早于当前已确认决策的
  run.as_of_seq；否则拒绝并记入 decision_attempts 审计表（HTTP 层映射为 409）。
- 合法的新决策把旧决策置为 superseded（历史保留，不物理删除）。
- 检查与写入在同一 BEGIN IMMEDIATE 事务内，并行确认不会双双成功。
"""
from __future__ import annotations

import time

VALID_DECISIONS = {"stop_efficacy", "stop_futility", "continue", "ship", "rollback"}


class StaleDecisionError(Exception):
    """迟到任务试图用过期数据水位确认决策。"""

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


def confirm_decision(conn, *, experiment_id: str, metric_id: str, run_id: int,
                     decision: str, rationale: str = "",
                     decided_by: str = "") -> dict:
    if decision not in VALID_DECISIONS:
        raise ValueError(f"invalid decision: {decision}")
    now = time.time()
    # BEGIN IMMEDIATE：立刻拿写锁，检查-插入原子化，挡住并行确认竞争
    conn.execute("BEGIN IMMEDIATE")
    try:
        run = conn.execute(
            "SELECT * FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["experiment_id"] != experiment_id \
                or run["metric_id"] != metric_id:
            conn.execute(
                "INSERT INTO decision_attempts(experiment_id,metric_id,run_id,"
                "decision,reason,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (experiment_id, metric_id, run_id, decision, "run_mismatch",
                 "run 不存在或与实验/指标不匹配", now))
            conn.commit()
            raise ValueError(f"run {run_id} 不属于该实验/指标")

        latest = conn.execute(
            "SELECT d.decision_id, d.decision, d.run_id, r.as_of_seq"
            " FROM decisions d JOIN analysis_runs r ON r.run_id=d.run_id"
            " WHERE d.experiment_id=? AND d.metric_id=? AND d.status='confirmed'"
            " ORDER BY d.decision_id DESC LIMIT 1",
            (experiment_id, metric_id)).fetchone()

        if latest is not None and run["as_of_seq"] < latest["as_of_seq"]:
            detail = (f"该 run 的数据水位 seq={run['as_of_seq']} 早于已确认决策"
                      f"(decision_id={latest['decision_id']})的水位 "
                      f"seq={latest['as_of_seq']}；迟到任务不能改写已确认决策")
            conn.execute(
                "INSERT INTO decision_attempts(experiment_id,metric_id,run_id,"
                "decision,reason,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (experiment_id, metric_id, run_id, decision, "stale_snapshot",
                 detail, now))
            conn.commit()
            raise StaleDecisionError(detail, {
                "run_as_of_seq": run["as_of_seq"],
                "confirmed_as_of_seq": latest["as_of_seq"],
                "confirmed_decision_id": latest["decision_id"]})

        if latest is not None:
            conn.execute("UPDATE decisions SET status='superseded'"
                         " WHERE decision_id=?", (latest["decision_id"],))
        cur = conn.execute(
            "INSERT INTO decisions(experiment_id,metric_id,run_id,decision,"
            "rationale,decided_by,status,created_at) VALUES(?,?,?,?,?,?, 'confirmed',?)",
            (experiment_id, metric_id, run_id, decision, rationale, decided_by, now))
        conn.commit()
    except Exception:
        conn.rollback() if conn.in_transaction else None
        raise
    return get_decision(conn, cur.lastrowid)


def get_decision(conn, decision_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM decisions WHERE decision_id=?",
                             (decision_id,)).fetchone())


def list_decisions(conn, exp_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT d.*, r.as_of_seq AS run_as_of_seq, r.metric_version,"
        " r.method_version FROM decisions d"
        " JOIN analysis_runs r ON r.run_id=d.run_id"
        " WHERE d.experiment_id=? ORDER BY d.decision_id", (exp_id,)).fetchall()
    return [dict(r) for r in rows]


def list_attempts(conn, exp_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decision_attempts WHERE experiment_id=? ORDER BY id",
        (exp_id,)).fetchall()
    return [dict(r) for r in rows]
