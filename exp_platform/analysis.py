"""分析层：按水位线可复算地构建数据集，执行序贯检验，产出不可变的 analysis_run。

关键语义：
- 每次 run 先固定水位线 as_of_seq，再计算 —— 计算期间并发摄入不影响本 run。
- 迟到（event_time 早、seq 晚）与撤销（revoke tombstone）随水位线推进自动进入重算。
- 观察窗 = [experiment.start_time, as_of_time]，与分流 epoch 无关，切流量不重置窗口。
- always-valid 链与组序贯检查都建立在"实际发生的 run"上（每次 run 是信息时间上的
  一次检查，绑定当时水位线）；轨迹重放仅用于可视化，不参与决策。
  链按 (experiment, metric_id, metric_version) 维护：指标换版 = 新假设，链重启。
"""
from __future__ import annotations

import json
import time

from . import METHOD_VERSION, statsutil, storage

DAY = 86400.0
LATE_THRESHOLD_SEC = 3600.0  # 到达比发生晚 1 小时以上记为"迟到"


# ---------------------------------------------------------------- 指标求值

def _match_where(payload: dict, where: list[dict]) -> bool:
    ops = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
           "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
           "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
    for cond in where or []:
        val = payload.get(cond["field"])
        try:
            if val is None or not ops[cond["op"]](val, cond["value"]):
                return False
        except TypeError:
            return False
    return True


def _fetch_events(conn, exp_id: str, event_type: str, look_time: float,
                  watermark: int) -> list[dict]:
    """截至 look_time（业务时间）且 seq <= 水位线、且未被（水位线内的）revoke 撤销。"""
    rows = conn.execute(
        "SELECT e.event_id,e.subject_id,e.variant,e.event_time,e.ingest_time,e.payload"
        " FROM events e WHERE e.experiment_id=? AND e.event_type=?"
        " AND e.event_time<=? AND e.seq<=?"
        " AND NOT EXISTS (SELECT 1 FROM events r WHERE r.target_event_id=e.event_id"
        "               AND r.seq<=?)",
        (exp_id, event_type, look_time, watermark, watermark)).fetchall()
    return [dict(r) for r in rows]


def cumulative_counts(conn, exp_id: str, metric_def: dict, look_time: float,
                      watermark: int) -> dict:
    """截至 look_time 的累计：每组暴露去重人数 n、转化去重人数 x。

    转化归属：主体须已暴露（曝光时间 <= 转化时间），按首次曝光定组。
    """
    exposures = _fetch_events(conn, exp_id, "exposure", look_time, watermark)
    conversions = _fetch_events(conn, exp_id, "conversion", look_time, watermark)

    first_exp: dict[str, tuple[str, float]] = {}
    for e in sorted(exposures, key=lambda r: (r["event_time"], r["event_id"])):
        sid = e["subject_id"]
        if sid not in first_exp:
            first_exp[sid] = (e["variant"], e["event_time"])

    num_filter = (metric_def.get("numerator") or {}).get("where") or []
    n_by: dict[str, int] = {}
    x_by: dict[str, int] = {}
    for variant, _ in first_exp.values():
        n_by[variant] = n_by.get(variant, 0) + 1
    converted: set[str] = set()
    for e in sorted(conversions, key=lambda r: (r["event_time"], r["event_id"])):
        sid = e["subject_id"]
        if sid in converted or sid not in first_exp:
            continue
        variant, exp_time = first_exp[sid]
        if e["event_time"] < exp_time:
            continue  # 曝光前的转化不归因
        if not _match_where(json.loads(e["payload"]), num_filter):
            continue
        converted.add(sid)
        x_by[variant] = x_by.get(variant, 0) + 1
    return {"n": n_by, "x": x_by}


# ---------------------------------------------------------------- 检查点与轨迹（可视化用）

def look_times(start_time: float, as_of_time: float, max_looks: int = 60) -> list[float]:
    """日终检查点序列：从 start_time 起每天一个，最后一个为 as_of_time。"""
    looks = []
    t = start_time + DAY
    while t < as_of_time and len(looks) < max_looks - 1:
        looks.append(t)
        t += DAY
    if as_of_time > start_time:
        looks.append(as_of_time)
    return looks


def _evaluate_trajectory(conn, exp: dict, metric_def: dict, looks: list[float],
                         watermark: int) -> list[dict]:
    """按当前水位线重放日终轨迹（含迟到/撤销后的最新数据）。仅供可视化：
    历史点用的是"现在才知道"的数据，不构成序贯决策依据。"""
    control, treatment = exp["variants"][0], exp["variants"][1]
    traj = []
    tau2 = exp["tau"] ** 2
    for lt in looks:
        counts = cumulative_counts(conn, exp["id"], metric_def, lt, watermark)
        n_c = counts["n"].get(control, 0)
        n_t = counts["n"].get(treatment, 0)
        x_c = counts["x"].get(control, 0)
        x_t = counts["x"].get(treatment, 0)
        tp = statsutil.two_prop(x_c, n_c, x_t, n_t)
        if tp is None:
            traj.append({"time": lt, "n_c": n_c, "n_t": n_t, "x_c": x_c, "x_t": x_t,
                         "theta": None, "z": None, "V": None})
            continue
        half = statsutil.cs_halfwidth(tp["V"], tau2, exp["alpha"])
        traj.append({"time": lt, "n_c": n_c, "n_t": n_t, "x_c": x_c, "x_t": x_t,
                     "theta": tp["theta"], "z": tp["z"], "V": tp["V"],
                     "cs_lo": tp["theta"] - half, "cs_hi": tp["theta"] + half,
                     "p_c": tp["p_c"], "p_t": tp["p_t"]})
    return traj


def _planned_precision(exp: dict) -> float:
    """计划终点的信息精度 V_planned（按基线率与计划流量，50/50 设计）。"""
    p0 = exp["baseline_p"]
    n = exp["planned_daily_n"] * exp["planned_days"]
    se2 = 2.0 * p0 * (1.0 - p0) / max(n, 1)
    return 1.0 / max(se2, 1e-12)


def _recommendation(exp: dict, last: dict, p_av: float, gsd: dict,
                    planned_end: bool) -> tuple[str, str]:
    """把统计信号翻译成建议动作。措辞保守：不宣称"无效应"，只说"未检出"。"""
    alpha = exp["alpha"]
    mde = exp["mde_abs"]
    if last.get("theta") is None:
        return "continue", "数据不足，继续观察。"
    if p_av <= alpha:
        if last["theta"] > 0:
            return ("stop_efficacy",
                    f"always-valid p={p_av:.4f} ≤ α={alpha}，置信序列 "
                    f"[{last['cs_lo']:.4f}, {last['cs_hi']:.4f}] 整体位于 0 上方，"
                    f"可提前停并判处理组更优（任意时刻有效性：在任意停时下总体一类错误 ≤ α）。")
        return ("rollback",
                f"always-valid p={p_av:.4f} ≤ α={alpha}，置信序列整体位于 0 下方，"
                f"处理组显著更差，建议止损回滚。")
    if planned_end:
        half = (last["cs_hi"] - last["cs_lo"]) / 2.0
        if last["cs_lo"] > -mde and last["cs_hi"] < mde:
            return ("stop_futility",
                    f"已达计划终点且置信序列 [{last['cs_lo']:.4f}, {last['cs_hi']:.4f}] "
                    f"落在 ±MDE({mde}) 之内：未检出 ≥MDE 的效应（不等于证明无效应）。")
        return ("stop_futility",
                f"已达计划终点但未越序贯边界（p_av={p_av:.4f} > α={alpha}）。"
                f"置信序列 [{last['cs_lo']:.4f}, {last['cs_hi']:.4f}]，半宽 {half:.4f}；"
                f"结论：未检出显著效应（不等于证明无效应）。")
    return ("continue",
            f"未越序贯边界（p_av={p_av:.4f} > α={alpha}），信息量 "
            f"{gsd.get('t', 0.0) * 100:.0f}%，继续观察。")


def run_analysis(conn, exp_id: str, metric_id: str,
                 metric_version: int | None = None,
                 as_of_seq: int | None = None) -> dict:
    """执行一次分析并落库为不可变 run。as_of_seq 缺省取当前水位线。"""
    exp = storage.get_experiment(conn, exp_id)
    if exp is None:
        raise KeyError(f"experiment not found: {exp_id}")
    mdef = storage.get_metric_def(conn, metric_id, metric_version)
    if mdef is None:
        raise KeyError(f"metric not found: {metric_id}@{metric_version}")
    if as_of_seq is None:
        as_of_seq = storage.current_watermark(conn)
    as_of_time = time.time()

    # 当前检查点：截至 as_of_time 的累计（含所有迟到/撤销）
    counts = cumulative_counts(conn, exp_id, mdef["definition"], as_of_time, as_of_seq)
    control, treatment = exp["variants"][0], exp["variants"][1]
    n_c = counts["n"].get(control, 0)
    n_t = counts["n"].get(treatment, 0)
    x_c = counts["x"].get(control, 0)
    x_t = counts["x"].get(treatment, 0)
    tp = statsutil.two_prop(x_c, n_c, x_t, n_t)

    tau2 = exp["tau"] ** 2
    if tp is not None:
        half = statsutil.cs_halfwidth(tp["V"], tau2, exp["alpha"])
        current = {"time": as_of_time, "n_c": n_c, "n_t": n_t, "x_c": x_c, "x_t": x_t,
                   "theta": tp["theta"], "z": tp["z"], "V": tp["V"],
                   "p_c": tp["p_c"], "p_t": tp["p_t"],
                   "cs_lo": tp["theta"] - half, "cs_hi": tp["theta"] + half}
        lambda_now = statsutil.mixture_lr(tp["theta"], tp["V"], tau2)
    else:
        current = {"time": as_of_time, "n_c": n_c, "n_t": n_t, "x_c": x_c,
                   "x_t": x_t, "theta": None, "z": None, "V": None}
        lambda_now = 1.0

    # ---- always-valid 链：同 (实验, 指标, 指标版本) 的历史 run 各是一次检查
    chain_rows = conn.execute(
        "SELECT run_id, result FROM analysis_runs WHERE experiment_id=?"
        " AND metric_id=? AND metric_version=? ORDER BY run_id",
        (exp_id, metric_id, mdef["version"])).fetchall()
    prev_lambdas, prev_ts, prev_zs = [], [], []
    v_planned = _planned_precision(exp)
    for row in chain_rows:
        r = json.loads(row["result"])
        prev_lambdas.append(r["msprt"]["lambda_current"])
        if r["current"].get("V"):
            prev_ts.append(min(1.0, r["current"]["V"] / v_planned))
            prev_zs.append(r["current"]["z"])
    max_lambda = max(prev_lambdas + [lambda_now])
    p_av = min(1.0, 1.0 / max_lambda)

    # ---- 组序贯（预定检查方案）：检查点 = 实际 run 序列，信息比例 = V/V_planned
    t_fracs = prev_ts + ([min(1.0, current["V"] / v_planned)] if current["V"] else [])
    z_seq = prev_zs + ([current["z"]] if current["z"] is not None else [])
    # 信息比例单调化（撤销可能让 V 微降）
    t_mono = []
    for t in t_fracs:
        t_mono.append(max(t, t_mono[-1] if t_mono else 0.0))
    gsd_us = statsutil.gsd_boundaries(t_mono, exp["alpha"], exp["spending"]) \
        if t_mono else []
    gsd_crossed = any(abs(z) >= u for z, u in zip(z_seq, gsd_us))
    gsd = {"spending": exp["spending"], "t": t_mono[-1] if t_mono else 0.0,
           "u_k": gsd_us[-1] if gsd_us else None,
           "looks": [[round(t, 4), round(u, 4)] for t, u in zip(t_mono, gsd_us)],
           "crossed": gsd_crossed, "n_checks": len(t_mono)}

    # ---- 可视化轨迹（当前水位线重放，含迟到数据；非决策依据）
    # 数据地平线：水位线内最大业务时间。轨迹与"计划终点"判定都以数据地平线为准，
    # 而不是真实时钟 —— 否则补录/灌数会把观察窗虚假拉长。
    horizon_row = conn.execute(
        "SELECT MAX(event_time) AS h FROM events WHERE experiment_id=? AND seq<=?"
        " AND event_type IN ('exposure','conversion')",
        (exp_id, as_of_seq)).fetchone()
    data_horizon = horizon_row["h"] or exp["start_time"]
    looks = look_times(exp["start_time"], min(as_of_time, data_horizon))
    traj = _evaluate_trajectory(conn, exp, mdef["definition"], looks, as_of_seq)

    planned_end = (gsd["t"] >= 1.0
                   or data_horizon >= exp["start_time"] + exp["planned_days"] * DAY)
    rec, rec_reason = _recommendation(exp, current, p_av, gsd, planned_end)

    late = late_data_summary(conn, exp_id, as_of_seq)
    prev = _latest_run(conn, exp_id, metric_id)
    since = _since_summary(conn, exp_id, prev["as_of_seq"] if prev else 0, as_of_seq)

    result = {
        "data_window": {"start_time": exp["start_time"], "as_of_time": as_of_time,
                        "as_of_seq": as_of_seq, "looks": len(looks)},
        "variants": exp["variants"],
        "current": current,
        "msprt": {"tau": exp["tau"], "alpha": exp["alpha"],
                  "lambda_current": lambda_now,
                  "chain_checks": len(prev_lambdas) + 1,
                  "p_always_valid": p_av,
                  "crossed": bool(tp is not None and p_av <= exp["alpha"])},
        "gsd": gsd,
        "trajectory": traj,
        "recommendation": rec,
        "recommendation_reason": rec_reason,
        "late_data": late,
        "since_previous_run": since,
    }
    params = {"alpha": exp["alpha"], "tau": exp["tau"], "spending": exp["spending"],
              "mde_abs": exp["mde_abs"], "planned_days": exp["planned_days"],
              "planned_daily_n": exp["planned_daily_n"],
              "baseline_p": exp["baseline_p"]}
    cur = conn.execute(
        "INSERT INTO analysis_runs(experiment_id,metric_id,metric_version,"
        "method_version,params,as_of_seq,as_of_time,result,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (exp_id, metric_id, mdef["version"], METHOD_VERSION,
         json.dumps(params), as_of_seq, as_of_time,
         json.dumps(result, ensure_ascii=False), time.time()))
    conn.commit()
    return get_run(conn, cur.lastrowid)


# ---------------------------------------------------------------- 迟到/撤销统计

def late_data_summary(conn, exp_id: str, watermark: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS late_n FROM events WHERE experiment_id=? AND seq<=?"
        " AND event_type IN ('exposure','conversion')"
        " AND ingest_time - event_time > ?",
        (exp_id, watermark, LATE_THRESHOLD_SEC)).fetchone()
    revoked = conn.execute(
        "SELECT COUNT(*) AS n FROM events r JOIN events e ON r.target_event_id=e.event_id"
        " WHERE r.experiment_id=? AND r.event_type='revoke' AND r.seq<=? AND e.seq<=?",
        (exp_id, watermark, watermark)).fetchone()
    by_day = conn.execute(
        "SELECT date(event_time,'unixepoch') AS d, COUNT(*) AS n FROM events"
        " WHERE experiment_id=? AND seq<=? AND event_type IN ('exposure','conversion')"
        " AND ingest_time - event_time > ? GROUP BY d ORDER BY d",
        (exp_id, watermark, LATE_THRESHOLD_SEC)).fetchall()
    return {"late_events": row["late_n"], "revoked_events": revoked["n"],
            "late_by_event_day": {r["d"]: r["n"] for r in by_day}}


def _since_summary(conn, exp_id: str, prev_seq: int, watermark: int) -> dict:
    """自上一次 run 的水位线以来，新进入本实验数据集的各类事件数。"""
    rows = conn.execute(
        "SELECT event_type, COUNT(*) AS n FROM events WHERE experiment_id=?"
        " AND seq>? AND seq<=? GROUP BY event_type",
        (exp_id, prev_seq, watermark)).fetchall()
    out = {r["event_type"]: r["n"] for r in rows}
    late = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE experiment_id=? AND seq>? AND seq<=?"
        " AND event_type IN ('exposure','conversion')"
        " AND ingest_time - event_time > ?",
        (exp_id, prev_seq, watermark, LATE_THRESHOLD_SEC)).fetchone()
    out["late_arrivals"] = late["n"]
    return out


# ---------------------------------------------------------------- 查询

def _latest_run(conn, exp_id: str, metric_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM analysis_runs WHERE experiment_id=? AND metric_id=?"
        " ORDER BY run_id DESC LIMIT 1", (exp_id, metric_id)).fetchone()
    return _row_to_run(row) if row else None


def get_run(conn, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM analysis_runs WHERE run_id=?",
                       (run_id,)).fetchone()
    return _row_to_run(row) if row else None


def list_runs(conn, exp_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM analysis_runs WHERE experiment_id=? ORDER BY run_id DESC",
        (exp_id,)).fetchall()
    return [_row_to_run(r) for r in rows]


def _row_to_run(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"])
    d["result"] = json.loads(d["result"])
    return d
