"""HTTP 服务：JSON API + 静态页面。纯标准库（http.server）。

    python3 -m exp_platform.server --db demo.db --port 8321

每个请求独立连接（WAL 模式支持多读单写）；分析/决策的并发语义由
analysis.run_analysis 的水位线快照与 decisions.confirm_decision 的原子守卫保证。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analysis, decisions, storage

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".json": "application/json"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, detail=None):
        super().__init__(message)
        self.status, self.detail = status, detail


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- 工具
        def _conn(self):
            return storage.connect(db_path)

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, path: Path):
            if not path.is_file() or WEB_DIR not in path.resolve().parents:
                self._send_json({"error": "not found"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def log_message(self, fmt, *args):  # 静默
            pass

        # ---------------------------------------------------------- 路由
        def do_GET(self):
            try:
                self._route_get()
            except ApiError as e:
                self._send_json({"error": str(e), "detail": e.detail}, e.status)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            try:
                self._route_post()
            except ApiError as e:
                self._send_json({"error": str(e), "detail": e.detail}, e.status)
            except decisions.StaleDecisionError as e:
                self._send_json({"error": str(e), "detail": e.detail}, 409)
            except (ValueError, KeyError) as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _route_get(self):
            path = self.path.split("?")[0]
            conn = self._conn()
            if path == "/" or path == "/index.html":
                return self._send_static(WEB_DIR / "index.html")
            if path.startswith("/static/"):
                return self._send_static(WEB_DIR / path[len("/static/"):])
            if path == "/api/experiments":
                exps = storage.list_experiments(conn)
                for e in exps:
                    runs = analysis.list_runs(conn, e["id"])
                    e["latest_run"] = _run_digest(runs[0]) if runs else None
                    decs = [d for d in decisions.list_decisions(conn, e["id"])
                            if d["status"] == "confirmed"]
                    e["confirmed_decision"] = decs[-1] if decs else None
                return self._send_json({"experiments": exps})
            m = re.fullmatch(r"/api/experiments/([\w-]+)", path)
            if m:
                exp = storage.get_experiment(conn, m.group(1))
                if not exp:
                    raise ApiError(404, "experiment not found")
                exp["epochs"] = storage.allocation_epochs(conn, exp["id"])
                exp["watermark"] = storage.current_watermark(conn)
                return self._send_json(exp)
            m = re.fullmatch(r"/api/experiments/([\w-]+)/analyses", path)
            if m:
                return self._send_json(
                    {"runs": [_run_digest(r) for r in
                              analysis.list_runs(conn, m.group(1))]})
            m = re.fullmatch(r"/api/analyses/(\d+)", path)
            if m:
                run = analysis.get_run(conn, int(m.group(1)))
                if not run:
                    raise ApiError(404, "run not found")
                return self._send_json(run)
            m = re.fullmatch(r"/api/experiments/([\w-]+)/decisions", path)
            if m:
                return self._send_json({
                    "decisions": decisions.list_decisions(conn, m.group(1)),
                    "rejected_attempts": decisions.list_attempts(conn, m.group(1))})
            m = re.fullmatch(r"/api/experiments/([\w-]+)/series", path)
            if m:
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                metric_id = q.get("metric", ["purchase"])[0]
                return self._send_json(self._series(conn, m.group(1), metric_id))
            if path == "/api/metrics":
                return self._send_json({"metrics": storage.list_metrics(conn)})
            raise ApiError(404, "not found")

        def _route_post(self):
            path = self.path.split("?")[0]
            body = self._body()
            conn = self._conn()
            if path == "/api/experiments":
                exp = storage.create_experiment(
                    conn, exp_id=body["id"], name=body["name"],
                    variants=body.get("variants", ["control", "treatment"]),
                    start_time=float(body.get("start_time", time.time())),
                    planned_days=int(body["planned_days"]),
                    planned_daily_n=int(body["planned_daily_n"]),
                    baseline_p=float(body["baseline_p"]),
                    alpha=float(body.get("alpha", 0.05)),
                    mde_abs=float(body["mde_abs"]),
                    tau=body.get("tau"), spending=body.get("spending", "obf"))
                return self._send_json(exp, 201)
            m = re.fullmatch(r"/api/experiments/([\w-]+)/events", path)
            if m:
                items = body.get("events") or []
                for it in items:
                    it["experiment_id"] = m.group(1)
                first, last = storage.ingest_events(conn, items)
                return self._send_json(
                    {"ingested": len(items), "first_seq": first, "last_seq": last}, 201)
            m = re.fullmatch(r"/api/experiments/([\w-]+)/analyses", path)
            if m:
                run = analysis.run_analysis(
                    conn, m.group(1), body["metric_id"],
                    metric_version=body.get("metric_version"),
                    as_of_seq=body.get("as_of_seq"))
                return self._send_json(_run_digest(run), 201)
            m = re.fullmatch(r"/api/experiments/([\w-]+)/decisions", path)
            if m:
                dec = decisions.confirm_decision(
                    conn, experiment_id=m.group(1), metric_id=body["metric_id"],
                    run_id=int(body["run_id"]), decision=body["decision"],
                    rationale=body.get("rationale", ""),
                    decided_by=body.get("decided_by", ""))
                if body["decision"] in ("stop_efficacy", "stop_futility", "rollback"):
                    storage.set_experiment_status(conn, m.group(1), "stopped")
                return self._send_json(dec, 201)
            m = re.fullmatch(r"/api/experiments/([\w-]+)/allocations", path)
            if m:
                weights = body["weights"]
                storage.ingest_events(conn, [{
                    "experiment_id": m.group(1), "event_type": "allocation_change",
                    "event_time": float(body.get("event_time", time.time())),
                    "payload": {"weights": weights,
                                "note": body.get("note", "")}}])
                return self._send_json({"ok": True, "weights": weights}, 201)
            m = re.fullmatch(r"/api/metrics/([\w-]+)/versions", path)
            if m:
                version = storage.add_metric_version(
                    conn, m.group(1), body["definition"], body.get("note", ""))
                return self._send_json({"metric_id": m.group(1), "version": version}, 201)
            raise ApiError(404, "not found")

        def _series(self, conn, exp_id: str, metric_id: str):
            """当前水位线下的日累计序列（迟到/撤销已参与重算）+ epoch + 迟到统计。"""
            exp = storage.get_experiment(conn, exp_id)
            if not exp:
                raise ApiError(404, "experiment not found")
            mdef = storage.get_metric_def(conn, metric_id)
            wm = storage.current_watermark(conn)
            now = time.time()
            looks = analysis.look_times(exp["start_time"], now)
            days = []
            if mdef:
                for lt in looks:
                    c = analysis.cumulative_counts(conn, exp_id, mdef["definition"],
                                                   lt, wm)
                    days.append({"time": lt, "n": c["n"], "x": c["x"]})
            return {"experiment_id": exp_id, "as_of_seq": wm, "as_of_time": now,
                    "days": days, "epochs": storage.allocation_epochs(conn, exp_id),
                    "late_data": analysis.late_data_summary(conn, exp_id, wm)}

    return Handler


def _run_digest(run: dict) -> dict:
    """run 的列表视图（去掉大轨迹，保留绑定信息与结论）。"""
    r = run["result"]
    return {"run_id": run["run_id"], "experiment_id": run["experiment_id"],
            "metric_id": run["metric_id"], "metric_version": run["metric_version"],
            "method_version": run["method_version"], "params": run["params"],
            "as_of_seq": run["as_of_seq"], "as_of_time": run["as_of_time"],
            "created_at": run["created_at"],
            "current": r["current"], "msprt": r["msprt"], "gsd": r["gsd"],
            "recommendation": r["recommendation"],
            "recommendation_reason": r["recommendation_reason"],
            "late_data": r["late_data"],
            "since_previous_run": r["since_previous_run"],
            "data_window": r["data_window"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="exp.db")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    conn = storage.connect(args.db)
    storage.init_db(conn)
    conn.close()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.db))
    print(f"实验分析平台: http://{args.host}:{args.port}  (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
