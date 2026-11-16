"""数据流水线：迟到/撤销重算、观察窗不重置、决策守卫、指标版本绑定、并行 run。

运行：python3 -m unittest tests.test_pipeline -v
"""
import concurrent.futures as cf
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exp_platform import analysis, decisions, storage

DAY = 86400.0


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = storage.connect(path)
    storage.init_db(conn)
    return conn, path


def make_exp(conn, start):
    storage.add_metric_version(conn, "purchase", {
        "unit": "subject", "denominator": "exposed_subjects",
        "numerator": {"event_type": "conversion", "where": []}})
    return storage.create_experiment(
        conn, exp_id="e1", name="测试实验", variants=["control", "treatment"],
        start_time=start, planned_days=7, planned_daily_n=100,
        baseline_p=0.10, alpha=0.05, mde_abs=0.02)


def expose(conn, exp_id, day0, n=100, conv_rate=0.10, conv_time_offset=60.0):
    """一天的事件：n/组曝光，部分转化。返回 (conversion_event_ids, last_seq)。"""
    items, cids = [], []
    for variant in ("control", "treatment"):
        for i in range(n):
            sid = f"{exp_id}-{variant}-{day0:.0f}-{i}"
            items.append({"experiment_id": exp_id, "event_type": "exposure",
                          "subject_id": sid, "variant": variant,
                          "event_time": day0 + i})
            if i < n * conv_rate:
                cid = f"conv-{sid}"
                cids.append(cid)
                items.append({"experiment_id": exp_id, "event_type": "conversion",
                              "subject_id": sid, "variant": variant,
                              "event_time": day0 + i + conv_time_offset,
                              "event_id": cid, "payload": {"amount": 20}})
    _, seq = storage.ingest_events(conn, items)
    return cids, seq


class TestLateAndRevoke(unittest.TestCase):
    def test_late_and_revoke_recompute(self):
        conn, path = make_db()
        start = time.time() - 3 * DAY
        make_exp(conn, start)
        cids, seq1 = expose(conn, "e1", start + 3600)
        run1 = analysis.run_analysis(conn, "e1", "purchase")
        x1 = run1["result"]["current"]["x_c"]

        # 迟到转化：发生在第一天，现在才到（subject 须为已曝光用户）
        late_sid = f"e1-control-{start + 3600:.0f}-50"
        storage.ingest_events(conn, [{
            "experiment_id": "e1", "event_type": "conversion",
            "subject_id": late_sid, "variant": "control",
            "event_time": start + 3600 + 50, "event_id": "late-conv-1",
            "payload": {"amount": 30}}])
        run2 = analysis.run_analysis(conn, "e1", "purchase")
        self.assertEqual(run2["result"]["current"]["x_c"], x1 + 1)

        # 撤销该迟到转化：重算后回到 x1
        storage.ingest_events(conn, [{
            "experiment_id": "e1", "event_type": "revoke",
            "target_event_id": "late-conv-1", "event_time": time.time()}])
        run3 = analysis.run_analysis(conn, "e1", "purchase")
        self.assertEqual(run3["result"]["current"]["x_c"], x1)

        # run 不可变：历史 run 的结果不被改写
        run1_again = analysis.get_run(conn, run1["run_id"])
        self.assertEqual(run1_again["result"]["current"]["x_c"], x1)
        self.assertEqual(run1_again["as_of_seq"], seq1)
        conn.close()
        os.remove(path)


class TestWindowNotReset(unittest.TestCase):
    def test_allocation_change_keeps_window(self):
        conn, path = make_db()
        start = time.time() - 3 * DAY
        exp = make_exp(conn, start)
        expose(conn, "e1", start + 3600)
        # 切流量
        storage.ingest_events(conn, [{
            "experiment_id": "e1", "event_type": "allocation_change",
            "event_time": start + DAY,
            "payload": {"weights": {"control": 0.7, "treatment": 0.3}}}])
        expose(conn, "e1", start + DAY + 3600)
        run = analysis.run_analysis(conn, "e1", "purchase")
        win = run["result"]["data_window"]
        # 观察窗仍从实验起点开始；累计样本包含切流量前的数据
        self.assertEqual(win["start_time"], exp["start_time"])
        self.assertEqual(run["result"]["current"]["n_c"], 200)
        self.assertEqual(run["result"]["current"]["n_t"], 200)
        conn.close()
        os.remove(path)


class TestDecisionGuard(unittest.TestCase):
    def test_stale_rejected_newer_supersedes(self):
        conn, path = make_db()
        start = time.time() - 5 * DAY
        make_exp(conn, start)
        expose(conn, "e1", start + 3600)
        run1 = analysis.run_analysis(conn, "e1", "purchase")
        expose(conn, "e1", start + DAY + 3600)
        run2 = analysis.run_analysis(conn, "e1", "purchase")

        d = decisions.confirm_decision(
            conn, experiment_id="e1", metric_id="purchase",
            run_id=run2["run_id"], decision="continue", decided_by="pm")
        self.assertEqual(d["status"], "confirmed")

        # 旧水位线的 run 不能改写已确认决策
        with self.assertRaises(decisions.StaleDecisionError):
            decisions.confirm_decision(
                conn, experiment_id="e1", metric_id="purchase",
                run_id=run1["run_id"], decision="stop_futility", decided_by="cron")
        attempts = decisions.list_attempts(conn, "e1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["reason"], "stale_snapshot")

        # 更新水位的 run 可以 supersede
        expose(conn, "e1", start + 2 * DAY + 3600)
        run3 = analysis.run_analysis(conn, "e1", "purchase")
        decisions.confirm_decision(
            conn, experiment_id="e1", metric_id="purchase",
            run_id=run3["run_id"], decision="stop_futility", decided_by="pm")
        decs = decisions.list_decisions(conn, "e1")
        self.assertEqual([x["status"] for x in decs], ["superseded", "confirmed"])
        self.assertEqual(decs[1]["decision"], "stop_futility")
        conn.close()
        os.remove(path)

    def test_run_mismatch_rejected(self):
        conn, path = make_db()
        start = time.time() - 2 * DAY
        make_exp(conn, start)
        expose(conn, "e1", start + 3600)
        run1 = analysis.run_analysis(conn, "e1", "purchase")
        with self.assertRaises(ValueError):
            decisions.confirm_decision(
                conn, experiment_id="e1", metric_id="other_metric",
                run_id=run1["run_id"], decision="continue")
        conn.close()
        os.remove(path)


class TestMetricVersionBinding(unittest.TestCase):
    def test_runs_bind_version_and_chain_restarts(self):
        conn, path = make_db()
        start = time.time() - 3 * DAY
        make_exp(conn, start)
        expose(conn, "e1", start + 3600)
        run_v1 = analysis.run_analysis(conn, "e1", "purchase")
        self.assertEqual(run_v1["metric_version"], 1)

        storage.add_metric_version(conn, "purchase", {
            "unit": "subject", "denominator": "exposed_subjects",
            "numerator": {"event_type": "conversion",
                          "where": [{"field": "amount", "op": ">=", "value": 10}]}})
        run_v2 = analysis.run_analysis(conn, "e1", "purchase")
        self.assertEqual(run_v2["metric_version"], 2)
        # 换版后 always-valid 链重启（新假设）
        self.assertEqual(run_v2["result"]["msprt"]["chain_checks"], 1)
        # 显式指定旧版本：仍绑定 v1，且 v1 链延续
        run_v1b = analysis.run_analysis(conn, "e1", "purchase", metric_version=1)
        self.assertEqual(run_v1b["metric_version"], 1)
        self.assertEqual(run_v1b["result"]["msprt"]["chain_checks"], 2)
        conn.close()
        os.remove(path)


class TestParallelRuns(unittest.TestCase):
    def test_concurrent_analyses(self):
        conn, path = make_db()
        start = time.time() - 2 * DAY
        make_exp(conn, start)
        expose(conn, "e1", start + 3600)
        conn.close()

        def one_run(_):
            c = storage.connect(path)
            try:
                return analysis.run_analysis(c, "e1", "purchase")["run_id"]
            finally:
                c.close()

        with cf.ThreadPoolExecutor(4) as pool:
            ids = list(pool.map(one_run, range(4)))
        self.assertEqual(len(set(ids)), 4)  # 各自独立落库
        conn = storage.connect(path)
        self.assertEqual(len(analysis.list_runs(conn, "e1")), 4)
        conn.close()
        os.remove(path)


if __name__ == "__main__":
    unittest.main()
