"""合成演示：从零造两个实验的完整生命周期，展示两条决策路径。

  实验 A（checkout-cta-copy）：真实提升 +1.5pp —— mSPRT 提前越界 -> 确认提前停
    -> 之后一个"迟到 worker"拿着旧水位线的 run 试图确认 continue -> 被拒绝并留痕。
  实验 B（reco-ranker-v2）：真实效应 ~0 —— 每日分析始终未越界 -> 中途切流量(50/50->65/35)
    -> 指标定义换版(v1->v2) -> 到计划终点确认 stop_futility（措辞：未检出 ≥MDE 效应）。
  全程混入迟到事件（转化延迟 1-3 天到达）与撤销（欺诈订单 tombstone），
  演示"迟到/撤销参与重算"：终点后再补一批迟到数据，重跑分析，对比前后结论。

用法：python3 -m exp_platform.seed_demo --db demo.db
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import random
import time

from . import analysis, decisions, storage

DAY = 86400.0


def _conv_payload(rng: random.Random) -> dict:
    # 30% 小额测试单（amount<10），其余正常订单
    amount = round(rng.uniform(1, 9.9), 2) if rng.random() < 0.3 \
        else round(rng.uniform(10, 80), 2)
    return {"amount": amount}


def _gen_day(rng, exp_id, day, day_start, weights, n_daily, p_by_variant,
             late_in, conv_ids_by_day):
    """生成一天的事件：曝光当日到达；转化 8% 迟到 1-3 天；返回当日摄入列表。"""
    items = []
    conv_ids_by_day[day] = []
    for variant, weight in weights.items():
        n = int(n_daily * weight)
        for i in range(n):
            sid = f"{exp_id}-d{day}-{variant[0]}{i}"
            t_exp = day_start + rng.uniform(0, DAY * 0.9)
            items.append({"experiment_id": exp_id, "event_type": "exposure",
                          "subject_id": sid, "variant": variant,
                          "event_time": t_exp, "ingest_time": t_exp + 5})
            if rng.random() < p_by_variant[variant]:
                ev = {"experiment_id": exp_id, "event_type": "conversion",
                      "subject_id": sid, "variant": variant,
                      "event_time": t_exp + rng.uniform(60, 7200),
                      "payload": _conv_payload(rng),
                      "event_id": f"conv-{sid}"}
                if rng.random() < 0.08:  # 迟到：1-3 天后才到达
                    lag = rng.randint(1, 3)
                    ev["ingest_time"] = day_start + lag * DAY + rng.uniform(0, 3600)
                    late_in.setdefault(day + lag, []).append(ev)
                else:
                    ev["ingest_time"] = ev["event_time"] + 5
                    items.append(ev)
                conv_ids_by_day[day].append(ev["event_id"])
    return items


def _digest(run):
    r = run["result"]
    cur = r["current"]
    gsd = r["gsd"]
    return (f"seq={run['as_of_seq']:>5} n={cur.get('n_c', 0):>6}/{cur.get('n_t', 0):<6} "
            f"θ̂={cur.get('theta', 0) * 100:+.2f}pp z={cur.get('z', 0):+.2f} "
            f"p_av={r['msprt']['p_always_valid']:.4f} "
            f"t={gsd['t']:.2f} u={gsd['u_k'] if gsd['u_k'] else float('nan'):.2f} "
            f"-> {r['recommendation']}")


def simulate(db_path: str) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    conn = storage.connect(db_path)
    storage.init_db(conn)
    rng = random.Random(20261116)

    now = time.time()
    start = now - 15 * DAY

    # ---- 指标定义：v1 任意购买；v2 剔除小额测试单（第 8 天换版）
    storage.add_metric_version(conn, "purchase", {
        "unit": "subject", "denominator": "exposed_subjects",
        "numerator": {"event_type": "conversion", "where": []},
        "description": "购买转化率：任意购买事件即算转化"}, note="v1 初始定义")

    # ---- 实验 A：真实 +1.5pp，预计提前停
    storage.create_experiment(
        conn, exp_id="exp-a", name="checkout-cta-copy（结算按钮文案）",
        variants=["control", "treatment"], start_time=start, planned_days=14,
        planned_daily_n=1500, baseline_p=0.12, alpha=0.05, mde_abs=0.01,
        spending="obf")
    # ---- 实验 B：真实 ~0，走到计划终点
    storage.create_experiment(
        conn, exp_id="exp-b", name="reco-ranker-v2（推荐排序模型）",
        variants=["control", "treatment"], start_time=start, planned_days=14,
        planned_daily_n=1200, baseline_p=0.08, alpha=0.05, mde_abs=0.008,
        spending="obf")

    late_in: dict[int, list] = {}
    conv_ids: dict[str, dict[int, list]] = {"exp-a": {}, "exp-b": {}}
    revoked: set[str] = set()
    a_stopped = False
    a_day5_seq = None
    b_weights = {"control": 0.5, "treatment": 0.5}

    print("=" * 100)
    print("逐日摄入 + 每日分析（A: 真实 +1.5pp；B: 真实 ~0）")
    print("=" * 100)
    for day in range(15):
        day_start = start + day * DAY
        batch = list(late_in.pop(day, []))  # 先到达前几天迟到的件
        # 撤销：前一天 ~1.5% 的转化被判欺诈（tombstone）
        for exp_id in ("exp-a", "exp-b"):
            for cid in conv_ids[exp_id].get(day - 1, []):
                if cid not in revoked and rng.random() < 0.015:
                    revoked.add(cid)
                    batch.append({"experiment_id": exp_id, "event_type": "revoke",
                                  "target_event_id": cid,
                                  "event_time": day_start + 100,
                                  "ingest_time": day_start + 100,
                                  "payload": {"reason": "fraud"}})
        if not a_stopped:
            batch += _gen_day(rng, "exp-a", day, day_start,
                              {"control": 0.5, "treatment": 0.5}, 1500,
                              {"control": 0.12, "treatment": 0.135},
                              late_in, conv_ids["exp-a"])
        batch += _gen_day(rng, "exp-b", day, day_start, b_weights, 1200,
                          {"control": 0.08, "treatment": 0.0804},
                          late_in, conv_ids["exp-b"])
        _, seq = storage.ingest_events(conn, batch)

        if day == 4 and not a_stopped:
            a_day5_seq = seq  # 记录第 5 天的水位线，后面模拟"迟到 worker"
        if day == 4:
            storage.ingest_events(conn, [{
                "experiment_id": "exp-b", "event_type": "allocation_change",
                "event_time": day_start, "ingest_time": day_start,
                "payload": {"weights": {"control": 0.65, "treatment": 0.35},
                            "note": "对照组业务活动需要更多流量"}}])
            b_weights = {"control": 0.65, "treatment": 0.35}
            print(f"[day {day:>2}] B 切流量 50/50 -> 65/35（观察窗不重置）")
        if day == 7:
            storage.add_metric_version(conn, "purchase", {
                "unit": "subject", "denominator": "exposed_subjects",
                "numerator": {"event_type": "conversion",
                              "where": [{"field": "amount", "op": ">=", "value": 10}]},
                "description": "购买转化率：剔除 amount<10 的小额测试单"},
                note="v2 剔除小额测试单")
            print(f"[day {day:>2}] 指标 purchase 换版 v1 -> v2（旧 run 仍绑定 v1）")

        run_a = run_b = None
        if not a_stopped:
            run_a = analysis.run_analysis(conn, "exp-a", "purchase")
            print(f"[day {day:>2}] A {_digest(run_a)}")
        run_b = analysis.run_analysis(conn, "exp-b", "purchase")
        print(f"[day {day:>2}] B {_digest(run_b)}")

        if day == 2 and run_a:
            decisions.confirm_decision(
                conn, experiment_id="exp-a", metric_id="purchase",
                run_id=run_a["run_id"], decision="continue",
                rationale="第 3 天例行检查：未越界，按方案继续",
                decided_by="pm-li")
        if day == 6 and run_b:
            decisions.confirm_decision(
                conn, experiment_id="exp-b", metric_id="purchase",
                run_id=run_b["run_id"], decision="continue",
                rationale="第一周例行检查：未越界，按方案继续",
                decided_by="pm-li")
        if run_a and run_a["result"]["recommendation"] == "stop_efficacy":
            decisions.confirm_decision(
                conn, experiment_id="exp-a", metric_id="purchase",
                run_id=run_a["run_id"], decision="stop_efficacy",
                rationale="mSPRT 越界且置信序列整体>0，提前停；逐步放量处理组",
                decided_by="pm-li")
            storage.set_experiment_status(conn, "exp-a", "stopped")
            storage.ingest_events(conn, [{
                "experiment_id": "exp-a", "event_type": "allocation_change",
                "event_time": day_start + 3600, "ingest_time": day_start + 3600,
                "payload": {"weights": {"control": 1.0, "treatment": 0.0},
                            "note": "提前停：新流量不再进实验"}}])
            a_stopped = True
            print(f"           >>> A 第 {day} 天确认 stop_efficacy，停止实验流量")

    # ---- A：迟到 worker 用第 5 天的旧水位线跑分析并试图确认 continue
    print("-" * 100)
    print("并发与守卫演示")
    print("-" * 100)
    stale_run = analysis.run_analysis(conn, "exp-a", "purchase",
                                      as_of_seq=a_day5_seq)
    try:
        decisions.confirm_decision(
            conn, experiment_id="exp-a", metric_id="purchase",
            run_id=stale_run["run_id"], decision="continue",
            rationale="（迟到的定时任务）", decided_by="cron-worker-7")
        print("!! 守卫失效：旧水位线确认成功了")
    except decisions.StaleDecisionError as e:
        print(f"迟到 worker 持旧水位 seq={a_day5_seq} 试图确认 continue -> 409 拒绝："
              f"{e}")

    # ---- B：并行分析任务（3 个线程同时跑），run 各自绑定各自水位
    def _parallel_run(_):
        c = storage.connect(db_path)
        try:
            return analysis.run_analysis(c, "exp-b", "purchase")["run_id"]
        finally:
            c.close()
    with cf.ThreadPoolExecutor(3) as pool:
        ids = list(pool.map(_parallel_run, range(3)))
    print(f"3 个并行分析任务完成，run_id={ids}（各自独立、不可变）")

    # ---- B：终点后再到一批迟到转化 + 撤销，重跑分析（迟到/撤销参与重算）
    print("-" * 100)
    print("迟到/撤销重算演示（实验 B）")
    print("-" * 100)
    before = analysis.run_analysis(conn, "exp-b", "purchase")
    tail_batch = list(late_in.pop(15, [])) + list(late_in.pop(16, [])) \
        + list(late_in.pop(17, []))
    extra_conv = 0
    day13 = start + 13 * DAY
    for i in range(40):  # 第 13 天的转化，迟了 2 天才到
        sid = f"exp-b-d13-t{9000 + i}"
        storage.ingest_events(conn, [{
            "experiment_id": "exp-b", "event_type": "exposure", "subject_id": sid,
            "variant": "treatment", "event_time": day13 + i * 100,
            "ingest_time": day13 + i * 100}])
        cid = f"conv-{sid}"
        storage.ingest_events(conn, [{
            "experiment_id": "exp-b", "event_type": "conversion", "subject_id": sid,
            "variant": "treatment", "event_time": day13 + i * 100 + 60,
            "ingest_time": time.time(), "event_id": cid,
            "payload": {"amount": 25.0}}])
        extra_conv += 1
        if i < 6:  # 其中 6 单随后被判欺诈撤销
            storage.ingest_events(conn, [{
                "experiment_id": "exp-b", "event_type": "revoke",
                "target_event_id": cid, "event_time": time.time(),
                "ingest_time": time.time(), "payload": {"reason": "fraud"}}])
    storage.ingest_events(conn, tail_batch)
    after = analysis.run_analysis(conn, "exp-b", "purchase")
    cb, ca = before["result"]["current"], after["result"]["current"]
    print(f"重算前 run#{before['run_id']}: x={cb['x_c']}/{cb['x_t']} "
          f"θ̂={cb['theta'] * 100:+.3f}pp p_av={before['result']['msprt']['p_always_valid']:.4f}")
    print(f"重算后 run#{after['run_id']}: x={ca['x_c']}/{ca['x_t']} "
          f"θ̂={ca['theta'] * 100:+.3f}pp p_av={after['result']['msprt']['p_always_valid']:.4f}"
          f"  （补入 {extra_conv} 笔迟到转化、6 笔撤销、{len(tail_batch)} 件滞留事件）")

    decisions.confirm_decision(
        conn, experiment_id="exp-b", metric_id="purchase",
        run_id=after["run_id"], decision="stop_futility",
        rationale="到达计划终点未越界；置信序列落在 ±MDE 附近，未检出 ≥0.8pp 的效应"
                  "（不宣称无效应）。回滚处理组。",
        decided_by="pm-li")
    storage.set_experiment_status(conn, "exp-b", "concluded")

    print("=" * 100)
    print("决策日志")
    print("=" * 100)
    for exp_id in ("exp-a", "exp-b"):
        for d in decisions.list_decisions(conn, exp_id):
            print(f"  {exp_id} #{d['decision_id']} [{d['status']:>10}] {d['decision']:<14}"
                  f" run={d['run_id']}(seq={d['run_as_of_seq']}, "
                  f"metric@{d['metric_version']}, {d['method_version']}) by {d['decided_by']}")
        for a in decisions.list_attempts(conn, exp_id):
            print(f"  {exp_id}   [被拒绝] {a['decision']:<14} run={a['run_id']} "
                  f"原因={a['reason']}: {a['detail'][:60]}...")
    print(f"\n完成。启动页面：python3 -m exp_platform.server --db {db_path} --port 8321")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="demo.db")
    args = ap.parse_args()
    simulate(args.db)


if __name__ == "__main__":
    main()
