"""HTTP 层冒烟测试：真实起服务（临时端口），走完整 API 流程。

运行：python3 -m unittest tests.test_http -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exp_platform import storage
from exp_platform.server import make_handler

DAY = 86400.0


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.db)
        conn = storage.connect(cls.db)
        storage.init_db(conn)
        conn.close()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cls.db + suffix):
                os.remove(cls.db + suffix)

    def test_full_flow(self):
        base = self.base
        start = time.time() - 2 * DAY
        st, _ = call(base, "POST", "/api/metrics/purchase/versions", {
            "definition": {"unit": "subject", "denominator": "exposed_subjects",
                           "numerator": {"event_type": "conversion", "where": []}}})
        self.assertEqual(st, 201)
        st, exp = call(base, "POST", "/api/experiments", {
            "id": "web-e1", "name": "HTTP 测试实验", "start_time": start,
            "planned_days": 7, "planned_daily_n": 50, "baseline_p": 0.1,
            "mde_abs": 0.02})
        self.assertEqual(st, 201)

        events = []
        for variant in ("control", "treatment"):
            for i in range(50):
                sid = f"u-{variant}-{i}"
                events.append({"event_type": "exposure", "subject_id": sid,
                               "variant": variant, "event_time": start + i})
                if i < (5 if variant == "control" else 9):
                    events.append({"event_type": "conversion", "subject_id": sid,
                                   "variant": variant,
                                   "event_time": start + i + 30,
                                   "payload": {"amount": 15}})
        st, r = call(base, "POST", "/api/experiments/web-e1/events",
                     {"events": events})
        self.assertEqual(st, 201)
        self.assertEqual(r["ingested"], len(events))

        st, run = call(base, "POST", "/api/experiments/web-e1/analyses",
                       {"metric_id": "purchase"})
        self.assertEqual(st, 201)
        self.assertEqual(run["metric_version"], 1)
        self.assertEqual(run["current"]["x_t"], 9)

        st, dec = call(base, "POST", "/api/experiments/web-e1/decisions", {
            "metric_id": "purchase", "run_id": run["run_id"],
            "decision": "continue", "rationale": "首次检查", "decided_by": "tester"})
        self.assertEqual(st, 201)

        # 用更旧水位的 run 确认 -> 409
        st, run_old = call(base, "POST", "/api/experiments/web-e1/analyses",
                           {"metric_id": "purchase",
                            "as_of_seq": run["as_of_seq"] - 10})
        self.assertEqual(st, 201)
        st, err = call(base, "POST", "/api/experiments/web-e1/decisions", {
            "metric_id": "purchase", "run_id": run_old["run_id"],
            "decision": "stop_futility", "rationale": "迟到任务"})
        self.assertEqual(st, 409)
        self.assertIn("水位", err["error"])

        # 列表/详情/决策日志/静态页
        st, exps = call(base, "GET", "/api/experiments")
        self.assertEqual(len(exps["experiments"]), 1)
        st, decs = call(base, "GET", "/api/experiments/web-e1/decisions")
        self.assertEqual(len(decs["decisions"]), 1)
        self.assertEqual(len(decs["rejected_attempts"]), 1)
        st, series = call(base, "GET", "/api/experiments/web-e1/series?metric=purchase")
        self.assertEqual(st, 200)
        self.assertTrue(series["days"])
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("实验分析平台", resp.read().decode())


if __name__ == "__main__":
    unittest.main()
